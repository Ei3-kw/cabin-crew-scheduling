#!/usr/bin/env python3
"""
Recompute the total crew cost of a two-layer result JSON under a SENIOR/NORMAL
differentiated labour-rate model.

Cost model (labour only, per crew member per minute):

                       flying ($/min)   waiting ($/min)
    senior                 420               1.0
    normal                 100               0.5

  * "flying" covers both operating a flight AND deadheading (riding as a
    passenger) AND a senior substituting into a normal seat -- a senior is
    always charged the senior rate, whatever the leg is doing. In the result
    JSON a substitution is already stored as a `flight` leg on the senior's
    route, so it is charged the senior flying rate automatically.
  * "waiting" is idle time between two consecutive legs. A layover at the crew's
    own home base is FREE (mandatory rest, no per-diem), exactly as the solver's
    wait arcs treat it; waiting away from base is charged the crew's wait rate.
  * This is a pure rate*minutes model: no deadhead seat-fare, no overnight flat
    penalty. The uncovered-slot penalty is added separately below as a flat
    big-M, matching the solver. The flights in a two-layer JSON do not carry
    distance/load-factor, so a fare-based deadhead cost is not derivable from
    this file anyway.

Usage:
    python3 compute_cost.py [results/result_ZW_twolayer.json]
"""
import sys
import json
from collections import defaultdict

# ── Rates: dollars per crew-member per minute ────────────────────────────────
RATE_FLY = {True: 420.0, False: 100.0}   # keyed by is_senior
RATE_WAIT = {True: 1.0,  False: 0.5}

# Uncovered-demand penalty. Each uncovered crew slot (a seat on a flight that no
# crew operates) is charged a flat big-M, exactly as the solver does, i.e.
#   penalty(slot on f) = C_UNC,  with C_UNC = 1e8.
# This is a coverage-forcing device, not a real cost: it sits orders of magnitude
# above any covering cost so the optimiser never trades a coverable flight for
# the penalty. A cancelled flight (no senior) contributes its full r_f slots; an
# understaffed flight contributes its shortfall r_f - (operators).
C_UNC = 10 ** 8

NORMAL_ID_OFFSET = 10 ** 8   # fallback senior/normal split if is_senior absent


def is_senior_map(data):
    """crew_id -> bool(is_senior), from the crew list; fall back to the id offset."""
    m = {}
    for c in data.get("crew", []):
        if "is_senior" in c:
            m[c["id"]] = bool(c["is_senior"])
        else:
            m[c["id"]] = c["id"] < NORMAL_ID_OFFSET
    return m


def compute_cost(data):
    senior = is_senior_map(data)

    # Per-class accumulators.
    fly_min   = defaultdict(float)   # operating-flight minutes
    dh_min    = defaultdict(float)   # deadhead minutes
    wait_min  = defaultdict(float)   # chargeable (away-from-base) waiting minutes
    fly_cost  = defaultdict(float)
    dh_cost   = defaultdict(float)
    wait_cost = defaultdict(float)
    n_legs    = defaultdict(int)

    operators = defaultdict(int)   # flight geometry (from,to,dep) -> # operating legs

    for r in data.get("routes", []):
        cid = r["crew_id"]
        sr = senior.get(cid, cid < NORMAL_ID_OFFSET)
        base = r.get("base")
        rfly, rwait = RATE_FLY[sr], RATE_WAIT[sr]

        legs = sorted(r.get("legs", []), key=lambda l: l.get("dep", 0))

        for l in legs:
            dur = max(0, l.get("arr", 0) - l.get("dep", 0))
            if l.get("type") == "flight":
                fly_min[sr]  += dur
                fly_cost[sr] += rfly * dur
                n_legs[sr]   += 1
                operators[(l.get("from"), l.get("to"), l.get("dep"))] += 1
            elif l.get("type") == "deadhead":
                dh_min[sr]  += dur
                dh_cost[sr] += rfly * dur     # deadhead charged at the flying rate
                n_legs[sr]  += 1

        # Waiting = gaps between consecutive legs; free when parked at home base.
        for a, b in zip(legs, legs[1:]):
            gap = b.get("dep", 0) - a.get("arr", 0)
            if gap <= 0:
                continue
            if a.get("to") == base:          # idle at home base -> free rest
                continue
            wait_min[sr]  += gap
            wait_cost[sr] += rwait * gap

    # ── Uncovered-demand penalty ─────────────────────────────────────────────
    # An uncovered slot is a required seat (r_f) with no operating crew. Each is
    # charged a flat big-M C_UNC = 1e8, matching the solver's coverage penalty.
    unc_slots = 0
    unc_penalty = 0.0
    for f in data.get("flights", []):
        geo = (f.get("origin"), f.get("dest"), f.get("dep_min"))
        rf = f.get("min_crew", 1)
        ops = operators.get(geo, 0)
        short = max(0, rf - ops)
        if short:
            unc_slots += short
            unc_penalty += short * C_UNC

    return {
        "senior": senior,
        "fly_min": fly_min, "dh_min": dh_min, "wait_min": wait_min,
        "fly_cost": fly_cost, "dh_cost": dh_cost, "wait_cost": wait_cost,
        "n_legs": n_legs,
        "unc_slots": unc_slots, "unc_penalty": unc_penalty,
    }


