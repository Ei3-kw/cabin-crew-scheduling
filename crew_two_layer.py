#!/usr/bin/env python3
"""
Two-layer crew scheduling on top of the v5 flow solver.

Layer 1 (SENIOR):  every flight needs exactly ONE senior crew (a designated subset
    of the crew pool). Solved as the v5 min_crew=1 flow — its integral, root-solvable
    sweet spot. A flight that gets no senior is CANCELLED (no senior on board), and is
    dropped from layer 2.

Layer 2 (FILL):  every surviving flight needs its remaining (min_crew - 1) seats,
    filled by NORMAL crew. Solved as a v5 flow over the surviving flights with demand
    (min_crew - 1).

Senior substitution (a senior filling a normal seat in an idle gap of its layer-1
    schedule) is NOT yet modelled here — see the note at the bottom. This file builds
    the two-layer structure + cancellation; substitution is the next increment.

Run:  python3 crew_two_layer.py data/flights_enriched.csv 3 ZW
"""
import sys, os, json
from dataclasses import replace
from collections import defaultdict

import crew_ddd_v5 as v5

OUT_DIR = "results"

# Normal crew-ids are offset by this so they never collide with senior ids (the two
# pools are sized by independent size_crew_bases calls, each starting ids at 0).
NORMAL_ID_OFFSET = 10**6


def _fkey(f):
    """Stable per-flight key (flight_num + route + dep) to match across the layer JSONs."""
    return (str(f.get("flight_num")), f["origin"], f["dest"], f["dep_min"])


def _flight_obj_key(f):
    return (str(f.flight_num), f.origin, f.dest, f.dep_min)


def solve_two_layer(csv_path, days, airline):
    import time as _t
    t_start = _t.time()
    fba, _ = v5.parse_flights_by_airline(csv_path, days)
    if airline not in fba:
        sys.exit(f"airline {airline} not found; have {sorted(fba)[:10]}...")
    flights = fba[airline]
    os.makedirs(OUT_DIR, exist_ok=True)

    print("=" * 70)
    print(f"TWO-LAYER  airline={airline}  flights={len(flights)}")
    print("=" * 70)

    # ── LAYER 1: seniors, exactly 1 per flight ────────────────────────────────
    # Size the SENIOR pool for the layer-1 demand (1 crew per flight) — NOT the full
    # min_crew. So size_crew_bases runs on flights stamped min_crew=1.
    print("\n########## LAYER 1 — SENIOR (1 per flight) ##########")
    flights_L1 = [replace(f, min_crew=1) for f in flights]
    seniors = v5.size_crew_bases(flights_L1)
    print(f"  Senior pool sized for 1-per-flight demand: {len(seniors)} seniors")
    _t_l1 = _t.time()
    v5.solve_airline(f"{airline}_L1senior", flights_L1, days,
                     out_dir=OUT_DIR, crew_override=seniors)
    l1_secs = _t.time() - _t_l1
    L1 = json.load(open(f"{OUT_DIR}/result_{airline}_L1senior.json"))
    cancelled_keys = {_fkey(u) for u in L1["uncovered_flights"]}
    surviving = [f for f in flights if _flight_obj_key(f) not in cancelled_keys]
    n_cancelled = len(flights) - len(surviving)
    print(f"\nLayer 1 result: {len(surviving)}/{len(flights)} flights got a senior; "
          f"{n_cancelled} CANCELLED (no senior available).")

    # ── LAYER 2: normals fill the remaining (min_crew - 1) on surviving flights ─
    # Size the NORMAL pool independently, for the layer-2 fill demand on the SURVIVING
    # flights (cancelled flights need no normals). Offset ids so they don't clash with
    # senior ids. Flights with min_crew == 1 are fully crewed by their senior alone.
    print("\n########## LAYER 2 — FILL (normal crew, min_crew-1) ##########")
    flights_L2 = [replace(f, min_crew=f.min_crew - 1)
                  for f in surviving if f.min_crew > 1]
    _t_l2 = _t.time()
    if flights_L2:
        normals = [replace(c, id=c.id + NORMAL_ID_OFFSET)
                   for c in v5.size_crew_bases(flights_L2)]
        print(f"  Normal pool sized for (min_crew-1) fill demand: {len(normals)} normals")
        v5.solve_airline(f"{airline}_L2normal", flights_L2, days,
                         out_dir=OUT_DIR, crew_override=normals)
        L2 = json.load(open(f"{OUT_DIR}/result_{airline}_L2normal.json"))
    else:
        normals = []
        print("  No layer-2 demand (all surviving flights are min_crew=1).")
        L2 = {"routes": [], "uncovered_flights": []}
    l2_secs = _t.time() - _t_l2

    timings = {"start": t_start, "layer1": l1_secs, "layer2": l2_secs}
    combine_and_report(airline, flights, seniors, normals, L1, L2, surviving,
                       n_cancelled, timings=timings)


