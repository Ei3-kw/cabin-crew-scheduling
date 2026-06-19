#!/usr/bin/env python3
"""
Validate that crew availability/clock state is tracked correctly in a solver
result.  Reconstructs each crew's physical timeline from `routes` and checks the
duty / rest / away / home-break rules, plus the key availability question:
at the moment of each UNCOVERED flight, was an eligible crew sitting idle at the
origin that the model failed to use?

Run:  python3 validate_availability.py result_ZW.json
"""
import json, sys
from collections import defaultdict

# Rule constants (mirror crew_ddd_v2.py)
DELTA_TA   = 45        # turnaround (min)
DELTA_REST = 8 * 60    # rest before next duty (min)
DELTA_DUTY = 14 * 60   # max on-duty per day (min)
DELTA_HB   = 48 * 60   # min home break after a long trip (min)
D_WORK     = 4         # max consecutive duty days
DELTA_AWAY = 4 * 1440  # max time away from home base (min)

def load(path):
    d = json.load(open(path))
    return d

def crew_legs(route):
    return sorted([l for l in route['legs'] if l['type'] in ('flight', 'deadhead')],
                  key=lambda l: l['dep'])

def clock_state_at(legs, base, t):
    """Home-break / away clock state at minute t (mirror home_break_clock.py).

    Returns (anchor, home_since, loc, arrived):
      anchor     : minute the crew last LEFT home AFTER a completed >=DELTA_HB home
                   break (or the first-ever leave-home). None if they never left home.
                   The DELTA_AWAY budget is measured as t - anchor.
      home_since : arrival that began the current continuous home stay, else None
                   (None means currently away).
      loc        : current ground airport at t.
      arrived    : arrival time at loc.

    Crucially, only a COMPLETED >=DELTA_HB continuous stay at base re-anchors the
    budget; brief touches at home do NOT, so a crew cannot dodge the cap by dipping
    through base.
    """
    anchor = None
    home_since = None
    loc, arrived = base, None
    for l in legs:
        if l['dep'] > t:
            break
        # Leaving home: seed on the first-ever departure, re-anchor only when the home
        # stay being left was itself a completed >=DELTA_HB break.
        if l['from'] == base and l['to'] != base:
            if anchor is None:
                anchor = l['dep']
            elif home_since is not None and (l['dep'] - home_since) >= DELTA_HB:
                anchor = l['dep']
            home_since = None
        # Arrival (only events that have happened by t).
        if l['arr'] <= t:
            loc, arrived = l['to'], l['arr']
            if l['to'] == base and home_since is None:
                home_since = l['arr']
    return anchor, home_since, loc, arrived

