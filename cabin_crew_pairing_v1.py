#!/usr/bin/env python3
"""
Cabin Crew Pairing Problem Solver
==================================
Implements TCCPP and MICCPP-ACCS from:
  Wen et al. (2022) – "Individual scheduling approach for multi-class airline
  cabin crew with manpower requirement heterogeneity"
  Transportation Research Part E, 163, 102763.

Key faithfulness changes vs. original code
-------------------------------------------
1.  PLANNING HORIZON  : Full 7-day week (paper Section 2.1 / 6.1.1).
2.  DUTY-BASED NETWORK: Flights are first grouped into duties (Section 2.2).
    A duty = briefing + sequence of flights connected by transits + debriefing.
    Pairings are sequences of duties linked by rests.
3.  REGULATIONS       : Max flights/duty, max duty period, min rest between
    duties, max TAFB per pairing — taken from Online Appendix 1 as described
    in Section 6.1.1.
4.  HOME BASE         : Single home base (JFK, replacing paper's HKG to
    match the available dataset); pairings must start and end there
    (Section 2.1).
5.  4 CREW CLASSES    : The Airways uses |R|=4 classes (Section 6.1.1).
    Per-class requirements b^r_i are heterogeneous across flights (Section 2.3).
6.  MM_r BENCHMARK    : Computed by solving MICCPP-A with all b^r_i = 1 and
    d_r = 0, as specified in Section 3.3 — not approximated as |F|.
7.  MS BENCHMARK      : Computed via dr-Zero-MICCPP-ACCS (Section 3.2).
8.  BOTH MODELS RUN   : TCCPP (Model 1) is re-enabled for comparison
    (Section 6.2).
9.  AVAIL LEVELS      : Three availability levels derived per paper Section
    6.1.2 (max MCr, min MCr, and one random level across instances).
10. SUBSTITUTION PENALTY μ and M_BIG set so that
    max_pairing_cost ≪ μ ≪ M_BIG, following Section 6.1.1 (μ=50 000,
    M=5 000 000 when TAFB is measured in minutes; we keep hours but scale
    accordingly).

Performance optimisations (vs. original)
-----------------------------------------
A.  build_duties: flights indexed by (ORIGIN, dep_min); bisect replaces
    linear scan for candidate next legs; early duty-period pruning.
B.  build_pairings_from_duties: rest-arc successor graph pre-computed once
    (O(D²) offline, sorted by departure) so each DFS step is O(out-degree)
    instead of O(|duties|). TAFB budget pruning cuts most branches early.
C.  MAX_PAIRINGS cap (default 500 K) prevents runaway enumeration on dense
    networks. Raise or set to None for full enumeration.

Data: flights_enriched.csv
  Key columns: ORIGIN, DEST, CRS_DEP_TIME, CRS_ARR_TIME,
               CRS_ELAPSED_TIME, MIN_CABIN_CREW, FL_DATE, CANCELLED, DIVERTED

If fewer than 10 real flights are found, a synthetic 7-day JFK-based
schedule is generated that mirrors the paper's test instances.
"""

from __future__ import annotations

import bisect
import warnings
from itertools import product as iproduct
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
import gurobipy as gp
from gurobipy import GRB

warnings.filterwarnings("ignore")
np.random.seed(42)

# ─────────────────────────────────────────────────────────────────────────────
# 1.  REGULATIONS  (Paper Online Appendix 1 / Section 6.1.1)
# ─────────────────────────────────────────────────────────────────────────────
BRIEF           = 60      # briefing before first departure (min)
DEBRIEF         = 30      # debriefing after last arrival   (min)
MIN_TRANSIT     = 45      # minimum transit between consecutive flights in duty (min)
MAX_DUTY_PERIOD = 13 * 60 # maximum duty period (min)  – CAD 371 typical limit
MAX_LEGS_DUTY   = 4       # maximum flight legs per duty
MIN_REST        = 10 * 60 # minimum rest between consecutive duties (min)
MAX_TAFB        = 7200    # maximum TAFB per pairing (min) – paper Section 6.1.1
MAX_DUTIES_PAI  = 5       # maximum duties per pairing (typical for week horizon)

# Penalty values: max TAFB cost ≪ μ ≪ M  (paper Section 6.1.1)
# With TAFB in minutes, max pairing cost ≤ 7200.
MU              = 50_000   # substitution penalty
M_BIG           = 5_000_000  # extra-crew big-M penalty

# Safety valve: stop collecting pairings once this many have been found.
# Raise or set to None to enumerate all (may be very slow for large networks).
MAX_PAIRINGS: int | None = 500_000

# ─────────────────────────────────────────────────────────────────────────────
# 2.  CREW CLASSES  (paper Section 6.1.1: The Airways uses |R| = 4 classes)
# ─────────────────────────────────────────────────────────────────────────────
CREW_CLASSES = [0, 1, 2, 3]   # Class 1–4 in the paper → 0-indexed here
CLASS_NAMES  = {0: "Class1", 1: "Class2", 2: "Class3", 3: "Class4"}

# Availability factor per class used to generate Level-1 / Level-2 / Level-3
# mirrors paper Table 8: Level1 = max MCr across instances,
# Level2 = min MCr, Level3 = random intermediate.
AVAIL_FACTOR_L1 = 1.20   # generous  → Scenario 8 (no shortage)
AVAIL_FACTOR_L2 = 0.90   # tight     → shortage scenarios
AVAIL_FACTOR_L3 = 1.05   # moderate  → CCS-only scenarios