DELTA_REST = 8 * 60     # 8h rest
DELTA_TA   = 45         # 45-min turnaround


def senior_idle_gaps(route_legs):
    """Idle gaps in a senior's layer-1 route: each is (loc, avail_from, next_dep,
    next_from) — the senior sits at `loc` from `avail_from` until its next duty departs
    from `next_from` at `next_dep` (None = no further duty)."""
    legs = sorted([l for l in route_legs if l["type"] in ("flight", "deadhead")],
                  key=lambda l: l["dep"])
    gaps = []
    for i, l in enumerate(legs):
        nxt = legs[i + 1] if i + 1 < len(legs) else None
        gaps.append({"loc": l["to"], "from": l["arr"],
                     "next_dep": nxt["dep"] if nxt else None,
                     "next_from": nxt["from"] if nxt else None})
    return gaps


def can_substitute(gap, F):
    """Can a senior idle in `gap` fill fill-flight F without contradicting layer 1?
    It must be parked at F.origin, rested, and after F be able to reach its next senior
    duty legally (greedy: only if that next duty departs from F.dest, or it has none)."""
    O, D, dep, arr = F["origin"], F["dest"], F["dep_min"], F["arr_min"]
    if gap["loc"] != O:
        return False
    if dep - gap["from"] < DELTA_REST:          # not yet rested in this gap
        return False
    nd, nf = gap["next_dep"], gap["next_from"]
    if nd is None:                               # no further senior duty — free to fly F
        return True
    # After F the senior is at D at time arr; it must make its next duty (from nf at nd).
    return D == nf and arr + DELTA_TA <= nd


def apply_substitution(flights, seniors, L1, surviving_keys, understaffed):
    """Greedy senior substitution: fill the understaffed flights with idle seniors whose
    layer-1 schedule allows it. Returns the list of (flight, senior_id) substitutions."""
    senior_gaps = {r["crew_id"]: senior_idle_gaps(r["legs"]) for r in L1.get("routes", [])}
    used_gap = set()    # (senior_id, gap_index) already spent on a substitution
    subs = []
    for F in understaffed:
        for sid, gaps in senior_gaps.items():
            placed = False
            for gi, g in enumerate(gaps):
                if (sid, gi) in used_gap:
                    continue
                if can_substitute(g, F):
                    subs.append((F, sid))
                    used_gap.add((sid, gi))
                    placed = True
                    break
            if placed:
                break
    return subs


def _remap_l2_routes(l2_routes, combined_flights):
    """Layer 2 is solved on its OWN flight set, so its route legs carry L2-LOCAL flight
    ids (0..|L2|-1) that collide with — but do not match — the global ids used by L1 and
    the combined `flights` list. Without remapping, every normal/deadhead leg points at
    the wrong flight in the viz. Re-key each leg to the global flight id by its geometry
    (origin, dest, departure), which is stable across the two solves."""
    gid = {(f["origin"], f["dest"], f["dep_min"]): f["id"] for f in combined_flights}
    out = []
    for r in l2_routes:
        legs = []
        for l in r["legs"]:
            l = dict(l)
            if l.get("flight_id") is not None:
                l["flight_id"] = gid.get((l["from"], l["to"], l["dep"]))
            legs.append(l)
        rr = dict(r)
        rr["legs"] = legs
        out.append(rr)
    return out


def _flights_with_real_min_crew(l1_flights, flights):
    """L1's serialised flights carry min_crew=1 (the senior demand). Patch each back to
    the real total requirement from the original Flight objects, keyed by id."""
    real = {f.id: f.min_crew for f in flights}
    out = []
    for ff in l1_flights:
        ff = dict(ff)
        if ff.get("id") in real:
            ff["min_crew"] = real[ff["id"]]
        out.append(ff)
    return out


