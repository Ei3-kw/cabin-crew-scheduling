"""
Flight Cabin Crew Scheduling — DIDP (DIDPPy)
=============================================

Problem
-------
A one-week planning window of flights read from a CSV (flights_enriched_copy.csv).
Each flight has:
  - ORIGIN, DEST airports
  - Scheduled departure/arrival times (CRS_DEP_TIME, CRS_ARR_TIME as HHMM integers)
  - MIN_CABIN_CREW  — minimum cabin crew required

Crew are of two types (Senior / Junior) and based at home airports.
Each crew member works a "pairing": an ordered sequence of flights that starts
and ends at their home base.  A crew can deadhead (position themselves on a flight
they don't staff) if required — but only flights from their base or reachable after
prior legs are served.

For this demonstration the CSV's template flights are replicated across 7 days.

DIDP model (per-crew pairing DP)
---------------------------------
State:
  * unassigned  – bit-set of flight indices not yet claimed by this crew member
  * location    – element variable, current airport index
  * cur_time    – integer resource (minutes from start-of-week), less_is_better
  * duty_used   – integer resource (minutes on duty), less_is_better

Transitions:
  * work(j)     – fly leg j as working crew (earns 1 coverage credit)
  * skip(j)     – forced removal of unreachable flight j
  * end_duty    – clear remainder and return to base (terminates pairing)

Base case: unassigned is empty AND location == home_base_idx
Cost:       total flights worked  (maximise)

Dual bounds:
  1. Sum of profits of all remaining unassigned flights
  2. Time-limited knapsack (density packing) of remaining legs

Outer loop: greedy — solve each crew member's pairing against remaining unfilled
slots; decrement counters after each assignment.
"""

import math, random, os, sys
import pandas as pd
import didppy as dp
from collections import defaultdict

random.seed(42069)

# ── constants ──────────────────────────────────────────────────────────────────
MIN_CONNECT   = 45    # minimum connection minutes at same airport
MAX_DUTY      = 900   # max duty-time minutes per pairing (15 h)
N_DAYS        = 7
WEEK_MIN      = N_DAYS * 1440
CREW_PER_BASE = 40     # crew members generated per base airport
SOLVER_TIME   = 12.0  # seconds per crew solve

# ── helpers ────────────────────────────────────────────────────────────────────

