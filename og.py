#!/usr/bin/env python3
"""
Cabin Crew Pairing Problem Solver
==================================
Implements Models 1 (TCCPP) and 2 (MICCPP-ACCS) from:
  Wen et al. (2022) – "Formulations for the Cabin Crew Pairing Problem"

Data: flights_enriched_copy.csv
  Key columns used: ORIGIN, DEST, CRS_DEP_TIME, CRS_ARR_TIME,
                    CRS_ELAPSED_TIME, MIN_CABIN_CREW, FL_DATE, CANCELLED, DIVERTED

NOTE: The uploaded CSV is a format sample (3 flights). When fewer than 10 flights
are present, synthetic flights are generated in the same schema so the full model
can be demonstrated. Replace the CSV with a full week's schedule to solve for real.
"""

import pandas as pd
import numpy as np
import gurobipy as gp
from gurobipy import GRB
import warnings
from collections import defaultdict

warnings.filterwarnings("ignore")
np.random.seed(42)

# ─────────────────────────────────────────────────────────
# 1.  CONSTANTS
# ─────────────────────────────────────────────────────────
MIN_CONNECT   = 45     # minimum connection time (minutes)
BRIEF         = 60     # briefing  time before first departure (minutes)
DEBRIEF       = 30     # debriefing time after last arrival  (minutes)
MAX_TAFB      = 24*60  # maximum Time Away From Base per pairing (minutes) – 24 h for 1-day horizon
MAX_LEGS      = 4      # maximum legs per pairing
AVAIL_FACTOR  = 1.10   # available crew = 110 % of minimum-cover level (approximation)
MU            = 1e4    # substitution penalty  (cost ≪ μ ≪ M)
M_BIG         = 1e6    # extra-crew  big-M penalty

# Crew classes: indices into R
# Class 0 = Senior  (1 required per flight)
# Class 1 = Junior  (MIN_CABIN_CREW − 2 required, ≥ 1)
# Class 2 = Trainee (1 required per flight, only when MIN_CABIN_CREW ≥ 3)
CREW_CLASSES  = [0, 1, 2]
CLASS_NAMES   = {0: "Senior", 1: "Junior", 2: "Trainee"}