def validate(d):
    routes = d['routes']
    base_of = {r['crew_id']: r['base'] for r in routes}
    issues = defaultdict(list)   # check_name -> list of messages

    # ── Per-crew timeline checks ───────────────────────────────────────────────
    for r in routes:
        cid, base = r['crew_id'], r['base']
        legs = crew_legs(r)
        prev = None
        # Away-from-home budget state (mirror home_break_clock.py):
        #   anchor      = minute the crew last LEFT home after a completed >=48h home
        #                 break (or first-ever leave-home). Budget = t - anchor.
        #   home_since  = arrival starting the current continuous home stay (None = away).
        #   away_flagged= at most one away/home-break violation reported per away period.
        anchor = None
        home_since = None
        away_flagged = False
        for l in legs:
            # (1) continuity: location + turnaround + no time overlap
            if prev is not None:
                if l['from'] != prev['to']:
                    issues['continuity'].append(
                        f"crew {cid}: jumps from {prev['to']} to {l['from']} "
                        f"(no connecting leg) before dep {l['dep']}")
                if l['dep'] < prev['arr']:
                    issues['overlap'].append(
                        f"crew {cid}: leg dep {l['dep']} starts before previous "
                        f"arr {prev['arr']} (in two places at once)")
                elif l['dep'] < prev['arr'] + DELTA_TA and prev['to'] == l['from']:
                    issues['turnaround'].append(
                        f"crew {cid}: only {l['dep']-prev['arr']}min turnaround at "
                        f"{l['from']} (need {DELTA_TA}) before dep {l['dep']}")

            # (2) away-from-home-BREAK budget — the corrected thing this script tracks.
            #     The DELTA_AWAY (4-day) budget counts minutes since the crew last LEFT
            #     home after a completed >=48h home break (or since first leaving home).
            #     A brief touch at base does NOT reset it; only a completed >=DELTA_HB
            #     home stay re-anchors. We flag only a GENUINE over-cap: a crew that goes
            #     more than DELTA_AWAY past the anchor with no 48h break served at all. A
            #     break that completes slightly past anchor+DELTA_AWAY is NOT flagged.
            if l['to'] == base and home_since is None:
                home_since = l['arr']
            if l['from'] == base and l['to'] != base:                 # leaving home
                served = home_since is not None and (l['dep'] - home_since) >= DELTA_HB
                if anchor is None:
                    anchor = l['dep']; away_flagged = False           # first leave-home
                elif served:
                    anchor = l['dep']; away_flagged = False           # break served -> reset
                home_since = None
            # Over the cap: a leg departs more than DELTA_AWAY after the anchor while the
            # crew still owes a 48h home break (no break reset the budget).
            if anchor is not None and not away_flagged and (l['dep'] - anchor) > DELTA_AWAY:
                over = (l['dep'] - anchor) - DELTA_AWAY
                issues['d_away'].append(
                    f"crew {cid} ({base}): away {(l['dep']-anchor)/1440:.2f}d since leaving "
                    f"home at {anchor} with no 48h home break — leg dep {l['dep']} "
                    f"(day {l['dep']//1440+1}) is {over}min ({over/1440:.2f}d) past the "
                    f"{DELTA_AWAY/1440:.0f}d cap")
                away_flagged = True
            prev = l

        # (4) duty-block: ≤ D_WORK consecutive duty days without a 48h home break.
        # Segment legs into work-blocks separated by home stays of ≥ DELTA_HB (only a
        # real 48h break resets the streak; short overnight touches do NOT). A duty
        # day is any calendar day with a flight. Flag blocks exceeding D_WORK days.
        duty_days = set(); blk_first = None; prev_home_arr = None; loc = base
        for l in legs:
            dep = l['dep']
            if prev_home_arr is not None and loc == base and (dep - prev_home_arr) >= DELTA_HB:
                if len(duty_days) > D_WORK:
                    issues['duty_block'].append(
                        f"crew {cid} ({base}): worked {len(duty_days)} consecutive duty "
                        f"days from day {blk_first}-{max(duty_days)} with no 48h break "
                        f"(limit {D_WORK})")
                duty_days = set(); blk_first = None
            if l['type'] == 'flight':
                dd = dep // 1440
                duty_days.add(dd)
                if blk_first is None:
                    blk_first = dd
            if l['to'] == base:
                prev_home_arr = l['arr']
            loc = l['to']
        if len(duty_days) > D_WORK:
            issues['duty_block'].append(
                f"crew {cid} ({base}): worked {len(duty_days)} consecutive duty "
                f"days from day {blk_first}-{max(duty_days)} with no 48h break "
                f"(limit {D_WORK})")

    # ── Coverage / availability cross-check ────────────────────────────────────
    # Build a position function: where is each crew at time t, and is it idle?
    crew_timeline = {}
    for r in routes:
        crew_timeline[r['crew_id']] = crew_legs(r)

    def status_at(cid, t):
        """Return ('airborne'|'idle', airport_or_None)."""
        legs = crew_timeline[cid]
        loc, loc_since = base_of[cid], None
        for l in legs:
            if l['dep'] <= t < l['arr']:
                return 'airborne', None
            if l['arr'] <= t:
                loc, loc_since = l['to'], l['arr']
            if l['dep'] > t:
                break
        return 'idle', loc

    print("=" * 70)
    print("UNCOVERED-FLIGHT AVAILABILITY CROSS-CHECK")
    print("=" * 70)
    for uf in d.get('uncovered_flights', []):
        o, dep = uf['origin'], uf['dep_min']
        idle_here = []
        for r in routes:
            cid = r['crew_id']
            st, loc = status_at(cid, dep)
            if st == 'idle' and loc == o:
                # how long have they been sitting, and what do they do next?
                legs = crew_timeline[cid]
                nxt = next((l for l in legs if l['dep'] > dep), None)
                idle_here.append((cid, base_of[cid],
                                  nxt['dep'] if nxt else None,
                                  nxt['type'] if nxt else None))
        tag = "  <-- crew sitting idle at origin!" if idle_here else ""
        print(f"\n  {uf['flight_num']} {o}->{uf['dest']} dep={dep} "
              f"(day {dep//1440+1} {dep%1440//60:02d}:{dep%1440%60:02d}){tag}")
        for cid, b, nd, nt in idle_here:
            print(f"      crew {cid} (base {b}) idle here; next move: "
                  f"{nt} at {nd}" if nd else f"      crew {cid} (base {b}) idle, no further legs")

    # ── Summary ────────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("RULE-VIOLATION SUMMARY")
    print("=" * 70)
    order = ['continuity', 'overlap', 'turnaround', 'd_away', 'duty_block']
    total = 0
    for k in order:
        v = issues.get(k, [])
        total += len(v)
        print(f"  {k:12}: {len(v):4d} violation(s)")
        for msg in v:
            print(f"       - {msg}")
    print(f"\n  TOTAL: {total} violations")
    if total == 0:
        print("  Availability/clock state is internally consistent.")
    return total