# ─────────────────────────────────────────────────────────────────────────────
# 3.  HETEROGENEOUS PER-CLASS REQUIREMENTS  b^r_i  (Section 2.3)
# ─────────────────────────────────────────────────────────────────────────────
def req_rf(total_crew: int, r: int) -> int:
    """Heterogeneous per-class requirement b^r_i for a flight needing
    `total_crew` cabin crew total.  Produces different values per class
    and across flights — consistent with paper Section 2.3."""
    total_crew = max(total_crew, 4)   # minimum 4 so every class gets ≥ 1
    c0 = 1
    c1 = max(1, total_crew // 4)
    c2 = max(1, total_crew // 3)
    c3 = max(1, total_crew - c0 - c1 - c2)
    return [c0, c1, c2, c3][r]


# ─────────────────────────────────────────────────────────────────────────────
# 4.  FLIGHT DATA LOADING
# ─────────────────────────────────────────────────────────────────────────────
def hhmm_to_min(val):
    v = int(val)
    return (v // 100) * 60 + (v % 100)


def load_flights(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False)

    # ── Normalise CANCELLED / DIVERTED ────────────────────────────────────────
    # BTS stores these as float (0.00 / 1.00) and sometimes NaN.
    # Treat NaN as 0 (not cancelled/diverted) to avoid silently dropping rows.
    for col in ("CANCELLED", "DIVERTED"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        else:
            df[col] = 0   # column absent → treat all as operating

    # ── Drop cancelled / diverted and rows missing key fields ────────────────
    df = df[
        (df["CANCELLED"] < 0.5) &
        (df["DIVERTED"]  < 0.5) &
        df["CRS_DEP_TIME"].notna() &
        df["CRS_ARR_TIME"].notna() &
        df["ORIGIN"].notna() &
        df["DEST"].notna()
    ].copy()

    # ── MIN_CABIN_CREW: fill missing from SEATS_RESOLVED if available ─────────
    if "MIN_CABIN_CREW" not in df.columns:
        df["MIN_CABIN_CREW"] = 4
    else:
        df["MIN_CABIN_CREW"] = pd.to_numeric(df["MIN_CABIN_CREW"], errors="coerce")
        if "SEATS_RESOLVED" in df.columns:
            seats = pd.to_numeric(df["SEATS_RESOLVED"], errors="coerce")
            # FAA rough rule: 1 FA per 50 seats, minimum 4
            fallback = (seats / 50).apply(lambda x: max(4, int(x)) if pd.notna(x) else 4)
            df["MIN_CABIN_CREW"] = df["MIN_CABIN_CREW"].fillna(fallback)
        df["MIN_CABIN_CREW"] = df["MIN_CABIN_CREW"].fillna(4)

    # ── Parse times ───────────────────────────────────────────────────────────
    df["FL_DATE"]  = pd.to_datetime(df["FL_DATE"], infer_datetime_format=True)
    df["day_num"]  = (df["FL_DATE"] - df["FL_DATE"].min()).dt.days

    # CRS times: BTS uses integers like 659, 2200.  hhmm_to_min converts them
    # to minutes-since-midnight, then we add day offset for absolute minutes.
    df["dep_min"] = df["CRS_DEP_TIME"].apply(hhmm_to_min) + df["day_num"] * 1440
    df["arr_min"] = df["CRS_ARR_TIME"].apply(hhmm_to_min) + df["day_num"] * 1440

    # Overnight flights: scheduled arrival time-of-day < departure time-of-day
    # means the flight crosses midnight — add one day to arr_min.
    overnight = (df["CRS_ARR_TIME"].apply(hhmm_to_min) <
                 df["CRS_DEP_TIME"].apply(hhmm_to_min))
    df.loc[overnight, "arr_min"] += 1440

    df["MIN_CABIN_CREW"] = df["MIN_CABIN_CREW"].astype(int).clip(lower=4)
    df = df.reset_index(drop=True)
    df["fid"] = ["F%03d" % i for i in range(len(df))]

    print(f"  Loaded {len(df):,} operating flights "
          f"({df['ORIGIN'].nunique()} origins, {df['DEST'].nunique()} dests)")
    return df


# ─────────────────────────────────────────────────────────────────────────────
# 5.  SYNTHETIC 7-DAY SCHEDULE  (mirrors paper Section 6.1.1)
# ─────────────────────────────────────────────────────────────────────────────
def make_synthetic_flights() -> pd.DataFrame:
    """
    7-day JFK-based schedule inspired by paper Section 6.1.1.
    Aircraft types → total crew requirements that vary per flight,
    creating the manpower-requirement heterogeneity of Section 2.3.
    """
    HOME = "JFK"
    # (dest, dep_HHMM, elapsed_min, total_crew)
    # total_crew varies to mimic different aircraft types / layouts
    daily_template = [
        ("LAX", 700,  330, 8),   # large wide-body layout
        ("LAX", 1300, 330, 6),   # same route, smaller layout
        ("ORD", 600,  150, 5),   # medium-haul type
        ("ORD", 1400, 150, 7),   # medium-haul, different layout
        ("MIA", 700,  175, 4),   # short-haul type
        ("MIA", 1600, 175, 5),
        ("LHR", 2100, 420, 9),   # long-haul type
        ("CDG", 2000, 400, 8),
        ("BOS", 800,   75, 4),   # regional
        ("ATL", 900,  130, 5),
        # Return legs (arrive back at JFK)
        ("JFK", 600,  330, 8),   # returns from LAX
        ("JFK", 1200, 330, 6),
        ("JFK", 500,  150, 5),   # returns from ORD
        ("JFK", 1300, 150, 7),
        ("JFK", 900,  175, 4),   # returns from MIA
        ("JFK", 1800, 175, 5),
        ("JFK", 1400, 420, 9),   # returns from LHR
        ("JFK", 1500, 400, 8),
        ("JFK", 1000,  75, 4),   # returns from BOS
        ("JFK", 1400, 130, 5),   # returns from ATL
    ]
    rows = []
    fid_counter = 0
    for day in range(7):
        base_date = pd.Timestamp("2017-11-19") + pd.Timedelta(days=day)
        for (dest, dep_hhmm, elapsed, crew) in daily_template:
            if dest == "JFK":
                orig = {330: "LAX", 150: "ORD", 175: "MIA",
                        420: "LHR", 400: "CDG", 75: "BOS", 130: "ATL"}.get(elapsed, "LAX")
            else:
                orig = HOME
            dep_m = hhmm_to_min(dep_hhmm) + day * 1440
            arr_m = dep_m + elapsed
            actual_crew = crew + np.random.choice([-1, 0, 0, 1])
            actual_crew = max(actual_crew, 4)
            rows.append({
                "ORIGIN": orig, "DEST": dest,
                "CRS_DEP_TIME": dep_hhmm,
                "CRS_ARR_TIME": (arr_m % 1440 // 60) * 100 + (arr_m % 1440 % 60),
                "CRS_ELAPSED_TIME": elapsed,
                "MIN_CABIN_CREW": actual_crew,
                "FL_DATE": base_date,
                "CANCELLED": 0, "DIVERTED": 0,
                "day_num": day,
                "dep_min": dep_m,
                "arr_min": arr_m,
                "fid": "F%03d" % fid_counter,
            })
            fid_counter += 1
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────────────────────
# 6.  DUTY-BASED NETWORK  (Section 2.2)
#
#     OPTIMISED: flights indexed by (ORIGIN, dep_min); bisect for candidate
#     next legs; early duty-period pruning avoids redundant recursion.
# ─────────────────────────────────────────────────────────────────────────────
def build_duties(info: dict) -> list:
    """
    Enumerate all feasible duty sequences from `info` (fid→flight dict).
    A duty is a list of flight IDs in time order, connected by valid
    transits, satisfying MAX_LEGS_DUTY and MAX_DUTY_PERIOD.

    Optimisation: flights indexed by (ORIGIN, dep_min) so candidate next
    legs are found without scanning the entire flight list.
    """
    # Index: airport → sorted list of (dep_min, fid)
    by_origin: Dict[str, List[Tuple[int, str]]] = {}
    for fid, f in info.items():
        by_origin.setdefault(f["ORIGIN"], []).append((f["dep_min"], fid))
    for lst in by_origin.values():
        lst.sort()

    duties: list = []
    seen: set = set()

    def duty_period(legs: list) -> int:
        first = info[legs[0]]
        last  = info[legs[-1]]
        return last["arr_min"] + DEBRIEF - (first["dep_min"] - BRIEF)

    def extend(legs: list) -> None:
        dp = duty_period(legs)
        if dp > MAX_DUTY_PERIOD:
            return
        key = tuple(legs)
        if key not in seen:
            seen.add(key)
            duties.append({
                "flights":       key,
                "dep":           info[legs[0]]["dep_min"] - BRIEF,
                "arr":           info[legs[-1]]["arr_min"] + DEBRIEF,
                "airport_start": info[legs[0]]["ORIGIN"],
                "airport_end":   info[legs[-1]]["DEST"],
            })
        if len(legs) >= MAX_LEGS_DUTY:
            return

        last_f  = info[legs[-1]]
        min_dep = last_f["arr_min"] + MIN_TRANSIT
        candidates = by_origin.get(last_f["DEST"], [])
        # binary search: first candidate with dep_min >= min_dep
        idx = bisect.bisect_left(candidates, (min_dep,))
        legs_set = set(legs)
        first_dep_brief = info[legs[0]]["dep_min"] - BRIEF
        for dep_min, nfid in candidates[idx:]:
            if nfid in legs_set:
                continue
            # Prune: would adding this leg exceed MAX_DUTY_PERIOD?
            new_dp = info[nfid]["arr_min"] + DEBRIEF - first_dep_brief
            if new_dp > MAX_DUTY_PERIOD:
                break  # sorted by dep_min → later flights only worse
            extend(legs + [nfid])

    for fid in info:
        extend([fid])

    return duties


def build_pairings_from_duties(duties: list, home_base: str, info: dict) -> list:
    """
    Chain duties with valid rest arcs into legal pairings that start and
    end at home_base (Section 2.2).

    OPTIMISED: rest-arc successor graph pre-computed once (O(D²) offline,
    sorted by departure) so each DFS step is O(out-degree) instead of
    O(|duties|). TAFB budget pruning cuts most branches early.
    """
    n = len(duties)
    if n == 0:
        return []

    # ── Pre-build successor adjacency list ────────────────────────────────────
    # successors[i] = list of duty indices j such that:
    #   • duties[j].airport_start == duties[i].airport_end
    #   • duties[j].dep - duties[i].arr >= MIN_REST
    # Sorted by duties[j].dep (ascending) to allow TAFB budget early-exit.
    by_start: Dict[str, List[Tuple[int, int]]] = {}
    for i, d in enumerate(duties):
        by_start.setdefault(d["airport_start"], []).append((d["dep"], i))
    for lst in by_start.values():
        lst.sort()

    print(f"    Pre-computing rest-arc graph for {n:,} duties …", flush=True)
    successors: List[List[int]] = [[] for _ in range(n)]
    for i, d in enumerate(duties):
        min_dep = d["arr"] + MIN_REST
        candidates = by_start.get(d["airport_end"], [])
        idx = bisect.bisect_left(candidates, (min_dep,))
        successors[i] = [j for _, j in candidates[idx:] if j != i]

    # ── Index duties by starting airport ──────────────────────────────────────
    home_seeds = [i for i, d in enumerate(duties)
                  if d["airport_start"] == home_base]
    print(f"    Home-base seed duties: {len(home_seeds):,}", flush=True)

    pairings: list = []
    seen_keys: set = set()
    cap = MAX_PAIRINGS

    def collect_flights(duty_seq: list) -> tuple:
        all_f: list = []
        seen_f: set = set()
        for di in duty_seq:
            for f in duties[di]["flights"]:
                if f not in seen_f:
                    all_f.append(f)
                    seen_f.add(f)
        return tuple(all_f)

    def dfs(duty_seq: list, visited: frozenset) -> None:
        if cap is not None and len(pairings) >= cap:
            return
        cur       = duty_seq[-1]
        d         = duties[cur]
        first_dep = duties[duty_seq[0]]["dep"]
        tv        = d["arr"] - first_dep

        if tv > MAX_TAFB:
            return

        # Record pairing if back at home base
        if d["airport_end"] == home_base:
            key = tuple(duty_seq)
            if key not in seen_keys:
                seen_keys.add(key)
                pairings.append({
                    "duties":  key,
                    "flights": collect_flights(duty_seq),
                    "tafb":    tv,
                })

        if len(duty_seq) >= MAX_DUTIES_PAI:
            return

        for nxt in successors[cur]:
            if nxt in visited:
                continue
            # Earliest possible tafb if we extend to this duty
            min_new_tafb = duties[nxt]["arr"] - first_dep
            if min_new_tafb > MAX_TAFB:
                break  # successors sorted by dep → later ones only worse
            dfs(duty_seq + [nxt], visited | {nxt})

    for seed in home_seeds:
        if cap is not None and len(pairings) >= cap:
            print(f"    ⚠  Pairing cap ({cap:,}) reached — "
                  f"increase MAX_PAIRINGS for full enumeration")
            break
        dfs([seed], frozenset({seed}))

    # deduplicate (shouldn't be needed after seen_keys, but kept for safety)
    seen = set()
    unique = []
    for p in pairings:
        key = p["duties"]
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def build_pairing_index(flights: pd.DataFrame, home_base: str):
    """
    Full duty-based network construction (Section 2.2).
    Returns:
      pairings    : list of pairing IDs
      p_covers    : {pid → set of fids}
      p_tafb      : {pid → TAFB in minutes}
      pe_pairings : extra-crew pairing IDs (one per flight, deadhead logic)
      pe_covers   : {pid → {fid}}
      pe_tafb     : {pid → TAFB in minutes}
    """
    info = flights.set_index("fid")[
        ["ORIGIN", "DEST", "dep_min", "arr_min"]
    ].to_dict("index")

    print("  Building duties …")
    duties = build_duties(info)
    print(f"  Duties enumerated  : {len(duties):,}")

    print("  Chaining duties into pairings …")
    raw_pairings = build_pairings_from_duties(duties, home_base, info)
    print(f"  Raw pairings found : {len(raw_pairings):,}")

    pairings, p_covers, p_tafb = [], {}, {}
    for i, p in enumerate(raw_pairings):
        pid = "P_%04d" % i
        pairings.append(pid)
        p_covers[pid] = set(p["flights"])
        p_tafb[pid]   = p["tafb"]

    # Extra-crew pairings: one per flight (positioned externally, big-M penalty).
    # TAFB = briefing + flight duration + debriefing  (Section 2.1 definition).
    pe_pairings, pe_covers, pe_tafb = [], {}, {}
    for _, row in flights.iterrows():
        pid = "PE_" + row["fid"]
        pe_pairings.append(pid)
        pe_covers[pid] = {row["fid"]}
        pe_tafb[pid]   = BRIEF + (row["arr_min"] - row["dep_min"]) + DEBRIEF

    return pairings, p_covers, p_tafb, pe_pairings, pe_covers, pe_tafb


# ─────────────────────────────────────────────────────────────────────────────
# 7.  MODEL 1 – TCCPP  (Section 3.1, Equations 3–5)
# ─────────────────────────────────────────────────────────────────────────────
def solve_tccpp(flights, pairings, p_covers, p_tafb):
    """
    (TCCPP)  min  Σ_{jt} c_{jt} · x_{jt}                     (Eq. 3)
    s.t.     Σ_{jt} a_{i,jt} · x_{jt} ≥ 1,  ∀i ∈ F          (Eq. 4)
             x_{jt} ∈ {0,1}                                   (Eq. 5)

    Cabin crew modelled as homogeneous teams; no class distinction.
    Cost = TAFB in minutes (paper Section 2.1).
    """
    F = list(flights["fid"])
    T = pairings

    m = gp.Model("TCCPP")
    m.setParam("OutputFlag", 0)
    m.setParam("TimeLimit", 120)

    select = m.addVars(T, vtype=GRB.BINARY, name="sel")

    m.setObjective(gp.quicksum(p_tafb[t] * select[t] for t in T), GRB.MINIMIZE)

    for f in F:
        covering = [t for t in T if f in p_covers[t]]
        if covering:
            m.addConstr(
                gp.quicksum(select[t] for t in covering) >= 1,
                name=f"cov_{f}"
            )

    m.optimize()
    return m, select


# ─────────────────────────────────────────────────────────────────────────────
# 8.  MODEL 2 – MICCPP-ACCS  (Section 3.2, Equations 6–13)
# ─────────────────────────────────────────────────────────────────────────────
def solve_miccpp_accs(flights, pairings, p_covers, p_tafb,
                      pe_pairings, pe_covers, pe_tafb, avail):
    """
    (MICCPP-ACCS)
    min  Σ_r Σ_{jr} c_{jr} · x_{jr}                           available-crew TAFB  (Eq. 6 part i)
       + Σ_r Σ_i   μ · s^r_i                                  substitution penalty (Eq. 6 part ii)
       + Σ_r Σ_{jr^e} (c_{jr^e} + M) · x_{jr^e}              extra-crew penalty   (Eq. 6 part iii)

    s.t.
      (7) Σ_r [Σ_{jr} a_{i,jr} x_{jr} + Σ_{jr^e} a_{i,jr^e} x_{jr^e}] ≥ Σ_r b^r_i  ∀i
          (total-satisfaction / CCS enabler)
      (8) Σ_{jr} a_{i,jr} x_{jr} + Σ_{jr^e} a_{i,jr^e} x_{jr^e} ≥ 1  ∀i,r
          (minimum-satisfaction: ≥1 qualified crew from each class)
      (9) Σ_{jr} a_{i,jr} x_{jr} + Σ_{jr^e} a_{i,jr^e} x_{jr^e} + s^r_i ≥ b^r_i  ∀i,r
          (substitution recording)
     (10) Σ_{jr} x_{jr} ≤ d_r  ∀r
          (crew availability)
     (11-13) integrality
    """
    F   = list(flights["fid"])
    R   = CREW_CLASSES

    # Build b^r_i: heterogeneous per-class requirements
    req = {}
    for _, row in flights.iterrows():
        for r in R:
            req[(r, row["fid"])] = req_rf(int(row["MIN_CABIN_CREW"]), r)

    m = gp.Model("MICCPP_ACCS")
    m.setParam("OutputFlag", 0)
    m.setParam("TimeLimit", 300)

    # Decision variables (Eqs. 11–13): non-negative integers
    assign = m.addVars([(r, p) for r in R for p in pairings],
                       vtype=GRB.INTEGER, lb=0, name="asgn")
    extra  = m.addVars([(r, p) for r in R for p in pe_pairings],
                       vtype=GRB.INTEGER, lb=0, name="xtra")
    sub    = m.addVars([(r, f) for r in R for f in F],
                       vtype=GRB.INTEGER, lb=0, name="sub")

    # Objective (Eq. 6)
    avail_cost = gp.quicksum(p_tafb[p]  * assign[r, p]
                             for r in R for p in pairings)
    sub_cost   = gp.quicksum(MU         * sub[r, f]
                             for r in R for f in F)
    extra_cost = gp.quicksum((pe_tafb[p] + M_BIG) * extra[r, p]
                             for r in R for p in pe_pairings)
    m.setObjective(avail_cost + sub_cost + extra_cost, GRB.MINIMIZE)

    for f in F:
        total_req_f = sum(req[(r, f)] for r in R)

        # Eq. 7 – total satisfaction (CCS enabler)
        m.addConstr(
            gp.quicksum(assign[r, p] for r in R for p in pairings
                        if f in p_covers[p])
          + gp.quicksum(extra[r, p]  for r in R for p in pe_pairings
                        if f in pe_covers[p])
          >= total_req_f,
            name=f"total_sat_{f}"
        )

        for r in R:
            av_cov = [p for p in pairings    if f in p_covers[p]]
            ex_cov = [p for p in pe_pairings if f in pe_covers[p]]
            crew_on_f = (
                gp.quicksum(assign[r, p] for p in av_cov)
              + gp.quicksum(extra[r, p]  for p in ex_cov)
            )
            # Eq. 8 – minimum satisfaction (≥1 per class per flight)
            m.addConstr(crew_on_f >= 1,
                        name=f"min_sat_{r}_{f}")
            # Eq. 9 – substitution recording
            m.addConstr(crew_on_f + sub[r, f] >= req[(r, f)],
                        name=f"sub_rec_{r}_{f}")

    # Eq. 10 – crew availability
    for r in R:
        m.addConstr(
            gp.quicksum(assign[r, p] for p in pairings) <= avail[r],
            name=f"avail_{r}"
        )

    m.optimize()
    return m, assign, extra, sub, req


# ─────────────────────────────────────────────────────────────────────────────
# 9.  BENCHMARK COMPUTATIONS  (Section 3.2–3.3, Table 2)
#
#   MS  : solve dr-Zero-MICCPP-ACCS (all d_r = 0) → Σ extra crew used
#   MCr : solve MICCPP-A with d_r = 0 per class   → extra crew for class r
#   MMr : solve MICCPP-A with d_r = 0, b^r_i = 1  → extra crew for class r
# ─────────────────────────────────────────────────────────────────────────────
def compute_benchmarks(flights, pairings, p_covers, p_tafb,
                       pe_pairings, pe_covers, pe_tafb):
    """
    Returns MS, MC (dict r→value), MM (dict r→value).
    All solved as MIPs with d_r = 0 (only extra-crew variables active).
    """
    F = list(flights["fid"])
    R = CREW_CLASSES

    req = {}
    for _, row in flights.iterrows():
        for r in R:
            req[(r, row["fid"])] = req_rf(int(row["MIN_CABIN_CREW"]), r)

    # ── Helper: MICCPP-A with d_r = 0 (paper Section 3.3) ───────────────────
    def miccpp_a_zero(req_override=None):
        """
        Solve MICCPP-A for each class r independently with d_r=0.
        req_override: if given, a dict (r,f)→value replacing req.
        Returns dict r → number of extra crew used.
        """
        use_req = req_override if req_override is not None else req
        mc = {}
        for r in R:
            m = gp.Model(f"MICCPP_A_r{r}")
            m.setParam("OutputFlag", 0)
            m.setParam("TimeLimit", 60)
            xtra = m.addVars(pe_pairings, vtype=GRB.INTEGER, lb=0, name="xtra")
            m.setObjective(
                gp.quicksum((pe_tafb[p] + M_BIG) * xtra[p] for p in pe_pairings),
                GRB.MINIMIZE
            )
            for f in F:
                ex_cov = [p for p in pe_pairings if f in pe_covers[p]]
                # Eq. 15: each class r covers flight f by at least b^r_i
                m.addConstr(
                    gp.quicksum(xtra[p] for p in ex_cov) >= use_req[(r, f)],
                    name=f"cov_{r}_{f}"
                )
            m.optimize()
            mc[r] = int(round(sum(xtra[p].X for p in pe_pairings))) if m.SolCount else 0
        return mc

    # ── MS: dr-Zero-MICCPP-ACCS (Section 3.2) ───────────────────────────────
    def dr_zero_miccpp_accs():
        """All d_r = 0 → only extra-crew variables. Returns total extra crew."""
        m = gp.Model("drZero_MICCPP_ACCS")
        m.setParam("OutputFlag", 0)
        m.setParam("TimeLimit", 60)
        extra  = m.addVars([(r, p) for r in R for p in pe_pairings],
                           vtype=GRB.INTEGER, lb=0, name="xtra")
        sub    = m.addVars([(r, f) for r in R for f in F],
                           vtype=GRB.INTEGER, lb=0, name="sub")
        m.setObjective(
            gp.quicksum((pe_tafb[p] + M_BIG) * extra[r, p]
                        for r in R for p in pe_pairings)
          + gp.quicksum(MU * sub[r, f] for r in R for f in F),
            GRB.MINIMIZE
        )
        for f in F:
            total_req_f = sum(req[(r, f)] for r in R)
            m.addConstr(
                gp.quicksum(extra[r, p] for r in R for p in pe_pairings
                            if f in pe_covers[p])
                >= total_req_f,
                name=f"total_sat_{f}"
            )
            for r in R:
                ex_cov = [p for p in pe_pairings if f in pe_covers[p]]
                crew_f = gp.quicksum(extra[r, p] for p in ex_cov)
                m.addConstr(crew_f >= 1,           name=f"min_sat_{r}_{f}")
                m.addConstr(crew_f + sub[r, f] >= req[(r, f)],
                            name=f"sub_rec_{r}_{f}")
        m.optimize()
        if m.SolCount:
            return int(round(sum(extra[r, p].X
                                 for r in R for p in pe_pairings)))
        return None

    print("  Computing MS  (dr-Zero-MICCPP-ACCS) …")
    MS = dr_zero_miccpp_accs()

    print("  Computing MCr (MICCPP-A, d_r=0, real b^r_i) …")
    MC = miccpp_a_zero()

    print("  Computing MMr (MICCPP-A, d_r=0, b^r_i=1) …")
    req_ones = {(r, f): 1 for r in R for f in F}
    MM = miccpp_a_zero(req_override=req_ones)

    return MS, MC, MM


# ─────────────────────────────────────────────────────────────────────────────
# 10. AVAILABILITY LEVELS  (Section 6.1.2, Table 8)
# ─────────────────────────────────────────────────────────────────────────────
def derive_availability_levels(MC, MM):
    """
    Returns three availability dicts {r: d_r} following paper Section 6.1.2.
    """
    L1 = {r: int(MC[r] * AVAIL_FACTOR_L1) for r in CREW_CLASSES}   # generous
    L2 = {r: int(MC[r] * AVAIL_FACTOR_L2) for r in CREW_CLASSES}   # tight
    # Level 3: between MM_r and MC_r per class, varied to trigger Scenarios 5/7
    L3 = {r: int(MM[r] + np.random.uniform(0.0, 0.5) * (MC[r] - MM[r]))
           for r in CREW_CLASSES}
    L3 = {r: max(L3[r], MM[r]) for r in CREW_CLASSES}   # must cover MM
    return L1, L2, L3


# ─────────────────────────────────────────────────────────────────────────────
# 11. SCENARIO CLASSIFICATION  (Section 4, Table 3)
# ─────────────────────────────────────────────────────────────────────────────
def classify_scenario(avail, MS, MC, MM):
    """
    Classify into one of Scenarios 1–8 from Table 3.
    avail: {r: d_r}, MS: int, MC: {r:int}, MM: {r:int}
    """
    R      = CREW_CLASSES
    TA     = sum(avail[r] for r in R)
    sum_MC = sum(MC[r] for r in R)

    if TA < MS:
        R1 = [r for r in R if avail[r] > MC[r]]
        R2 = [r for r in R if r not in R1 and avail[r] < MC[r]]
        if R1:
            R3 = [r for r in R2 if MM[r] < MC[r]]
            return 2 if R3 else 1
        else:
            return 3 if MS == sum_MC else 4
    else:
        R4 = [r for r in R if avail[r] < MC[r]]
        if R4:
            short_below_MM = [r for r in R4 if avail[r] < MM[r]]
            if not short_below_MM:
                return 5
            R5 = [r for r in R4 if MM[r] < MC[r]]
            return 7 if R5 else 6
        else:
            return 8


# ─────────────────────────────────────────────────────────────────────────────
# 12. REPORTING
# ─────────────────────────────────────────────────────────────────────────────
_STATUS = {
    GRB.OPTIMAL:     "Optimal",
    GRB.INFEASIBLE:  "Infeasible",
    GRB.INF_OR_UNBD: "Infeasible or Unbounded",
    GRB.TIME_LIMIT:  "Time limit (best solution shown)",
    GRB.SUBOPTIMAL:  "Sub-optimal",
}


def section(title):
    print("\n" + "═" * 65)
    print(f"  {title}")
    print("═" * 65)


def report_tccpp(m, select, pairings, p_covers, p_tafb, flights):
    section("MODEL 1 : TCCPP  (Traditional Homogeneous Team Pairings)")
    F   = set(flights["fid"])
    sts = _STATUS.get(m.Status, f"Code {m.Status}")
    print(f"  Solver status      : {sts}")
    if m.Status not in (GRB.OPTIMAL, GRB.TIME_LIMIT, GRB.SUBOPTIMAL):
        return
    if m.SolCount == 0:
        print("  (No solution found)")
        return

    sel     = [t for t in pairings if select[t].X > 0.5]
    covered = set()
    for t in sel:
        covered |= p_covers[t]

    total_tafb = sum(p_tafb[t] for t in sel)
    print(f"  MIP gap            : {m.MIPGap*100:.2f}%")
    print(f"  Total pairings pool: {len(pairings)}")
    print(f"  Selected pairings  : {len(sel)}")
    print(f"  Total TAFB (min)   : {total_tafb:.0f}")
    print(f"  Flights covered    : {len(covered & F)}/{len(F)}")
    print(f"  Uncovered flights  : {sorted(F - covered) or 'None'}")

    if sel:
        print(f"\n  {'Pairing ID':<15} {'Legs':>5}  {'TAFB (min)':>10}")
        print("  " + "-" * 35)
        for t in sorted(sel, key=lambda x: -p_tafb[x]):
            print(f"  {t:<15} {len(p_covers[t]):>5}  {p_tafb[t]:>10.0f}")

def report_miccpp(m, assign, extra, sub, pairings, p_covers, p_tafb,
                  pe_pairings, pe_covers, pe_tafb, flights, avail, req):
    section("MODEL 2 : MICCPP-ACCS  (Multi-Class Individual + CCS)")
    F   = list(flights["fid"])
    R   = CREW_CLASSES
    sts = _STATUS.get(m.Status, f"Code {m.Status}")
    print(f"  Solver status      : {sts}")
    if m.Status not in (GRB.OPTIMAL, GRB.TIME_LIMIT, GRB.SUBOPTIMAL):
        return
    if m.SolCount == 0:
        print("  (No solution found)")
        return

    def X(v): return v.X

    ac = sum(p_tafb[p]  * X(assign[r, p]) for r in R for p in pairings)
    sc = sum(MU         * X(sub[r, f])    for r in R for f in F)
    ec = sum((pe_tafb[p] + M_BIG) * X(extra[r, p])
             for r in R for p in pe_pairings)

    print(f"  MIP gap            : {m.MIPGap*100:.2f}%")
    print(f"\n  ── Objective breakdown ──────────────────────────────")
    print(f"  Available-crew TAFB cost : {ac:>14.0f} min")
    print(f"  Substitution penalty     : {sc:>14.0f}  (μ={MU:,.0f})")
    print(f"  Extra-crew penalty       : {ec:>14.0f}  (M={M_BIG:,.0f})")
    print(f"  Total objective          : {ac+sc+ec:>14.0f}")

    print(f"\n  ── Workforce summary ─────────────────────────────────")
    print(f"  {'Class':<10} {'Cap':>6} {'Used':>6} {'Extra':>6} {'Subs':>6}")
    print("  " + "-" * 40)
    for r in R:
        used  = sum(X(assign[r, p]) for p in pairings)
        xused = sum(X(extra[r, p])  for p in pe_pairings)
        nsub  = sum(X(sub[r, f])    for f in F)
        print(f"  {CLASS_NAMES[r]:<10} {avail[r]:>6d} {used:>6.0f} {xused:>6.0f} {nsub:>6.0f}")

    ta  = sum(X(assign[r, p]) for r in R for p in pairings)
    ts  = sum(X(sub[r, f])    for r in R for f in F)
    te  = sum(X(extra[r, p])  for r in R for p in pe_pairings)
    print(f"\n  Total available-crew assignments : {ta:.0f}")
    print(f"  Total CCS substitutions          : {ts:.0f}")
    print(f"  Total extra-crew assignments     : {te:.0f}")

    # Show flights where CCS occurred
    spf = {f: sum(X(sub[r, f]) for r in R) for f in F}
    nonzero = sorted([(f, s) for f, s in spf.items() if s > 0],
                     key=lambda x: -x[1])
    if nonzero:
        fmap = flights.set_index("fid")[["ORIGIN", "DEST"]].to_dict("index")
        print(f"\n  ── CCS events (flights with substitutions) ──────────")
        print(f"  {'Flight':>6}  {'Subs':>5}  Route")
        print("  " + "-" * 30)
        for f, s in nonzero:
            print(f"  {f:>6}  {s:>5.0f}  "
                  f"{fmap[f]['ORIGIN']}→{fmap[f]['DEST']}")
    else:
        print("\n  No CCS substitutions required.")


def report_benchmarks(MS, MC, MM, avail_levels, flights):
    section("MANPOWER BENCHMARKS  (Table 2, Section 3.2–3.3)")
    R = CREW_CLASSES
    sum_MC = sum(MC[r] for r in R)
    print(f"  {'Benchmark':<6}  {'Class':<8}  {'Value':>6}  Description")
    print("  " + "-" * 55)
    for r in R:
        print(f"  {'MCr':<6}  {CLASS_NAMES[r]:<8}  {MC[r]:>6d}  "
              f"Min demand class r (no CCS)")
        print(f"  {'MMr':<6}  {CLASS_NAMES[r]:<8}  {MM[r]:>6d}  "
              f"Min satisfaction (1 per flight)")
    print(f"\n  MS  (with CCS, all classes) = {MS}")
    print(f"  ΣMCr (no CCS, all classes)  = {sum_MC}")
    if MS < sum_MC:
        print(f"  → CCS saves {sum_MC - MS} crew-slots vs. no-substitution baseline")
    else:
        print(f"  → CCS offers no saving for this schedule (MS = ΣMCr)")

    section("AVAILABILITY-REQUIREMENT SCENARIOS  (Table 3, Section 4)")
    level_names = ["Level 1 (generous)", "Level 2 (tight)", "Level 3 (moderate)"]
    for lname, avail in zip(level_names, avail_levels):
        TA   = sum(avail[r] for r in R)
        scen = classify_scenario(avail, MS, MC, MM)
        print(f"\n  {lname}")
        print(f"    Availability : { {CLASS_NAMES[r]: avail[r] for r in R} }")
        print(f"    TA={TA}, MS={MS}, ΣMCr={sum_MC}")
        print(f"    → Scenario {scen}  ", end="")
        desc = {
            1: "(TA<MS, some r>MCr, all short have MM=MC → extra only)",
            2: "(TA<MS, some r>MCr, some short have MM<MC → CCS + extra)",
            3: "(TA<MS, all r≤MCr, MS=ΣMCr → extra only, no CCS benefit)",
            4: "(TA<MS, all r≤MCr, MS<ΣMCr → CCS + extra)",
            5: "(TA≥MS, some r<MCr, all short have d_r≥MM → CCS only)",
            6: "(TA≥MS, some r<MCr, some d_r<MM, all short MM=MC → extra only)",
            7: "(TA≥MS, some r<MCr, some d_r<MM, some MM<MC → CCS + extra)",
            8: "(TA≥MS, all r≥MCr → no shortage, no CCS needed)",
        }
        print(desc.get(scen, ""))


# ─────────────────────────────────────────────────────────────────────────────
# 13. MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    CSV_PATH  = "data/flights_enriched.csv"
    HOME_BASE = "JFK"    # paper uses HKG; replaced with JFK to match available dataset

    # ── Load data ────────────────────────────────────────────────────────────
    print("Loading flight data …")
    try:
        flights = load_flights(CSV_PATH)
    except Exception:
        flights = pd.DataFrame()

    if len(flights) < 10:
        print(f"  Real data insufficient ({len(flights)} flights). "
              f"Generating synthetic 7-day JFK schedule …\n")
        flights = make_synthetic_flights()
        HOME_BASE = "JFK"
    else:
        # Paper: weekly planning horizon (Section 2.1).
        # Restrict to first 7 days in the dataset.
        day0 = flights["day_num"].min()
        flights = flights[flights["day_num"] <= day0 + 6].reset_index(drop=True)
        flights["fid"] = ["F%03d" % i for i in range(len(flights))]

    print(f"  Planning horizon   : 7 days   ({len(flights)} flights)")
    print(f"  Home base          : {HOME_BASE}")
    print(f"  Crew classes       : {len(CREW_CLASSES)} (|R|={len(CREW_CLASSES)})")

    # Restrict to flights reachable from home base (paper preprocessing step)
    # flights = flights[
    #     (flights["ORIGIN"] == HOME_BASE) | (flights["DEST"] == HOME_BASE)
    # ].reset_index(drop=True)
    flights["fid"] = ["F%03d" % i for i in range(len(flights))]
    print(f"  Flights on JFK route: {len(flights)}")

    # ── Duty-based network & pairing generation ───────────────────────────────
    print("\nBuilding duty-based network & pairings (Section 2.2) …")
    pairings, p_covers, p_tafb, pe_pairings, pe_covers, pe_tafb = \
        build_pairing_index(flights, HOME_BASE)
    print(f"  Available-crew pairings : {len(pairings)}")
    print(f"  Extra-crew pairings     : {len(pe_pairings)}")

    F = list(flights["fid"])
    coverable = {f for p in pairings for f in p_covers[p]}
    print(f"  Flights coverable       : {len(coverable)}/{len(F)}")
    if set(F) - coverable:
        print(f"  Uncoverable (need extra): {sorted(set(F) - coverable)}")

    # ── Manpower benchmarks (Table 2) ─────────────────────────────────────────
    print("\nComputing manpower benchmarks (Table 2) …")
    MS, MC, MM = compute_benchmarks(
        flights, pairings, p_covers, p_tafb,
        pe_pairings, pe_covers, pe_tafb
    )

    # ── Availability levels (Section 6.1.2) ──────────────────────────────────
    L1, L2, L3 = derive_availability_levels(MC, MM)
    print(f"\n  Availability Level 1 : { {CLASS_NAMES[r]: L1[r] for r in CREW_CLASSES} }")
    print(f"  Availability Level 2 : { {CLASS_NAMES[r]: L2[r] for r in CREW_CLASSES} }")
    print(f"  Availability Level 3 : { {CLASS_NAMES[r]: L3[r] for r in CREW_CLASSES} }")

    # ── MODEL 1 – TCCPP (re-enabled for comparison per Section 6.2) ──────────
    print("\nSolving Model 1: TCCPP …")
    prob1, select = solve_tccpp(flights, pairings, p_covers, p_tafb)
    report_tccpp(prob1, select, pairings, p_covers, p_tafb, flights)

    # ── MODEL 2 – MICCPP-ACCS under each availability level ──────────────────
    for level_name, avail in [("Level 1", L1), ("Level 2", L2), ("Level 3", L3)]:
        print(f"\nSolving Model 2: MICCPP-ACCS ({level_name}) …")
        prob2, assign, extra, sub, req = solve_miccpp_accs(
            flights, pairings, p_covers, p_tafb,
            pe_pairings, pe_covers, pe_tafb, avail
        )
        report_miccpp(prob2, assign, extra, sub,
                      pairings, p_covers, p_tafb,
                      pe_pairings, pe_covers, pe_tafb,
                      flights, avail, req)

    # ── Benchmarks + scenario report ─────────────────────────────────────────
    report_benchmarks(MS, MC, MM, [L1, L2, L3], flights)
    print()


if __name__ == "__main__":
    main()