def hhmm_to_min(hhmm: int) -> int:
    return (hhmm // 100) * 60 + (hhmm % 100)

# ── 1. Load & expand flights ───────────────────────────────────────────────────

def load_flights(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path, low_memory=False)
    df = df[df["CANCELLED"] == 0].dropna(
        subset=["CRS_DEP_TIME", "CRS_ARR_TIME", "MIN_CABIN_CREW", "FL_DATE"])

    # Parse real calendar dates and restrict to the first N_DAYS distinct dates
    df["FL_DATE_parsed"] = pd.to_datetime(df["FL_DATE"], infer_datetime_format=True)
    all_dates  = sorted(df["FL_DATE_parsed"].unique())
    week_dates = all_dates[:N_DAYS]
    df = df[df["FL_DATE_parsed"].isin(week_dates)].copy()

    # Map each date to a day index 0..N_DAYS-1
    date_to_day = {d: i for i, d in enumerate(week_dates)}
    df["day"] = df["FL_DATE_parsed"].map(date_to_day)

    rows = []
    for fid, (_, r) in enumerate(df.iterrows()):
        day    = int(r["day"])
        offset = day * 1440
        dep    = hhmm_to_min(int(r["CRS_DEP_TIME"]))
        arr    = hhmm_to_min(int(r["CRS_ARR_TIME"]))
        if arr < dep:
            arr += 1440            # overnight flight crosses midnight
        jitter = random.randint(-3, 3)
        rows.append({
            "flight_id" : fid,
            "origin"    : r["ORIGIN"],
            "dest"      : r["DEST"],
            "dep_min"   : offset + dep + jitter,
            "arr_min"   : offset + arr + jitter,
            "min_crew"  : int(r["MIN_CABIN_CREW"]),
            "day"       : day,
        })

    result = pd.DataFrame(rows)
    print(f"\n  Dates used: {[str(d)[:10] for d in week_dates]}")
    return result

# ── 2. Generate crew pool ──────────────────────────────────────────────────────

def generate_crew(airports: list) -> list:
    crew, cid = [], 0
    for base in airports:
        for _ in range(CREW_PER_BASE):
            ctype = "Senior" if random.random() < 0.4 else "Junior"
            crew.append({"id": cid, "base": base, "type": ctype})
            cid += 1
    return crew

# ── 3. DIDP per-crew pairing DP ───────────────────────────────────────────────

def solve_pairing(crew: dict, flights: pd.DataFrame, need: dict) -> list:
    """
    Build and solve a DIDP model for one crew member's weekly pairing.

    Returns list of flight_ids this crew member should *work* (not deadhead).
    """
    base = crew["base"]

    # Restrict to flights that still need staff
    eligible = flights[
        flights["flight_id"].map(lambda f: need.get(int(f), 0) > 0)
    ].copy().reset_index(drop=True)

    # Juniors cannot work large aircraft (>4 crew required)
    if crew["type"] == "Junior":
        eligible = eligible[eligible["min_crew"] <= 4].reset_index(drop=True)

    if eligible.empty:
        return []

    N    = len(eligible)
    fids = list(eligible["flight_id"].astype(int))

    # Airport indices within this sub-problem
    all_ap = sorted(set([base] + list(eligible["origin"]) + list(eligible["dest"])))
    ap_idx = {a: i for i, a in enumerate(all_ap)}
    n_ap   = len(all_ap)
    base_i = ap_idx[base]

    dep_l  = eligible["dep_min"].astype(int).tolist()
    arr_l  = eligible["arr_min"].astype(int).tolist()
    dur_l  = [arr_l[j] - dep_l[j] for j in range(N)]
    orig_l = [ap_idx[o] for o in eligible["origin"]]
    dest_l = [ap_idx[d] for d in eligible["dest"]]
    prof_l = [1] * N          # 1 credit per flight covered

    # ── Model ────────────────────────────────────────────────────────────────
    model = dp.Model(maximize=True, float_cost=False)

    ft = model.add_object_type(number=N)
    at = model.add_object_type(number=n_ap)

    unassigned = model.add_set_var(object_type=ft, target=list(range(N)))
    location   = model.add_element_var(object_type=at, target=base_i)
    cur_time   = model.add_int_resource_var(target=0, less_is_better=True)
    duty_used  = model.add_int_resource_var(target=0, less_is_better=True)

    dep_t  = model.add_int_table(dep_l)
    arr_t  = model.add_int_table(arr_l)
    dur_t2 = model.add_int_table(dur_l)
    orig_t = model.add_element_table(orig_l)
    dest_t = model.add_element_table(dest_l)
    prof_t = model.add_int_table(prof_l)

    empty = model.create_set_const(object_type=ft, value=[])

    # ── Base case ─────────────────────────────────────────────────────────────
    model.add_base_case([unassigned.is_empty(), location == base_i])

    # ── Hard constraints ──────────────────────────────────────────────────────
    model.add_state_constr(duty_used  <= MAX_DUTY)
    model.add_state_constr(cur_time   <= WEEK_MIN)

    # ── Transitions ───────────────────────────────────────────────────────────

    for j in range(N):
        # WORK: fly leg j as working crew (must be at origin with time to connect)
        model.add_transition(dp.Transition(
            name=f"work {j}",
            cost=prof_t[j] + dp.IntExpr.state_cost(),
            preconditions=[
                unassigned.contains(j),
                location == orig_t[j],
                cur_time + MIN_CONNECT <= dep_t[j],
                duty_used + dur_t2[j]  <= MAX_DUTY,
            ],
            effects=[
                (unassigned, unassigned.remove(j)),
                (location,   dest_t[j]),
                (cur_time,   arr_t[j]),
                (duty_used,  duty_used + dur_t2[j]),
            ],
        ))

        # FORCED SKIP: immediately remove flight if impossible to reach in time
        model.add_transition(dp.Transition(
            name=f"skip {j}",
            cost=dp.IntExpr.state_cost(),
            preconditions=[
                unassigned.contains(j),
                (location != orig_t[j]) | (cur_time + MIN_CONNECT > dep_t[j]),
            ],
            effects=[(unassigned, unassigned.remove(j))],
        ), forced=True)

    # END DUTY: terminate pairing; clear list & return conceptually to base
    model.add_transition(dp.Transition(
        name="end duty",
        cost=dp.IntExpr.state_cost(),
        preconditions=[location != base_i],
        effects=[
            (unassigned, empty),
            (location,   base_i),
        ],
    ))

    # DONE: at base with nothing left
    model.add_transition(dp.Transition(
        name="done",
        cost=dp.IntExpr.state_cost(),
        preconditions=[location == base_i],
        effects=[(unassigned, empty)],
    ))

    # ── Dual bounds ───────────────────────────────────────────────────────────
    # Bound 1: sum of profits of remaining unassigned flights
    model.add_dual_bound(prof_t[unassigned])

    # Bound 2: time-limited knapsack on remaining legs (density packing)
    # Pre-compute for each flight j: max profit achievable starting from j
    tail_profits = []
    for j in range(N):
        time_avail = WEEK_MIN - arr_l[j]   # conservative: time left after j
        others = [(prof_l[k], dur_l[k]) for k in range(N) if k != j and dur_l[k] > 0]
        others.sort(key=lambda x: x[0]/x[1], reverse=True)
        acc, t = 0, time_avail
        for p, d in others:
            if d <= t:
                acc += p; t -= d
            else:
                acc += math.ceil(t * p / d); break
        tail_profits.append(acc)

    tail_t = model.add_int_table(tail_profits)
    model.add_dual_bound(tail_t[unassigned])      # max over remaining set

    # ── Solve ─────────────────────────────────────────────────────────────────
    solver   = dp.CABS(model, time_limit=SOLVER_TIME, threads=4)
    solution = solver.search()

    worked = []
    for t in solution.transitions:
        parts = t.name.split()
        if parts[0] == "work":
            worked.append(fids[int(parts[1])])
    return worked

# ── 4. Greedy outer assignment loop ───────────────────────────────────────────

def schedule(flights: pd.DataFrame, crew_pool: list) -> dict:
    need       = {int(r["flight_id"]): int(r["min_crew"])
                  for _, r in flights.iterrows()}
    assignment = defaultdict(list)

    for crew in crew_pool:
        remaining = sum(need.values())
        if remaining == 0:
            print("\n  ✓ All flights fully staffed — stopping early.")
            break

        print(f"\n  Crew {crew['id']:3d} | {crew['type']:6s} | @{crew['base']} "
              f"| Remaining slots: {remaining}")

        pairing = solve_pairing(crew, flights, need)

        for fid in pairing:
            if need.get(fid, 0) > 0:
                need[fid] -= 1
                assignment[crew["id"]].append(fid)

        n = len(assignment[crew["id"]])
        if n:
            print(f"    → Worked {n} flights: {assignment[crew['id']]}")
        else:
            print("    → No flights assigned")

    return dict(assignment)

# ── 5. Report ──────────────────────────────────────────────────────────────────

def report(flights: pd.DataFrame, crew_pool: list, assignment: dict):
    crew_by_id   = {c["id"]: c for c in crew_pool}
    flight_by_id = flights.set_index("flight_id")
    DAYS = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]

    print("\n" + "="*70)
    print("   WEEKLY CABIN CREW SCHEDULE  — DIDP Solution")
    print("="*70)

    for cid, fids in sorted(assignment.items()):
        if not fids: continue
        c = crew_by_id[cid]
        print(f"\n  Crew {cid:3d} | {c['type']:6s} | Base: {c['base']}")
        print(f"  {'Day':<5} {'Flt':>5} {'Route':<14} {'Dep':>6} {'Arr':>6} {'Dur':>5}")
        for fid in sorted(fids, key=lambda f: flight_by_id.loc[f,"dep_min"]):
            r  = flight_by_id.loc[fid]
            d  = int(r["dep_min"]) % 1440
            a  = int(r["arr_min"]) % 1440
            dh, dm = divmod(d, 60)
            ah, am = divmod(a, 60)
            dur = int(r["arr_min"]) - int(r["dep_min"])
            print(f"  {DAYS[int(r['day'])]:<5} {fid:>5}  "
                  f"{r['origin']} → {r['dest']:<6} "
                  f" {dh:02d}:{dm:02d}  {ah:02d}:{am:02d}  {dur:>3}m")

    total  = int(flights["min_crew"].sum())
    filled = sum(len(v) for v in assignment.values())
    used   = sum(1 for v in assignment.values() if v)

    print(f"\n{'='*70}")
    print(f"  Coverage summary")
    print(f"  {'Total crew-slots required':<40}: {total:>4}")
    print(f"  {'Crew-slots filled (DIDP)':<40}: {filled:>4}  ({filled/total*100:.1f}%)")
    print(f"  {'Crew members with ≥1 assignment':<40}: {used:>4} / {len(crew_pool)}")
    print("="*70)

# ── 6. Main ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    csv_path = "data/flights_enriched.csv"
    if not os.path.exists(csv_path):
        sys.exit(f"CSV not found at {csv_path}")

    print("─"*65)
    print("  Domain-Independent Dynamic Programming")
    print("  Flight Cabin Crew Scheduling — 1-Week Horizon")
    print("─"*65)

    flights   = load_flights(csv_path)
    airports  = sorted(set(list(flights["origin"]) + list(flights["dest"])))
    crew_pool = generate_crew(airports)

    print(f"\n  Flights : {len(flights)} legs | "
          f"Airports: {airports} | "
          f"Crew-slots: {int(flights['min_crew'].sum())}")
    print(f"  Crew    : {len(crew_pool)} members  ({CREW_PER_BASE} per base)")
    print(f"\n  Crew roster:")
    for c in crew_pool:
        print(f"    Crew {c['id']:3d}: {c['type']:6s} @ {c['base']}")

    print(f"\n  Solving (CABS, {SOLVER_TIME}s limit per crew member)…")
    assignment = schedule(flights, crew_pool)

    report(flights, crew_pool, assignment)