if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv) > 1 else 'result_G7_twolayer.json'
    validate(load(path))


# ── Canonical crew-status state machine (port this into FlightViz) ─────────────
def crew_status(route, t):
    """Mutually-exclusive availability state for one crew at minute t.
    States: AIRBORNE, DEADHEADING, RESTING, HOME_BREAK, AVAILABLE_AT_BASE,
            AVAILABLE_AWAY, AWAY_OVERDUE."""
    base = route['base']
    legs = crew_legs(route)
    # in motion?
    for l in legs:
        if l['dep'] <= t < l['arr']:
            return 'DEADHEADING' if l['type'] == 'deadhead' else 'AIRBORNE'
    # Home-break / away clock state at t (anchored on the last 48h home break, not the
    # last time they left base — brief home touches do not reset the away budget).
    anchor, home_since, loc, arrived = clock_state_at(legs, base, t)
    # On a 48h home break: at base, the current continuous home stay is itself a >=48h
    # break and we are within its first 48h. (Checked before RESTING so a home break
    # isn't mislabelled as the 8h rest that begins it.)
    if loc == base and home_since is not None:
        nxt = next((l['dep'] for l in legs if l['dep'] > home_since), None)
        stay = (nxt - home_since) if nxt is not None else DELTA_HB  # open-ended stay = break
        if stay >= DELTA_HB and t < home_since + DELTA_HB:
            return 'HOME_BREAK'
    # currently away and over the 4-day budget since the last home-break departure?
    if loc != base and anchor is not None and (t - anchor) > DELTA_AWAY:
        return 'AWAY_OVERDUE'
    # within mandatory 8h rest after last arrival?
    if arrived is not None and (t - arrived) < DELTA_REST:
        return 'RESTING'
    return 'AVAILABLE_AT_BASE' if loc == base else 'AVAILABLE_AWAY'


def snapshot(d, t):
    from collections import Counter
    c = Counter(crew_status(r, t) for r in d['routes'])
    print(f"\n  Crew status snapshot at t={t} (day {t//1440+1} "
          f"{t%1440//60:02d}:{t%1440%60:02d}):")
    for k, v in sorted(c.items(), key=lambda x: -x[1]):
        print(f"    {k:18}: {v}")
    avail = c['AVAILABLE_AT_BASE'] + c['AVAILABLE_AWAY']
    print(f"    --> {avail}/{sum(c.values())} truly available; "
          f"{sum(c.values())-avail} NOT (airborne/rest/break)")