def report(data, res):
    def line(label, s_val, n_val, fmt="{:>18,.1f}"):
        print(f"  {label:<26}" + fmt.format(s_val) + fmt.format(n_val)
              + fmt.format(s_val + n_val))

    S, N = True, False
    n_sr = sum(1 for v in res["senior"].values() if v)
    n_nm = sum(1 for v in res["senior"].values() if not v)
    n_routes = len(data.get("routes", []))

    print("=" * 82)
    print(f"COST  —  {data.get('meta', {}).get('airline', '?')}  "
          f"(two-layer, {n_routes} active routes of {len(res['senior'])} crew)")
    print(f"  rates $/min — senior: fly {RATE_FLY[S]:.0f}, wait {RATE_WAIT[S]:.1f}"
          f"   |   normal: fly {RATE_FLY[N]:.0f}, wait {RATE_WAIT[N]:.1f}")
    print(f"  crew: {n_sr} senior, {n_nm} normal")
    print("=" * 82)
    print(f"  {'':<26}{'SENIOR':>18}{'NORMAL':>18}{'TOTAL':>18}")
    print("-" * 82)
    line("flying minutes",    res["fly_min"][S],  res["fly_min"][N])
    line("deadhead minutes",  res["dh_min"][S],   res["dh_min"][N])
    line("waiting minutes",   res["wait_min"][S], res["wait_min"][N])
    print("-" * 82)
    line("flying cost  $",    res["fly_cost"][S],  res["fly_cost"][N])
    line("deadhead cost $",   res["dh_cost"][S],   res["dh_cost"][N])
    line("waiting cost $",    res["wait_cost"][S], res["wait_cost"][N])
    print("-" * 82)
    sr_total = res["fly_cost"][S] + res["dh_cost"][S] + res["wait_cost"][S]
    nm_total = res["fly_cost"][N] + res["dh_cost"][N] + res["wait_cost"][N]
    line("crew TOTAL cost $",  sr_total,           nm_total)
    print("=" * 82)
    labour = sr_total + nm_total
    pen = res["unc_penalty"]
    print(f"  crew labour cost        : ${labour:>18,.2f}")
    print(f"  uncovered penalty       : ${pen:>18,.2f}"
          f"   ({res['unc_slots']} slots @ $1e8 big-M)")
    print("  " + "-" * 56)
    print(f"  GRAND TOTAL             : ${labour + pen:>18,.2f}")
    return labour + pen


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "results/result_ZW_twolayer.json"
    with open(path) as f:
        data = json.load(f)
    res = compute_cost(data)
    report(data, res)


if __name__ == "__main__":
    main()