# ─────────────────────────────────────────────────────────
# 2.  LOAD & PREPROCESS FLIGHTS
# ─────────────────────────────────────────────────────────
def hhmm_to_min(val):
    """Convert CRS_DEP/ARR_TIME integer HHMM → minutes from midnight."""
    v = int(val)
    return (v // 100) * 60 + (v % 100)


def load_flights(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df = df[
        (df["CANCELLED"] == 0) &
        (df["DIVERTED"]  == 0) &
        df["CRS_DEP_TIME"].notna() &
        df["CRS_ARR_TIME"].notna() &
        df["MIN_CABIN_CREW"].notna() &
        df["ORIGIN"].notna() &
        df["DEST"].notna()
    ].copy()

    df["FL_DATE"]        = pd.to_datetime(df["FL_DATE"])
    df["day_num"]        = (df["FL_DATE"] - df["FL_DATE"].min()).dt.days
    df["dep_min"]        = df["CRS_DEP_TIME"].apply(hhmm_to_min) + df["day_num"] * 1440
    df["arr_min"]        = df["CRS_ARR_TIME"].apply(hhmm_to_min) + df["day_num"] * 1440
    # Handle overnight arrivals within the same calendar day
    overnight = df["arr_min"] < df["dep_min"]
    df.loc[overnight, "arr_min"] += 1440
    df["MIN_CABIN_CREW"] = df["MIN_CABIN_CREW"].astype(int).clip(lower=2)
    df = df.reset_index(drop=True)
    df["fid"] = ["F%03d" % i for i in range(len(df))]
    return df


# ─────────────────────────────────────────────────────────
# 3.  SYNTHETIC DATA (used when CSV has < 10 flights)
# ─────────────────────────────────────────────────────────
def make_synthetic_flights() -> pd.DataFrame:
    """
    Generate a plausible one-day short-haul schedule for 5 hubs.
    Airports and rough distances are loosely based on the sample CSV.
    """
    hubs = ["JFK", "LAX", "ORD", "CLT", "DFW"]
    # (origin, dest, dep_HHMM, elapsed_min, crew_req)
    schedule_template = [
        ("JFK","LAX",700,370,5), ("JFK","LAX",1000,370,5),
        ("JFK","ORD",600,150,4), ("JFK","ORD",1300,150,4),
        ("JFK","CLT",700,100,3), ("JFK","CLT",1600,100,3),
        ("LAX","JFK",800,310,5), ("LAX","JFK",1400,310,5),
        ("LAX","ORD",900,220,4), ("LAX","ORD",1500,220,4),
        ("LAX","DFW",700,175,4), ("LAX","DFW",1800,175,4),
        ("ORD","JFK",700,140,4), ("ORD","JFK",1600,140,4),
        ("ORD","LAX",800,230,4), ("ORD","LAX",1700,230,4),
        ("ORD","CLT",900,115,3), ("ORD","DFW",1000,175,4),
        ("CLT","JFK",600, 95,3), ("CLT","JFK",1500, 95,3),
        ("CLT","ORD",800,115,3), ("CLT","DFW",1100,165,3),
        ("DFW","LAX",700,185,4), ("DFW","LAX",1600,185,4),
        ("DFW","ORD",800,165,4), ("DFW","CLT",900,170,3),
        ("DFW","JFK",1000,200,5), ("DFW","JFK",1700,200,5),
    ]
    rows = []
    for i, (orig, dest, dep, elapsed, crew) in enumerate(schedule_template):
        dep_m = hhmm_to_min(dep)
        arr_m = dep_m + elapsed
        arr_hhmm = (arr_m // 60) * 100 + (arr_m % 60)
        rows.append({
            "ORIGIN": orig, "DEST": dest,
            "CRS_DEP_TIME": dep, "CRS_ARR_TIME": arr_hhmm,
            "CRS_ELAPSED_TIME": elapsed, "MIN_CABIN_CREW": crew,
            "FL_DATE": "1/1/2025 12:00:00 AM",
            "CANCELLED": 0, "DIVERTED": 0,
            "day_num": 0,
            "dep_min": dep_m, "arr_min": arr_m,
            "fid": "F%03d" % i,
        })
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────
# 4.  CREW REQUIREMENTS PER CLASS
# ─────────────────────────────────────────────────────────
def req_rf(total_crew: int, r: int) -> int:
    """Per-class crew requirement on a flight with `total_crew` total cabin crew."""
    if r == 0:   return 1                          # 1 Senior always
    if r == 2:   return 1                          # 1 Trainee always
    return max(total_crew - 2, 1)                  # Juniors fill the rest


# ─────────────────────────────────────────────────────────
# 5.  PAIRING GENERATION  (duty-based network, simplified)
# ─────────────────────────────────────────────────────────
def can_connect(f1: dict, f2: dict) -> bool:
    """True if f2 can follow f1 (same layover airport, sufficient connection time)."""
    return (f1["DEST"] == f2["ORIGIN"] and
            f2["dep_min"] >= f1["arr_min"] + MIN_CONNECT)


def tafb(legs: list, info: dict) -> float:
    """TAFB in hours for a pairing defined by a list of flight IDs."""
    first = info[legs[0]]
    last  = info[legs[-1]]
    return (last["arr_min"] + DEBRIEF - (first["dep_min"] - BRIEF)) / 60.0


def generate_pairings(home_base: str, info: dict, max_legs: int = MAX_LEGS):
    """
    DFS over the flight-connection graph to find all feasible pairings
    (paths) that start and end at `home_base`.
    """
    pairings = []
    outbound = [fid for fid, f in info.items() if f["ORIGIN"] == home_base]

    def dfs(path, cur_airport):
        last_f = info[path[-1]]
        # Valid pairing: returned to base with ≥ 2 legs and TAFB ≤ limit
        if cur_airport == home_base and len(path) >= 2:
            if tafb(path, info) <= MAX_TAFB / 60:
                pairings.append(tuple(path))
        if len(path) >= max_legs:
            return
        for nxt_fid, nxt_f in info.items():
            if nxt_fid not in path and can_connect(last_f, nxt_f):
                if tafb(path + [nxt_fid], info) <= MAX_TAFB / 60:
                    dfs(path + [nxt_fid], nxt_f["DEST"])

    for fid in outbound:
        dfs([fid], info[fid]["DEST"])
    return pairings


def build_pairing_index(flights: pd.DataFrame, home_bases: list):
    """
    Returns:
      pairings   : list of pairing IDs  (P_r, same for every class – all are cross-qualified)
      p_covers   : dict {pid → set of fids}
      p_tafb     : dict {pid → TAFB hours}
      pe_pairings: list of extra-crew pairing IDs (single-leg, any flight)
      pe_covers  : dict {pid → {fid}}
      pe_tafb    : dict {pid → TAFB hours}
    """
    info = flights.set_index("fid")[
        ["ORIGIN","DEST","dep_min","arr_min"]
    ].to_dict("index")

    pairings, p_covers, p_tafb = [], {}, {}
    for base in home_bases:
        for legs in generate_pairings(base, info):
            pid = "P_" + "_".join(legs)
            if pid not in p_covers:
                pairings.append(pid)
                p_covers[pid] = set(legs)
                p_tafb[pid]   = tafb(list(legs), info)

    # Extra-crew pairings: one per flight, single-leg (no base constraint – extra crew
    # are positioned externally and incur the big-M penalty regardless of base)
    pe_pairings, pe_covers, pe_tafb = [], {}, {}
    for _, row in flights.iterrows():
        pid = "PE_" + row["fid"]
        pe_pairings.append(pid)
        pe_covers[pid] = {row["fid"]}
        pe_tafb[pid]   = (row["arr_min"] + DEBRIEF - (row["dep_min"] - BRIEF)) / 60.0

    return pairings, p_covers, p_tafb, pe_pairings, pe_covers, pe_tafb


# ─────────────────────────────────────────────────────────
# 6.  MODEL 1 – TCCPP
# ─────────────────────────────────────────────────────────
def solve_tccpp(flights, pairings, p_covers, p_tafb):
    """
    (TCCPP)  min  Σ_{t∈T} cost_t · select_t          (Eq. 1)
    s.t.     Σ_{t∈T} covers_{ft} · select_t ≥ 1,  ∀f  (Eq. 2)
             select_t ∈ {0,1}                          (Eq. 3)

    Cabin crew treated as homogeneous; pairings = team pairings.
    """
    F = list(flights["fid"])
    T = pairings

    m = gp.Model("TCCPP")
    m.setParam("OutputFlag", 0)
    m.setParam("TimeLimit", 120)

    # Variables (Eq. 3)
    select = m.addVars(T, vtype=GRB.BINARY, name="sel")

    # Objective (Eq. 1)
    m.setObjective(gp.quicksum(p_tafb[t] * select[t] for t in T), GRB.MINIMIZE)

    # Coverage constraints (Eq. 2)
    for f in F:
        covering = [t for t in T if f in p_covers[t]]
        if covering:
            m.addConstr(gp.quicksum(select[t] for t in covering) >= 1, name=f"cov_{f}")

    m.optimize()
    return m, select


# ─────────────────────────────────────────────────────────
# 7.  MODEL 2 – MICCPP-ACCS
# ─────────────────────────────────────────────────────────
def solve_miccpp_accs(flights, pairings, p_covers, p_tafb,
                      pe_pairings, pe_covers, pe_tafb, avail):
    """
    (MICCPP-ACCS)
    min  Σ_r Σ_p cost^avail_{rp}·assign_{rp}          available-crew cost
       + Σ_r Σ_f μ · sub_{rf}                         substitution penalty
       + Σ_r Σ_p (cost^extra_{rp}+M)·extra_{rp}       extra-crew cost      (Eq. 4)

    s.t. (5) Total-satisfaction   – total assigned ≥ Σ_r req_{rf}
         (6) Minimum-satisfaction – each class r covers flight f ≥ 1
         (7) Substitution recording
         (8) Availability limit
         (9–11) Integrality
    """
    F = list(flights["fid"])
    R = CREW_CLASSES
    Pr   = {r: pairings    for r in R}   # cross-qualified
    Pe_r = {r: pe_pairings for r in R}

    # Build requirement lookup
    req = {}
    for _, row in flights.iterrows():
        for r in R:
            req[(r, row["fid"])] = req_rf(int(row["MIN_CABIN_CREW"]), r)

    # ── variables (Eq. 9–11) ────────────────────────────────────
    m = gp.Model("MICCPP_ACCS")
    m.setParam("OutputFlag", 0)
    m.setParam("TimeLimit", 180)

    assign = m.addVars([(r,p) for r in R for p in Pr[r]],
                       vtype=GRB.INTEGER, lb=0, name="asgn")
    extra  = m.addVars([(r,p) for r in R for p in Pe_r[r]],
                       vtype=GRB.INTEGER, lb=0, name="xtra")
    sub    = m.addVars([(r,f) for r in R for f in F],
                       vtype=GRB.INTEGER, lb=0, name="sub")

    # ── objective (Eq. 4) ───────────────────────────────────────
    avail_cost = gp.quicksum(p_tafb[p]  * assign[r,p] for r in R for p in Pr[r])
    sub_cost   = gp.quicksum(MU         * sub[r,f]    for r in R for f in F)
    extra_cost = gp.quicksum((pe_tafb[p] + M_BIG) * extra[r,p]
                             for r in R for p in Pe_r[r])
    m.setObjective(avail_cost + sub_cost + extra_cost, GRB.MINIMIZE)

    for f in F:
        total_req_f = sum(req[(r,f)] for r in R)

        # (5) Total-satisfaction
        m.addConstr(
            gp.quicksum(assign[r,p] for r in R for p in Pr[r]   if f in p_covers[p])
          + gp.quicksum(extra[r,p]  for r in R for p in Pe_r[r] if f in pe_covers[p])
          >= total_req_f,
            name=f"total_sat_{f}"
        )

        for r in R:
            avail_cover = [p for p in Pr[r]   if f in p_covers[p]]
            extra_cover = [p for p in Pe_r[r] if f in pe_covers[p]]
            crew_on_f = (
                gp.quicksum(assign[r,p] for p in avail_cover)
              + gp.quicksum(extra[r,p]  for p in extra_cover)
            )

            # (6) Minimum-satisfaction: ≥ 1 from each class
            m.addConstr(crew_on_f >= 1, name=f"min_sat_{r}_{f}")

            # (7) Substitution recording
            m.addConstr(crew_on_f + sub[r,f] >= req[(r,f)],
                        name=f"sub_rec_{r}_{f}")

    # (8) Crew availability
    for r in R:
        m.addConstr(gp.quicksum(assign[r,p] for p in Pr[r]) <= avail[r],
                    name=f"avail_{r}")

    m.optimize()
    return m, assign, extra, sub, req


# ─────────────────────────────────────────────────────────
# 8.  REPORTING
# ─────────────────────────────────────────────────────────
_STATUS = {
    GRB.OPTIMAL:       "Optimal",
    GRB.INFEASIBLE:    "Infeasible",
    GRB.INF_OR_UNBD:   "Infeasible or Unbounded",
    GRB.TIME_LIMIT:    "Time limit (best solution shown)",
    GRB.SUBOPTIMAL:    "Sub-optimal",
}

def section(title):
    print("\n" + "═"*60)
    print(f"  {title}")
    print("═"*60)

def report_tccpp(m, select, pairings, p_covers, p_tafb, flights):
    section("MODEL 1 : TCCPP (Traditional – Homogeneous Teams)")
    F   = set(flights["fid"])
    sts = _STATUS.get(m.Status, f"Code {m.Status}")
    print(f"  Solver status      : {sts}")
    if m.Status not in (GRB.OPTIMAL, GRB.TIME_LIMIT, GRB.SUBOPTIMAL):
        return
    if m.SolCount == 0:
        print("  (No solution found within time limit)")
        return

    sel     = [t for t in pairings if select[t].X > 0.5]
    covered = set()
    for t in sel:
        covered |= p_covers[t]

    total_tafb = sum(p_tafb[t] for t in sel)
    print(f"  MIP gap            : {m.MIPGap*100:.2f}%")
    print(f"  Total pairings     : {len(pairings)}")
    print(f"  Selected pairings  : {len(sel)}")
    print(f"  Total TAFB (hrs)   : {total_tafb:.1f}")
    print(f"  Flights covered    : {len(covered & F)}/{len(F)}")
    print(f"  Uncovered flights  : {sorted(F - covered) or 'None'}")

    if sel:
        print(f"\n  {'Pairing ID':<40} {'Legs':>4}  {'TAFB (h)':>8}")
        print("  " + "-"*55)
        for t in sorted(sel, key=lambda x: -p_tafb[x])[:15]:
            print(f"  {t[:40]:<40} {len(p_covers[t]):>4}  {p_tafb[t]:>8.2f}")
        if len(sel) > 15:
            print(f"  … ({len(sel)-15} more pairings not shown)")


def report_miccpp(m, assign, extra, sub, pairings, p_covers, p_tafb,
                  pe_pairings, pe_covers, pe_tafb, flights, avail, req):
    section("MODEL 2 : MICCPP-ACCS (Multi-Class + Controlled Crew Substitution)")
    F   = list(flights["fid"])
    R   = CREW_CLASSES
    sts = _STATUS.get(m.Status, f"Code {m.Status}")
    print(f"  Solver status      : {sts}")
    if m.Status not in (GRB.OPTIMAL, GRB.TIME_LIMIT, GRB.SUBOPTIMAL):
        return
    if m.SolCount == 0:
        print("  (No solution found within time limit)")
        return

    def X(v): return v.X

    avail_cost_v = sum(p_tafb[p]  * X(assign[r,p]) for r in R for p in pairings)
    sub_cost_v   = sum(MU         * X(sub[r,f])    for r in R for f in F)
    extra_cost_v = sum((pe_tafb[p] + M_BIG) * X(extra[r,p])
                       for r in R for p in pe_pairings)

    print(f"  MIP gap            : {m.MIPGap*100:.2f}%")
    print(f"\n  ── Objective breakdown ──")
    print(f"  Available-crew TAFB cost : {avail_cost_v:>12.1f} hrs")
    print(f"  Substitution penalty     : {sub_cost_v:>12.1f}  (μ={MU:.0f})")
    print(f"  Extra-crew penalty       : {extra_cost_v:>12.1f}  (M={M_BIG:.0f})")
    print(f"  ─────────────────────────────────────────")
    print(f"  Total objective          : {avail_cost_v+sub_cost_v+extra_cost_v:>12.1f}")

    print(f"\n  ── Workforce summary ──")
    for r in R:
        used     = sum(X(assign[r,p]) for p in pairings)
        used_xtr = sum(X(extra[r,p])  for p in pe_pairings)
        n_subs   = sum(X(sub[r,f])    for f in F)
        print(f"  Class {r} ({CLASS_NAMES[r]:<7}):  "
              f"avail_cap={avail[r]:4d}  used={used:5.0f}  "
              f"extra={used_xtr:5.0f}  subs={n_subs:5.0f}")

    total_assign = sum(X(assign[r,p]) for r in R for p in pairings)
    total_subs   = sum(X(sub[r,f])    for r in R for f in F)
    total_extra  = sum(X(extra[r,p])  for r in R for p in pe_pairings)
    print(f"\n  Total assignments (available crew) : {total_assign:.0f}")
    print(f"  Total substitutions (CCS events)   : {total_subs:.0f}")
    print(f"  Total extra-crew assignments       : {total_extra:.0f}")

    sub_per_flight = {f: sum(X(sub[r,f]) for r in R) for f in F}
    non_zero = [(f,s) for f,s in sorted(sub_per_flight.items(),
                key=lambda x:-x[1]) if s > 0]
    if non_zero:
        fmap = flights.set_index("fid")[["ORIGIN","DEST"]].to_dict("index")
        print(f"\n  ── Flights with substitutions ──")
        print(f"  {'Flight':>6}  {'Total subs':>10}  {'Route'}")
        print("  " + "-"*45)
        for f, s in non_zero[:10]:
            print(f"  {f:>6}  {s:>10.0f}  {fmap[f]['ORIGIN']}→{fmap[f]['DEST']}")
    else:
        print("\n  No cross-class substitutions required.")


# ─────────────────────────────────────────────────────────
# 9.  MAIN
# ─────────────────────────────────────────────────────────
def main():
    CSV_PATH = "data/flights_enriched.csv"

    # ── Load real data ──────────────────────────────────
    print("Loading flight data …")
    flights = load_flights(CSV_PATH)

    if len(flights) < 10:
        print(f"  Only {len(flights)} usable flight(s) in CSV "
              f"(sample/format-only file detected).")
        print("  Generating synthetic schedule for demonstration …\n")
        flights = make_synthetic_flights()

    # Limit to one planning day for tractability
    day0 = flights["day_num"].min()
    flights = flights[flights["day_num"] == day0].reset_index(drop=True)
    flights["fid"] = ["F%03d" % i for i in range(len(flights))]
    print(f"  Planning horizon   : day {day0}   ({len(flights)} flights)")

    # ── Home bases: top 5 by departure frequency ────────
    top_bases = flights["ORIGIN"].value_counts().head(5).index.tolist()
    print(f"  Home bases         : {top_bases}")

    # ── Pairing generation ──────────────────────────────
    print("\nGenerating pairings …")
    pairings, p_covers, p_tafb, pe_pairings, pe_covers, pe_tafb = \
        build_pairing_index(flights, top_bases)
    print(f"  Available-crew pairings : {len(pairings)}")
    print(f"  Extra-crew pairings     : {len(pe_pairings)}")

    F = list(flights["fid"])
    coverable = {f for p in pairings for f in p_covers[p]}
    print(f"  Flights coverable by pairings : {len(coverable)}/{len(F)}")
    uncoverable = set(F) - coverable
    if uncoverable:
        print(f"  Uncoverable flights (will need extra crew): {sorted(uncoverable)}")

    # ── Crew availability ───────────────────────────────
    # Minimum crew needed per class (sum over all flights)
    min_crew = {r: sum(req_rf(int(row["MIN_CABIN_CREW"]), r)
                       for _, row in flights.iterrows())
                for r in CREW_CLASSES}
    avail = {r: max(int(min_crew[r] * AVAIL_FACTOR), 1) for r in CREW_CLASSES}
    print(f"\n  Crew availability cap  : { {CLASS_NAMES[r]: avail[r] for r in CREW_CLASSES} }")

    # # ─────────────────────────────────────────────────────
    # # MODEL 1 – TCCPP
    # # ─────────────────────────────────────────────────────
    # print("\nSolving Model 1: TCCPP …")
    # prob1, select = solve_tccpp(flights, pairings, p_covers, p_tafb)
    # report_tccpp(prob1, select, pairings, p_covers, p_tafb, flights)

    # ─────────────────────────────────────────────────────
    # MODEL 2 – MICCPP-ACCS
    # ─────────────────────────────────────────────────────
    print("\nSolving Model 2: MICCPP-ACCS …")
    prob2, assign, extra, sub, req = solve_miccpp_accs(
        flights, pairings, p_covers, p_tafb,
        pe_pairings, pe_covers, pe_tafb, avail)
    report_miccpp(prob2, assign, extra, sub,
                  pairings, p_covers, p_tafb,
                  pe_pairings, pe_covers, pe_tafb,
                  flights, avail, req)

    # ─────────────────────────────────────────────────────
    # MANPOWER BENCHMARKS
    # ─────────────────────────────────────────────────────
    section("MANPOWER BENCHMARKS")
    print(f"  {'Benchmark':<10}  {'Class':<10}  {'Value':>8}  Description")
    print("  " + "-"*70)
    for r in CREW_CLASSES:
        mc_r = sum(req_rf(int(row["MIN_CABIN_CREW"]), r)
                   for _, row in flights.iterrows())
        mm_r = len(flights)   # one crew per flight = minimum satisfaction
        ta_r = avail[r]
        print(f"  {'MC_r':<10}  {CLASS_NAMES[r]:<10}  {mc_r:>8d}  "
              f"Min class demand (no CCS)   TA={ta_r}")
        print(f"  {'MM_r':<10}  {CLASS_NAMES[r]:<10}  {mm_r:>8d}  "
              f"Min satisfaction (1/flight)")
    total_avail = sum(avail[r] for r in CREW_CLASSES)
    ms = sum(sum(req_rf(int(row["MIN_CABIN_CREW"]), r)
                 for r in CREW_CLASSES)
             for _, row in flights.iterrows())
    print(f"\n  MS (with CCS)      = {ms}  (total crew needed across all flights)")
    print(f"  TA (total avail)   = {total_avail}")
    shortage = ms - total_avail
    if shortage > 0:
        print(f"  ⚠  Potential shortage of {shortage} crew-slots → CCS or extra hiring needed")
    else:
        print(f"  ✓  Sufficient crew available (surplus = {-shortage})")

    print()


if __name__ == "__main__":
    main()