def _cap_overstaffing(routes, combined_flights, senior_ids):
    """Cap each flight at exactly its requirement -- 1 senior + (min_crew-1) normals
    OPERATING -- and reclassify any surplus operators as deadheads. The surplus crew
    still ride the flight (so routes stay continuous), they just no longer count as
    operating it. This is what they really were: repositioning crew that the flow model
    routes through a flight arc because operating (cost c_fl) is cheaper than a deadhead
    seat (c_dh = c_fl + fare).

    With the equality coverage constraint now enforced in the solver this is a no-op for
    fresh runs; it remains here so the combined schedule is clean regardless of the layer
    solves (e.g. cached pre-equality results). Mutates the leg dicts in `routes`."""
    req = {f["id"]: f["min_crew"] for f in combined_flights}
    ops = defaultdict(lambda: {"sr": [], "nm": []})
    for r in routes:
        is_sr = r["crew_id"] in senior_ids
        for l in r["legs"]:
            if l.get("type") == "flight" and l.get("flight_id") is not None:
                ops[l["flight_id"]]["sr" if is_sr else "nm"].append(l)
    n = 0
    for fid, o in ops.items():
        mc = req.get(fid, 1)
        for l in o["sr"][1:]:              # keep 1 senior operating, rest deadhead
            l["type"] = "deadhead"; n += 1
        for l in o["nm"][max(0, mc - 1):]:  # keep (min_crew-1) normals, rest deadhead
            l["type"] = "deadhead"; n += 1
    return n


def combine_and_report(airline, flights, seniors, normals, L1, L2, surviving,
                       n_cancelled, timings=None):
    import time as _t
    t_combine = _t.time()
    # working crew per flight: 1 senior (if surviving) + normals from L2
    # Count normals operating each flight from L2 routes (by flight_id within L2's
    # own flight set is awkward across the remap, so count by route legs' from/to/dep).
    normal_work = defaultdict(int)
    for r in L2.get("routes", []):
        for l in r["legs"]:
            if l["type"] == "flight":
                normal_work[(l["from"], l["to"], l["dep"])] += 1

    surviving_keys = {_flight_obj_key(f) for f in surviving}
    understaffed_flights = []
    fully = cancelled = 0
    short_slots = 0
    for f in flights:
        k = _flight_obj_key(f)
        if k not in surviving_keys:
            cancelled += 1
            continue
        need_normal = f.min_crew - 1
        got_normal = normal_work.get((f.origin, f.dest, f.dep_min), 0)
        if got_normal >= need_normal:
            fully += 1
        else:
            understaffed_flights.append(
                {"flight_num": f.flight_num, "origin": f.origin, "dest": f.dest,
                 "dep_min": f.dep_min, "arr_min": f.arr_min,
                 "short": need_normal - got_normal})
            short_slots += need_normal - got_normal

    # ── Senior substitution: fill the understaffed seats with idle seniors ──────
    subs = apply_substitution(flights, seniors, L1, surviving_keys, understaffed_flights)
    subbed_keys = {(F["origin"], F["dest"], F["dep_min"]) for F, _ in subs}
    understaffed = sum(1 for u in understaffed_flights
                       if (u["origin"], u["dest"], u["dep_min"]) not in subbed_keys)
    fully += len(subs)
    short_slots -= len(subs)

    print("\n" + "=" * 70)
    print("TWO-LAYER SUMMARY")
    print("=" * 70)
    print(f"  total flights        : {len(flights)}")
    print(f"  CANCELLED (no senior): {cancelled}")
    print(f"  fully crewed         : {fully}  (incl. {len(subs)} via senior substitution)")
    print(f"  understaffed (after substitution): {understaffed}  "
          f"({short_slots} normal seats short)")
    if subs:
        for F, sid in subs:
            print(f"      SUBSTITUTED  {F['flight_num']} {F['origin']}->{F['dest']} "
                  f"by senior #{sid}")
    print(f"  senior routes        : {len(L1.get('routes', []))}")
    print(f"  normal routes        : {len(L2.get('routes', []))}")

    runtime = None
    if timings is not None:
        combine_secs = _t.time() - t_combine
        total_secs = _t.time() - timings["start"]
        runtime = {"layer1": round(timings["layer1"], 1),
                   "layer2": round(timings["layer2"], 1),
                   "combine": round(combine_secs, 1),
                   "total": round(total_secs, 1)}
        print("  " + "-" * 52)
        print(f"  run time -- Layer 1 (senior solve)      : {timings['layer1']:8.1f}s")
        print(f"  run time -- Layer 2 (normal fill solve) : {timings['layer2']:8.1f}s")
        print(f"  run time -- combine + substitution      : {combine_secs:8.1f}s")
        print(f"  run time -- TOTAL                       : {total_secs:8.1f}s")

    # Write a combined result for the viz/validator: merge senior + normal routes.
    _combined_flights = _flights_with_real_min_crew(L1.get("flights", []), flights)
    # L1 routes already use global ids; L2 routes must be remapped to them (see above).
    _merged_routes = L1.get("routes", []) + _remap_l2_routes(L2.get("routes", []), _combined_flights)
    # Normalise any flow-model over-coverage: surplus operators -> deadhead.
    _n_capped = _cap_overstaffing(_merged_routes, _combined_flights, {c.id for c in seniors})
    if _n_capped:
        print(f"  normalised {_n_capped} surplus operator leg(s) -> deadhead (over-coverage)")
    combined = {
        "meta": {
            # Carry through the senior layer's v5 meta (horizon_end, days, solve_status,
            # costs, …) so the visualiser/validator have everything they expect, then
            # overlay the two-layer-specific fields.
            **L1.get("meta", {}),
            "airline": airline,
            "two_layer": True,
            "n_senior": len(seniors),
            "n_normal": len(normals),
            "num_flights": len(flights),
            "cancelled": cancelled,
            "fully_crewed": fully,
            "understaffed": understaffed,
            "runtime_secs": runtime,
        },
        "crew": [{"id": c.id, "base": c.base,
                  "is_senior": c in set(seniors)} for c in (seniors + normals)],
        # The senior layer stamped every flight min_crew=1; restore the REAL total
        # requirement (1 senior + (min_crew-1) normals) so the viz shows the right need.
        "flights": _combined_flights,
        "routes": _merged_routes,
        "uncovered_flights": L1.get("uncovered_flights", []),   # cancelled flights
    }
    out = f"{OUT_DIR}/result_{airline}_twolayer.json"
    json.dump(combined, open(out, "w"), indent=2)
    print(f"\n  Combined → {out}")


if __name__ == "__main__":
    import csv as _csv
    from collections import Counter as _Counter

    path = sys.argv[1] if len(sys.argv) > 1 else "data/flights_enriched.csv"
    # days = length of the coverage period; default 30 so the rolling horizon runs ALL
    # windows (build_windows(30) -> 10 commit windows). Pass a smaller value only to
    # test a short slice.
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 30

    # List airlines (most flights first) and let the user choose, exactly like v5's main.
    with open(path, encoding="utf-8") as _f:
        _counts = _Counter(row["OP_CARRIER"].strip()
                           for row in _csv.DictReader(_f)
                           if row.get("OP_CARRIER", "").strip())
    ranked = _counts.most_common()
    print(f"\n{len(ranked)} airlines found in {path}:\n")
    for i, (code, n) in enumerate(ranked, 1):
        print(f"  [{i:2d}]  {code:4s}  {n:>9,} flights")

    choice = sys.argv[3].strip() if len(sys.argv) > 3 else None
    if not choice:
        choice = input("\nChoose an airline (enter its code or list number): ").strip()
    num_to_code = {str(i): code for i, (code, _) in enumerate(ranked, 1)}
    valid_codes = {code for code, _ in ranked}
    if choice in num_to_code:
        airline = num_to_code[choice]
    elif choice.upper() in valid_codes:
        airline = choice.upper()
    else:
        raise SystemExit(f"'{choice}' is not a valid airline code or list number.")

    print(f"\nSelected airline: {airline} ({_counts[airline]:,} flights)  "
          f"coverage={days}d (all windows)")
    solve_two_layer(path, days, airline)

# ── NEXT STEP: senior substitution ────────────────────────────────────────────
# A senior may fill a normal seat ONLY inside an idle gap of its layer-1 schedule
# (not on rest/home-break, and able to return legal for its next senior duty). To add
# it without breaking reconstruction: in layer 2, add each senior as its own commodity
# whose source = its parked position at a gap start and sink = its next layer-1 duty
# start, time-windowed to the gap, routed through the layer-2 graph so the fill leg +
# return is clock-legal by construction. The senior commodity then contributes to the
# (min_crew-1) coverage alongside normals. This is a self-contained insertion into the
# fixed senior timeline — see the design discussion — and is the next increment.
