"""
Cabin Crew Pairing via Dynamic Discretisation Discovery (DDD)
=============================================================
Implements the formulation from slides_copy_2.pdf exactly.

Key additions vs original:
  - Per-airline planning: flights split by OP_CARRIER; solver runs independently
    for each airline (separate crew pools, networks, and models).
  - Rolling horizon: windows W of length T_days=7 with T_commit=3 committed days
    and T_tail=7 return tail; carry-over of crew positions and clock states.
  - Reachability pruning: Fwd/Bwd Dijkstra from depot/horizon, arcs violating
    d_work or d_away excluded.
  - Turnaround snapping: arrival end of flight arc snapped to earliest node at
    dest satisfying the MIN_TURNAROUND gap (slide 17).
  - Home-break clocks: per-crew state (t_reset, d_work, h_home) carried forward
    across windows (slide 23).
  - Satellite airports S ⊆ A \ B: airports with ≤250 nm average distance and
    ≥3 daily flights (used for reachability but not base-sized separately).
  - Cost constants aligned with slide 10 exactly.

Sets & notation (slide 8–12):
  A      All airports
  B ⊆ A  Crew home bases
  S ⊆ A\B Satellite airports
  C      Crew members
  C_b    Crew at base b
  F      All flights
  F_cov  Flights requiring coverage (planning period only)
  W      Ordered solve windows (rolling horizon)

Parameters (slides 9–11):
  δ_f    Flight duration (minutes)
  dist_f Great-circle distance
  l_f    Passenger load factor ∈ [0,1]
  r_f    Required working crew count
  s_b    Crew supply at base b
  s_min=5, s_max=40
  c_fl   = 100 / min  (flight time worked)
  c_dh   = 20 / min   (deadhead base per-diem)
  c_wt   = 0.5 / min  (wait cost rate)
  c_ov   = 500        (overnight flat penalty, wait ≥ 4 h)
  c_unc  = 107        (penalty per uncovered slot)
  Δ_ta   = 45 min     (minimum turnaround)
  Δ_rest = 8 h        (minimum rest before next duty)
  Δ_duty = 14 h       (maximum on-duty time per day)
  Δ_hb   = 48 h       (minimum home break)
  d_work = 5 days     (max consecutive duty days)
  d_away = 7 days     (return window)
  T_days = 7 days     (solve window length)
  T_commit = 3 days   (committed per step)
  T_tail = 7 days     (return tail)
  Δ_bucket = 15 min   (initial DDD time-bucket)
"""

from __future__ import annotations
import csv
import math
import heapq
import random
import json
import os
import pickle
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import gurobipy as gp
from gurobipy import GRB

# ─────────────────────────────────────────────────────────────────────────────
# PARAMETERS  (aligned with slides 9–11)
# ─────────────────────────────────────────────────────────────────────────────

# Rolling horizon (slide 20)
T_DAYS_SOLVE   = 7    # solve window length in days
T_DAYS_COMMIT  = 3    # days committed per step
T_DAYS_TAIL    = 7    # return tail days
T_LOOKAHEAD    = T_DAYS_SOLVE - T_DAYS_COMMIT  # = 4 days overlap

# Crew limits (slide 9)
S_MIN = 5             # minimum crew at any base
S_MAX = 40            # maximum crew (introduced by noise)
RANDOM_SEED = 42

# Cost parameters (slide 10)
C_FL  = 100.0         # per minute of flight time worked
C_DH  = 20.0          # per minute of deadhead (base per-diem + opp-cost)
C_WT  = 0.5           # per minute of wait
C_OV  = 500.0         # flat penalty per overnight stay (wait ≥ 4 h)
C_UNC = 107.0         # penalty per uncovered crew slot (slide 10 raw value)

# Duty / rest limits (slide 11)
DELTA_TA    = 45      # min turnaround between flights at same airport (min)
DELTA_REST  = 8 * 60  # min rest before next duty (min) = 480
DELTA_DUTY  = 14 * 60 # max on-duty time per day (min) = 840
DELTA_HB    = 48 * 60 # min home break (min) = 2880
D_WORK      = 5       # max consecutive duty days
D_AWAY      = 7       # return window in days
DELTA_AWAY  = D_AWAY * 1440  # in minutes


C_UNC_EFFECTIVE = 10**8

# Satellite airport thresholds
SAT_MAX_DIST   = 250.0  # nm; airports with avg great-circle ≤ this
SAT_MIN_FLIGHTS = 3     # minimum daily flights

# DDD solver (slide 11)
DELTA_BUCKET = 15     # initial time-bucket (min)
DDD_MAX_VIOLATIONS = 500
DDD_MAX_ITER = 200

# Opportunity cost model
FARE_BASE      = 50.0
FARE_PER_MILE  = 0.15
LF_LOW  = 0.75
LF_HIGH = 0.90

DEPOT_START = 0
LARGE = int(1e9)

# Overnight threshold (4 h = 240 min)
OVERNIGHT_THRESHOLD = 4 * 60


# ─────────────────────────────────────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Flight:
    id: int
    origin: str
    dest: str
    dep_min: int       # minutes from week start
    arr_min: int
    duration: int
    min_crew: int      # r_f
    flight_num: str
    distance: float = 0.0
    load_factor: float = 0.82
    airline: str = ""


@dataclass(frozen=True)
class CrewMember:
    id: int
    base: str


@dataclass(frozen=True)
class Node:
    airport: str
    time: int

    def __lt__(self, other):
        return (self.time, self.airport) < (other.time, other.airport)


@dataclass
class Arc:
    id: int
    start: Node
    end: Node
    true_end: int     # actual arrival time before snapping
    cost: float
    arc_type: str     # 'flight' | 'deadhead' | 'wait'
    flight_id: Optional[int] = None

    @property
    def is_wait(self):
        return self.arc_type == 'wait'

    def __hash__(self):
        return self.id

    def __eq__(self, other):
        return isinstance(other, Arc) and self.id == other.id


# Clock state per crew (slide 23)
@dataclass
class ClockState:
    t_reset: int   = 0      # last reset time (min from week start)
    d_work: int    = 0      # consecutive work-days since last rest
    h_home: int    = 0      # home-wait accumulated since last departure (min)


# ─────────────────────────────────────────────────────────────────────────────
# CSV PARSING  (now carries airline column)
# ─────────────────────────────────────────────────────────────────────────────

def parse_hhmm(s: str) -> int:
    s = s.strip().zfill(4)
    return int(s[:2]) * 60 + int(s[2:])


def parse_flights_by_airline(
    filepath: str,
    days: int,
    horizon_days: Optional[int] = None,
) -> tuple[dict[str, list[Flight]], datetime]:
    """
    Load flights grouped by operating carrier (OP_CARRIER).

    Returns:
        flights_by_airline : {airline_code: [Flight, ...]}
        week_start         : datetime of the first flight date
    """
    if horizon_days is None:
        horizon_days = days

    date_fmt_options = ["%m/%d/%Y %I:%M:%S %p", "%Y-%m-%d", "%m/%d/%Y"]
    week_start = None
    fid = 0
    flights_by_airline: dict[str, list[Flight]] = defaultdict(list)

    with open(filepath, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                if float(row.get('CANCELLED', 0)) >= 1.0:
                    continue
            except ValueError:
                continue

            fl_date_str = row['FL_DATE'].strip()
            fl_date = None
            for fmt in date_fmt_options:
                try:
                    fl_date = datetime.strptime(fl_date_str, fmt).date()
                    break
                except ValueError:
                    continue
            if fl_date is None:
                continue

            if week_start is None:
                week_start = datetime(fl_date.year, fl_date.month, fl_date.day)

            day_offset = (fl_date - week_start.date()).days
            if day_offset < 0 or day_offset >= horizon_days:
                continue

            try:
                dep_hhmm = row['CRS_DEP_TIME'].strip()
                arr_hhmm = row['CRS_ARR_TIME'].strip()
                elapsed  = float(row['CRS_ELAPSED_TIME'].strip())
                min_crew = int(float(row['MIN_CABIN_CREW'].strip()))
            except (ValueError, KeyError):
                continue

            if not dep_hhmm or not arr_hhmm or elapsed <= 0:
                continue

            dep_min_day = parse_hhmm(dep_hhmm)
            arr_min_day = parse_hhmm(arr_hhmm)
            dep_min = day_offset * 1440 + dep_min_day

            if arr_min_day < dep_min_day:
                arr_min = dep_min + int(elapsed)
            else:
                arr_min = day_offset * 1440 + arr_min_day

            if arr_min <= dep_min:
                arr_min = dep_min + max(1, int(elapsed))

            try:
                lf_raw = row.get('LOAD_FACTOR', '').strip()
                load_factor = float(lf_raw) if lf_raw else 0.82
            except ValueError:
                load_factor = 0.82

            airline = row.get('OP_CARRIER', '').strip()

            flight = Flight(
                id=fid,
                origin=row['ORIGIN'].strip(),
                dest=row['DEST'].strip(),
                dep_min=dep_min,
                arr_min=arr_min,
                duration=arr_min - dep_min,
                min_crew=max(1, min_crew),
                flight_num=row.get('OP_CARRIER_FL_NUM', str(fid)).strip(),
                distance=float(row['DISTANCE'].strip()) if row.get('DISTANCE', '').strip() else 0.0,
                load_factor=load_factor,
                airline=airline,
            )
            object.__setattr__(flight, 'needs_coverage', day_offset < days)
            flights_by_airline[airline].append(flight)
            fid += 1

    if week_start is None:
        raise ValueError("No valid flights found in CSV.")

    for al, fl in flights_by_airline.items():
        n_cov = sum(1 for f in fl if getattr(f, 'needs_coverage', True))
        print(f"  Airline {al:4s}: {len(fl):5d} flights  ({n_cov} need coverage)")

    return dict(flights_by_airline), week_start


# ─────────────────────────────────────────────────────────────────────────────
# SATELLITE AIRPORTS  (slide 8: S ⊆ A \ B)
# ─────────────────────────────────────────────────────────────────────────────

def classify_airports(
    flights: list[Flight],
    bases: set[str],
    days: int,
) -> tuple[set[str], set[str]]:
    """
    Return (B_final, S_final) where:
      B = bases (all airports with crew)
      S = satellite airports: avg dist ≤ SAT_MAX_DIST AND ≥ SAT_MIN_FLIGHTS/day
    """
    from collections import Counter
    ap_flights: dict[str, list[Flight]] = defaultdict(list)
    for f in flights:
        ap_flights[f.origin].append(f)

    satellite: set[str] = set()
    for ap, flist in ap_flights.items():
        if ap in bases:
            continue
        avg_dist = sum(f.distance for f in flist) / len(flist) if flist else LARGE
        daily_avg = len(flist) / max(1, days)
        if avg_dist <= SAT_MAX_DIST and daily_avg >= SAT_MIN_FLIGHTS:
            satellite.add(ap)

    return bases, satellite


# ─────────────────────────────────────────────────────────────────────────────
# CREW BASE SIZING  (slide 12)
# n_b = max(s_min, ceil( (Σ_{f: orig=b} r_f * δ_f / d_duty) * 1.5 ) + noise )
# ─────────────────────────────────────────────────────────────────────────────

def size_crew_bases(
    flights: list[Flight],
    seed: int = RANDOM_SEED,
) -> list[CrewMember]:
    """
    Implements the base-sizing formula from slide 12:
      n_b = max(s_min, ceil((Σ_{f: orig=b} r_f * δ_f / d_duty) * 1.5) + noise)

    Key constraints:
    - Bases are ONLY created at airports with originating flights (demand > 0).
      Destination-only airports cannot be crew bases: a crew member there would
      have no outgoing flight arcs from their home depot, making flow balance
      (out - in = 1) immediately infeasible.
    - S_MIN applies only to real origin airports, not the entire network.
    """
    rng = random.Random(seed)

    # Only origin airports can be bases (slide 8: B ⊆ A)
    origin_airports = sorted(set(f.origin for f in flights))

    # d_duty: available duty minutes per crew member per planning horizon (slide 12)
    # Use 8h/day as practical working utilisation (not the 14h legal maximum).
    horizon_days = max(f.arr_min for f in flights) / 1440 if flights else 3
    d_duty = 8 * 60 * max(1.0, horizon_days)

    demand_min: dict[str, float] = defaultdict(float)
    for f in flights:
        demand_min[f.origin] += f.min_crew * f.duration

    crew_list: list[CrewMember] = []
    cid = 0
    for ap in origin_airports:
        raw_demand = demand_min[ap]   # always > 0 since ap ∈ origin_airports
        needed = math.ceil((raw_demand / d_duty) * 1.5)
        noisy  = int(rng.gauss(needed, max(1, needed * 0.10)))
        count  = max(S_MIN, min(S_MAX, noisy))
        for _ in range(count):
            crew_list.append(CrewMember(id=cid, base=ap))
            cid += 1

    total_demand = sum(demand_min.values())
    print(f"  Created {len(crew_list):,} crew across {len(origin_airports)} origin bases "
          f"(skipped {len(set(f.dest for f in flights) - set(origin_airports))} dest-only airports)")
    print(f"  Total crew-minutes demand: {total_demand:,.0f}")
    return crew_list


# ─────────────────────────────────────────────────────────────────────────────
# OPPORTUNITY COST MODEL  (slide 10: c_dh = 20/min + opp_cost)
# ─────────────────────────────────────────────────────────────────────────────

def _opp_cost_scale(lf: float) -> float:
    if lf <= LF_LOW:
        return 0.0
    if lf >= LF_HIGH:
        return 1.0
    return (lf - LF_LOW) / (LF_HIGH - LF_LOW)


def deadhead_cost(f: Flight) -> float:
    """c_dh^a for deadhead arc on flight f (slide 10)."""
    base = f.duration * C_DH
    fare = FARE_BASE + FARE_PER_MILE * max(0.0, f.distance)
    opp  = fare * _opp_cost_scale(f.load_factor)
    return base + opp


# ─────────────────────────────────────────────────────────────────────────────
# ROLLING HORIZON WINDOWS  (slide 20)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Window:
    idx: int
    t_start: int    # minutes
    t_commit: int   # minutes (commit boundary)
    t_hor: int      # minutes (horizon end = t_start + (T_solve+T_tail)*1440)


def build_windows(total_days: int) -> list[Window]:
    """
    Slide 20:
      t_start_w = (w-1) * T_commit * 1440
      t_commit_w = t_start_w + T_commit * 1440
      t_hor_w   = t_start_w + (T_solve + T_tail) * 1440
    """
    windows = []
    w = 0
    while True:
        t_start  = w * T_DAYS_COMMIT * 1440
        if t_start >= total_days * 1440:
            break
        t_commit = t_start + T_DAYS_COMMIT * 1440
        t_hor    = t_start + (T_DAYS_SOLVE + T_DAYS_TAIL) * 1440
        windows.append(Window(idx=w, t_start=t_start, t_commit=t_commit, t_hor=t_hor))
        w += 1
    return windows


def slice_flights(flights: list[Flight], win: Window) -> tuple[list[Flight], list[Flight]]:
    """
    Slide 21:
      F_w     = {f : t_start ≤ dep_f < t_hor}
      F_cov_w = {f : dep_f < t_commit}
    """
    f_w   = [f for f in flights if win.t_start <= f.dep_min < win.t_hor]
    f_cov = [f for f in f_w     if f.dep_min  <  win.t_commit]
    return f_w, f_cov


# ─────────────────────────────────────────────────────────────────────────────
# REACHABILITY PRUNING  (slides 18–19)
# Fwd/Bwd Dijkstra on the time-expanded graph; arcs violating d_work or d_away
# are excluded.
# ─────────────────────────────────────────────────────────────────────────────

def compute_reachable(
    nodes: list[Node],
    arcs_from: dict[Node, list[Arc]],
    arcs_to:   dict[Node, list[Arc]],
    depot_nodes: list[Node],
    horizon_nodes: list[Node],
    t_hor: int,
) -> set[Node]:
    """
    R_b^w = Fwd_b(G_w) ∩ Bwd_b(G_w)  (slide 18).
    Fwd: Dijkstra from depot nodes.
    Bwd: Dijkstra backwards from horizon nodes.
    Clock state (d_work, d_away) tracked; arcs that violate limits are excluded.
    Returns the intersection set of reachable nodes.
    """
    INF = float('inf')
    node_set = set(nodes)

    def dijkstra_fwd(sources: list[Node]) -> set[Node]:
        dist = {n: INF for n in node_set}
        # State: (cost, d_work_days, time_away, node)
        pq = []
        for s in sources:
            dist[s] = 0.0
            heapq.heappush(pq, (0.0, 0, 0, s))
        visited = set()
        while pq:
            d, dw, ta, u = heapq.heappop(pq)
            if u in visited:
                continue
            visited.add(u)
            for arc in arcs_from.get(u, []):
                v = arc.end
                if v not in node_set:
                    continue
                # d_work violation: more than D_WORK consecutive days
                new_dw = dw + (1 if arc.arc_type == 'flight' else 0)
                if new_dw > D_WORK:
                    continue
                # d_away: time_away > DELTA_AWAY minutes
                new_ta = arc.true_end - (u.time if u.time > 0 else 0)
                if arc.arc_type in ('flight', 'deadhead') and new_ta > DELTA_AWAY:
                    continue
                nd = d + arc.cost
                if nd < dist.get(v, INF):
                    dist[v] = nd
                    heapq.heappush(pq, (nd, new_dw, new_ta, v))
        return {n for n, d in dist.items() if d < INF}

    def dijkstra_bwd(sources: list[Node]) -> set[Node]:
        dist = {n: INF for n in node_set}
        pq = []
        for s in sources:
            dist[s] = 0.0
            heapq.heappush(pq, (0.0, s))
        visited = set()
        while pq:
            d, u = heapq.heappop(pq)
            if u in visited:
                continue
            visited.add(u)
            for arc in arcs_to.get(u, []):
                v = arc.start
                if v not in node_set:
                    continue
                nd = d + arc.cost
                if nd < dist.get(v, INF):
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))
        return {n for n, d in dist.items() if d < INF}

    fwd = dijkstra_fwd(depot_nodes)
    bwd = dijkstra_bwd(horizon_nodes)
    return fwd & bwd


# ─────────────────────────────────────────────────────────────────────────────
# CORE DDD NETWORK (one airline, one window)
# ─────────────────────────────────────────────────────────────────────────────

class CrewDDDNetwork:
    """
    Time-expanded network for one airline and one rolling-horizon window.

    Variables (slide 25):
      x_{c,a} ∈ [0,1]  per crew member c, arc a  (LP relaxation → MIP binary)
      s_f ∈ [0, r_f]   coverage slack per flight f ∈ F_cov

    Objective (slide 26):
      min Σ_c Σ_{a∈A_fl} c_fl*δ_a * x_{c,a}
        + Σ_c Σ_{a∈A_dh} c_dh^a * x_{c,a}
        + Σ_c Σ_{a∈A_wt} (c_wt*Δt_a + c_ov*1[Δt≥4h]) * x_{c,a}
        + Σ_{f∈F_cov} c_unc * s_f

    Constraint 1' (slide 27): flow balance per crew member
    Constraint 2' (slide 28): coverage per flight (with slack)
    Constraint 3' (slide 29): duty + home-break enforced structurally (no MIP constr)
    Constraint 4' (slide 30): x ∈ [0,1], s ∈ ℤ≥0
    """

    def __init__(
        self,
        flights: list[Flight],         # F_w (full window)
        cov_flights: list[Flight],     # F_cov_w (need coverage)
        crew: list[CrewMember],
        win: Window,
        time_bucket: int = DELTA_BUCKET,
        carry_positions: Optional[dict[str, dict[str, int]]] = None,
        carry_clocks: Optional[dict[int, ClockState]] = None,
        verbose: bool = True,
    ):
        self.flights    = flights
        self.cov_set    = {f.id for f in cov_flights}
        self.cov_flights = cov_flights
        self.crew       = crew
        self.crew_by_id = {c.id: c for c in crew}
        self.win        = win
        self.horizon_end = win.t_hor
        self.time_bucket = time_bucket
        self.verbose    = verbose

        # Carry-over from previous window (slide 22–23)
        self.carry_positions: dict[str, dict[str, int]] = carry_positions or {}
        self.carry_clocks: dict[int, ClockState] = carry_clocks or {}

        # Crew per base
        self.base_crew: dict[str, int] = defaultdict(int)
        for c in crew:
            self.base_crew[c.base] += 1
        self.airports = sorted(self.base_crew.keys())

        # Network topology
        self.nodes: set[Node] = set()
        self.nodes_by_airport: dict[str, list[Node]] = defaultdict(list)
        self.arcs: set[Arc]   = set()
        self._arc_counter = 0
        self.arcs_from: dict[Node, list[Arc]] = defaultdict(list)
        self.arcs_to:   dict[Node, list[Arc]] = defaultdict(list)
        self.wait_arc_by_start: dict[Node, Arc] = {}
        self._arcs_by_flight: dict[int, list[Arc]] = defaultdict(list)

        # Duty tracking (shared topology estimate for arc building)
        self.min_duty_at: dict[Node, int] = {}

        # Gurobi handles
        self.model: Optional[gp.Model] = None
        self.arc_var: dict[int, dict[Arc, gp.Var]] = {}
        self.slack_var: dict[int, gp.Var] = {}
        self.flow_constrs: dict[int, dict[Node, gp.Constr]] = {}
        self.coverage_constrs: dict[int, gp.Constr] = {}
        self._base_reachable_arcs: dict[str, set[Arc]] = {}  # populated in build_model

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _find_node_at_or_before(self, airport: str, time: int) -> Optional[Node]:
        import bisect
        nodes = self.nodes_by_airport[airport]
        if not nodes:
            return None
        times = [n.time for n in nodes]
        idx = bisect.bisect_right(times, time) - 1
        return nodes[idx] if idx >= 0 else None

    def _find_node_at_or_after(self, airport: str, time: int) -> Optional[Node]:
        import bisect
        nodes = self.nodes_by_airport[airport]
        if not nodes:
            return None
        times = [n.time for n in nodes]
        idx = bisect.bisect_left(times, time)
        return nodes[idx] if idx < len(nodes) else None

    def _snap_arrival(self, airport: str, true_arr: int) -> Optional[Node]:
        """
        Slide 17: snap arrival end of flight arc to earliest existing node at dest
        satisfying the turnaround gap:
          t' = min{t' ∈ T_ap : t' >= true_arr + Δ_ta}

        This enforces that crew cannot immediately depart on another flight after
        landing — they must wait at least Δ_ta minutes at the destination.
        The arc ends at t', not at true_arr. true_arr is stored separately as
        arc.true_end for violation detection in the DDD loop.

        Depot departures (t=0) are exempt per slide 17.
        """
        return self._find_node_at_or_after(airport, true_arr + DELTA_TA)

    def _add_node_sorted(self, node: Node):
        import bisect
        if node in self.nodes:
            return
        self.nodes.add(node)
        ap_list = self.nodes_by_airport[node.airport]
        times = [n.time for n in ap_list]
        idx = bisect.bisect_left(times, node.time)
        ap_list.insert(idx, node)

    # ── Network construction ──────────────────────────────────────────────────

    def build_initial_network(self):
        import bisect, time as _t
        print(f"  Building network (window {self.win.idx}: "
              f"t={self.win.t_start//1440}d–{self.win.t_commit//1440}d commit, "
              f"hor={self.win.t_hor//1440}d)...")
        t0 = _t.time()

        # 1. Depot (t=0) and horizon nodes (slide 14).
        #    Only crew BASE airports (origin airports) get depot/horizon nodes.
        #    Destination-only airports have no crew starting there, so no depot needed.
        #    All airports still get flight-time nodes (step 2 below).
        base_airports = set(c.base for c in self.crew)
        for ap in base_airports:
            self._add_node_sorted(Node(ap, DEPOT_START))
            self._add_node_sorted(Node(ap, self.horizon_end))

        # 2. Departure-time nodes only (slide 14).
        #    We insert dep times at both origin AND dest so that every airport
        #    has nodes for every flight event touching it.
        #
        #    We do NOT pre-insert snapped arrival times (arr + Delta_ta).
        #    The old code inserted (dest, arr+45) as a standalone node, which
        #    sat outside the wait-arc chain if it didn't coincide with a
        #    departure time or horizon node — leaving crew stranded with no
        #    path home and making the horizon flow constraint permanently
        #    infeasible. By inserting departure times only, the wait-arc chain
        #    built in step 3 is guaranteed to connect every node to the horizon,
        #    and _snap_arrival finds the next node already in that chain.
        for f in self.flights:
            self._add_node_sorted(Node(f.origin, f.dep_min))
            self._add_node_sorted(Node(f.dest,   f.dep_min))

        if self.verbose:
            print(f"    Nodes after dep/arr: {len(self.nodes)}  ({_t.time()-t0:.1f}s)")

        # 3. Wait arcs: chain consecutive nodes at each airport (slide 15).
        #    Every airport's timeline now ends at its horizon node (for bases)
        #    so wait arcs fully connect each node to the horizon.
        for ap in self.nodes_by_airport:
            ap_nodes = self.nodes_by_airport[ap]
            for i in range(len(ap_nodes) - 1):
                self._make_wait_arc(ap_nodes[i], ap_nodes[i+1])

        if self.verbose:
            print(f"    Wait arcs built  ({_t.time()-t0:.1f}s)")

        # 4. Initialise duty clocks at depots
        for ap in self.nodes_by_airport:
            depot_node = Node(ap, DEPOT_START)
            if depot_node in self.nodes:
                self.min_duty_at[depot_node] = 0

        # 5. Flight + deadhead arcs (slides 15–16)
        #    Arrival end is snapped to satisfy Δ_ta (slide 17).
        n_arcs = 0
        n_missing = 0
        n_duty_pruned = 0
        for f in sorted(self.flights, key=lambda x: x.dep_min):
            dep_node = self._find_node_at_or_before(f.origin, f.dep_min)
            if dep_node is None or dep_node.time != f.dep_min:
                n_missing += 1
                continue

            # Snap arrival (slide 17)
            arr_node = self._snap_arrival(f.dest, f.arr_min)
            if arr_node is None or arr_node.time > self.horizon_end:
                n_missing += 1
                continue

            # Duty-time check (slide 11 / Constraint 3')
            duty_at_dep = self.min_duty_at.get(dep_node, 0)
            duty_after  = duty_at_dep + f.duration
            if duty_after > DELTA_DUTY:
                n_duty_pruned += 1
                continue

            # Update min_duty_at arr_node
            existing = self.min_duty_at.get(arr_node, DELTA_DUTY + 1)
            if duty_after < existing:
                self.min_duty_at[arr_node] = duty_after

            # Flight arc: cost = c_fl * δ_f  (slide 15)
            self._make_arc(dep_node, arr_node, f.arr_min,
                           f.duration * C_FL, 'flight', f.id)
            # Deadhead arc: cost = c_dh^a  (slide 16)
            self._make_arc(dep_node, arr_node, f.arr_min,
                           deadhead_cost(f), 'deadhead', f.id)
            n_arcs += 2

        if self.verbose:
            if n_missing:
                print(f"    WARNING: {n_missing} flight arcs could not be placed")
            if n_duty_pruned:
                print(f"    Duty-pruned: {n_duty_pruned} arcs exceed Δ_duty")
            print(f"    Flight+deadhead arcs: {n_arcs}  total: {len(self.arcs)}  "
                  f"({_t.time()-t0:.1f}s)")

    def _make_wait_arc(self, frm: Node, to: Node) -> Arc:
        dt = to.time - frm.time
        overnight = 1 if dt >= OVERNIGHT_THRESHOLD else 0
        cost = dt * C_WT + overnight * C_OV
        arc = self._make_arc(frm, to, to.time, cost, 'wait')
        self.wait_arc_by_start[frm] = arc
        # Rest resets duty (slide 29: wait ≥ Δ_rest resets duty clock)
        if dt >= DELTA_REST:
            self.min_duty_at[to] = 0
        return arc

    def _make_arc(self, start: Node, end: Node, true_end: int,
                  cost: float, arc_type: str,
                  flight_id: Optional[int] = None) -> Arc:
        # De-duplicate
        for existing in self.arcs_from.get(start, []):
            if (existing.end == end and existing.arc_type == arc_type
                    and existing.flight_id == flight_id):
                return existing

        arc = Arc(
            id=self._arc_counter,
            start=start, end=end,
            true_end=true_end, cost=cost,
            arc_type=arc_type, flight_id=flight_id,
        )
        self._arc_counter += 1
        self.arcs.add(arc)
        self.arcs_from[start].append(arc)
        self.arcs_to[end].append(arc)
        if flight_id is not None:
            self._arcs_by_flight[flight_id].append(arc)

        if self.model is not None:
            for c in self.crew:
                if self._crew_can_use_arc(c, arc):
                    self._add_arc_var_for_crew(c.id, arc)
        return arc

    def _remove_arc(self, arc: Arc):
        self.arcs.discard(arc)
        self.arcs_from[arc.start] = [a for a in self.arcs_from[arc.start] if a != arc]
        self.arcs_to[arc.end]     = [a for a in self.arcs_to[arc.end]     if a != arc]
        if arc.flight_id is not None:
            self._arcs_by_flight[arc.flight_id] = [
                a for a in self._arcs_by_flight[arc.flight_id] if a != arc
            ]
        if self.model is not None:
            for cid, cvars in self.arc_var.items():
                if arc in cvars:
                    var = cvars[arc]
                    # Zero out this variable's contribution in flow balance constraints
                    # BEFORE removing it.  Gurobi silently drops removed variables from
                    # constraints, which would corrupt the depot out-in==1 RHS.
                    cf = self.flow_constrs.get(cid, {})
                    if arc.start in cf:
                        self.model.chgCoeff(cf[arc.start], var, 0.0)
                    if arc.end in cf:
                        self.model.chgCoeff(cf[arc.end], var, 0.0)
                    # Zero out coverage constraint contribution (should only be
                    # flight arcs, but guard generically)
                    if arc.arc_type == 'flight' and arc.flight_id in self.coverage_constrs:
                        self.model.chgCoeff(
                            self.coverage_constrs[arc.flight_id], var, 0.0
                        )
                    self.model.remove(var)
                    del cvars[arc]

    def _crew_can_use_arc(self, crew: CrewMember, arc: Arc) -> bool:
        """
        Return True iff this crew member may use this arc.

        Home-spine wait arcs (both endpoints at the crew's home airport) are
        always allowed, regardless of the reachability set.  They form the
        chain depot->...->horizon that every crew member must traverse, and
        must never be blocked by a stale or temporarily incomplete reachability
        computation (which would turn fb_depot_<c> into 0 = 1).
        """
        if (arc.arc_type == 'wait'
                and arc.start.airport == crew.base
                and arc.end.airport == crew.base):
            return True
        return arc in self._base_reachable_arcs.get(crew.base, set())

    def _compute_base_reachability(self):
        """
        Compute reachable arc sets once per base (not per crew member).
        All crew from the same base with the same clock state share the same
        reachable subgraph. This reduces Dijkstra calls from |C| to |B|.

        R_b = arcs whose both endpoints are in Fwd_b ∩ Bwd_b  (slide 18).
        Clock state from carry-over is used for Fwd pruning (d_work, d_away).
        """
        import time as _t
        t0 = _t.time()
        bases = sorted(set(c.base for c in self.crew))
        self._base_reachable_arcs: dict[str, set[Arc]] = {}

        node_list   = list(self.nodes)
        arcs_from_d = dict(self.arcs_from)   # snapshot
        arcs_to_d   = dict(self.arcs_to)

        for base in bases:
            depot_node   = Node(base, DEPOT_START)
            horizon_node = Node(base, self.horizon_end)

            if depot_node not in self.nodes or horizon_node not in self.nodes:
                self._base_reachable_arcs[base] = set()
                continue

            # Worst-case carry-over clock for this base
            base_clocks = [self.carry_clocks.get(c.id, ClockState())
                           for c in self.crew if c.base == base]
            max_dwork = max((cs.d_work for cs in base_clocks), default=0)

            reachable_nodes = compute_reachable(
                nodes=node_list,
                arcs_from=arcs_from_d,
                arcs_to=arcs_to_d,
                depot_nodes=[depot_node],
                horizon_nodes=[horizon_node],
                t_hor=self.horizon_end,
            )

            # An arc is usable iff both its endpoints are reachable
            reachable_arcs = {
                arc for arc in self.arcs
                if arc.start in reachable_nodes and arc.end in reachable_nodes
            }
            self._base_reachable_arcs[base] = reachable_arcs

        n_bases = len(bases)
        avg_arcs = (sum(len(v) for v in self._base_reachable_arcs.values()) / max(1, n_bases))
        total_arcs = len(self.arcs)
        first_call = not getattr(self, '_reachability_logged', False)
        if self.verbose:
            print(f"    Reachability: {n_bases} bases, avg {avg_arcs:.0f}/{total_arcs} arcs "
                  f"reachable per base  ({_t.time()-t0:.1f}s)")

        # ── Connected component diagnostic (first call only) ──────────────────
        # Two bases are in the same component if their reachable flight-arc sets
        # overlap (i.e. there exists at least one flight arc reachable from both).
        if first_call and self.verbose:
            flight_arc_sets: dict[str, frozenset] = {
                b: frozenset(
                    a.id for a in arcs
                    if a.arc_type in ('flight', 'deadhead')
                )
                for b, arcs in self._base_reachable_arcs.items()
            }

            # Union-Find
            parent = {b: b for b in bases}

            def find(x):
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            def union(x, y):
                parent[find(x)] = find(y)

            base_list = list(bases)
            for i in range(len(base_list)):
                for j in range(i + 1, len(base_list)):
                    bi, bj = base_list[i], base_list[j]
                    if flight_arc_sets[bi] & flight_arc_sets[bj]:
                        union(bi, bj)

            # Group bases by root
            components: dict[str, list[str]] = defaultdict(list)
            for b in bases:
                components[find(b)].append(b)

            # Separate out isolated bases (no flight arcs at all)
            connected = {r: sorted(members)
                         for r, members in components.items()
                         if any(flight_arc_sets[b] for b in members)}
            isolated  = sorted(
                b for b in bases if not flight_arc_sets[b]
            )

            n_comp = len(connected)
            print(f"    Connected components: {n_comp}  |  "
                  f"isolated bases (no flight arcs): {len(isolated)}")
            if n_comp > 1:
                for idx, (_, members) in enumerate(
                    sorted(connected.items(), key=lambda kv: -len(kv[1])), 1
                ):
                    n_crew = sum(self.base_crew.get(b, 0) for b in members)
                    print(f"      Component {idx}: {len(members)} bases, "
                          f"{n_crew} crew  [{', '.join(members[:8])}"
                          f"{'…' if len(members) > 8 else ''}]")
            if isolated:
                n_iso_crew = sum(self.base_crew.get(b, 0) for b in isolated)
                print(f"      Isolated: {isolated[:8]}"
                      f"{'…' if len(isolated) > 8 else ''}  "
                      f"({n_iso_crew} crew will be dropped)")

            self._reachability_logged = True

    def add_node(self, airport: str, time: int):
        """DDD refinement: insert a new node, rewire wait arcs, expose new flight arcs."""
        import bisect
        new_node = Node(airport=airport, time=time)
        if new_node in self.nodes:
            return

        self._add_node_sorted(new_node)

        ap_nodes = self.nodes_by_airport[airport]
        times = [n.time for n in ap_nodes]
        pos = bisect.bisect_left(times, time)

        prev_node = ap_nodes[pos - 1] if pos > 0 else None
        next_node = ap_nodes[pos + 1] if pos + 1 < len(ap_nodes) else None

        # Remove the old spanning wait arc BEFORE recomputing reachability so
        # that Dijkstra does not traverse a stale arc that is about to be split.
        if prev_node and prev_node in self.wait_arc_by_start:
            old = self.wait_arc_by_start[prev_node]
            if old.end.time >= new_node.time:   # old arc spans the new node
                self._remove_arc(old)
                del self.wait_arc_by_start[prev_node]

        # Recompute reachability NOW — after removing the stale arc but BEFORE
        # creating the two replacement wait arcs — so that _make_arc's auto-wiring
        # sees an up-to-date reachable set and correctly registers the new wait
        # arcs for every crew member.  Doing this after _make_wait_arc (as the
        # previous version did) left depot nodes with no outgoing variable,
        # turning fb_depot_<c> into 0 = 1 and making Iter 1 infeasible.
        if self.model is not None and self._base_reachable_arcs:
            _sv, self.verbose = self.verbose, False
            self._compute_base_reachability()
            self.verbose = _sv

        # Now create the two replacement wait arcs; reachability is fresh so
        # _make_arc will wire them into the model correctly.
        if prev_node:
            self._make_wait_arc(prev_node, new_node)
        if next_node:
            self._make_wait_arc(new_node, next_node)

        # Add flow balance constraints for the new node for every crew member
        # that already has a model variable touching it (via the newly created
        # wait arcs or the flight arcs about to be exposed below).
        if self.model is not None:
            for cid in self.flow_constrs:
                if new_node not in self.flow_constrs[cid]:
                    self._add_flow_constr_for_crew(cid, new_node)

        # Expose new flight/deadhead arcs departing from or arriving at new_node,
        # then recompute reachability once more so subsequent add_node calls and
        # _add_arc_var_for_crew see the newly reachable flight arcs.
        self._expose_flight_arcs_from(new_node)
        self._expose_flight_arcs_to(new_node)

        if self.model is not None and self._base_reachable_arcs:
            _sv, self.verbose = self.verbose, False
            self._compute_base_reachability()
            self.verbose = _sv

    def _expose_flight_arcs_from(self, node: Node):
        for f in self.flights:
            if f.origin != node.airport or f.dep_min != node.time:
                continue
            arr_node = self._snap_arrival(f.dest, f.arr_min)
            if arr_node is None:
                continue
            duty_at_dep = self.min_duty_at.get(node, 0)
            if duty_at_dep + f.duration > DELTA_DUTY:
                continue
            self._make_arc(node, arr_node, f.arr_min,
                           f.duration * C_FL, 'flight', f.id)
            self._make_arc(node, arr_node, f.arr_min,
                           deadhead_cost(f), 'deadhead', f.id)

    def _expose_flight_arcs_to(self, node: Node):
        for f in self.flights:
            if f.dest != node.airport:
                continue
            dep_node = self._find_node_at_or_before(f.origin, f.dep_min)
            if dep_node is None or dep_node.time != f.dep_min:
                continue
            duty_at_dep = self.min_duty_at.get(dep_node, 0)
            if duty_at_dep + f.duration > DELTA_DUTY:
                continue
            # Check snap: does this flight's arrival snap to node?
            snapped = self._snap_arrival(f.dest, f.arr_min)
            if snapped != node:
                continue
            self._make_arc(dep_node, node, f.arr_min,
                           f.duration * C_FL, 'flight', f.id)
            self._make_arc(dep_node, node, f.arr_min,
                           deadhead_cost(f), 'deadhead', f.id)

    # ── Gurobi model (Constraints 1'–4', slides 27–30) ───────────────────────

    def build_model(self):
        import time as _t
        t0 = _t.time()
        self.model = gp.Model("CrewPairing_DDD")
        self.model.setParam("OutputFlag", 0)

        # ── Per-base reachability (prune variables before creation) ───────────
        self._base_reachable_arcs = {}
        self._compute_base_reachability()

        # ── Active crew: drop crew whose base cannot reach any coverage flight ─
        # Crew at bases in disconnected network components (or bases with no path
        # to any coverage flight) generate only wait-arc variables and contribute
        # nothing to coverage constraints, while bloating the model with thousands
        # of useless variables and flow-balance constraints.
        coverage_flight_ids = {f.id for f in self.cov_flights}
        active_crew = []
        dropped_crew = 0
        for c in self.crew:
            reachable = self._base_reachable_arcs.get(c.base, set())
            can_cover = any(
                a.arc_type == 'flight' and a.flight_id in coverage_flight_ids
                for a in reachable
            )
            if can_cover:
                active_crew.append(c)
            else:
                dropped_crew += 1
        self.active_crew = active_crew  # stored for use in extract_solution etc.
        if self.verbose and dropped_crew:
            print(f"    Dropped {dropped_crew} crew with no path to any coverage flight "
                  f"({len(active_crew)} active of {len(self.crew)} total)")

        # ── Variables: x_{c,a} ∈ [0,1] (slide 25) ───────────────────────────
        total_vars = 0
        for c in active_crew:
            self.arc_var[c.id] = {}
            self.flow_constrs[c.id] = {}
            for arc in self.arcs:
                if self._crew_can_use_arc(c, arc):
                    var = self.model.addVar(
                        lb=0.0, ub=1.0, obj=arc.cost,
                        vtype=GRB.CONTINUOUS,
                        name=f"x_{c.id}_{arc.id}",
                    )
                    self.arc_var[c.id][arc] = var
                    total_vars += 1

        # ── Slack variables s_f (slide 25) ───────────────────────────────────
        for f in self.cov_flights:
            sv = self.model.addVar(
                lb=0.0, ub=float(f.min_crew),
                obj=C_UNC_EFFECTIVE,
                vtype=GRB.CONTINUOUS,
                name=f"slack_{f.id}",
            )
            self.slack_var[f.id] = sv

        self.model.update()
        if self.verbose:
            print(f"    Variables: {total_vars:,} arc + {len(self.slack_var)} slack  "
                  f"({_t.time()-t0:.1f}s)")

        # ── Constraint 1': Flow balance (slide 27) ────────────────────────────
        # "Crew cannot start from or end at another base's depot/horizon node."
        # Enforced by: home depot gets +1 source, home horizon gets -1 sink.
        # All other nodes (including other bases' depot nodes at t=0) are plain
        # flow-conservation (in == out). We do NOT set out==0 at other depots —
        # that would block wait arcs originating there and cause infeasibility.
        node_list = list(self.nodes)
        skipped_crew = 0
        for c in active_crew:
            cvars = self.arc_var[c.id]
            if not cvars:
                skipped_crew += 1
                continue  # no arcs for this crew — skip to avoid 0==1 constraint

            home_depot   = Node(c.base, DEPOT_START)
            home_horizon = Node(c.base, self.horizon_end)

            for node in node_list:
                out_arcs = [a for a in self.arcs_from.get(node, []) if a in cvars]
                in_arcs  = [a for a in self.arcs_to.get(node, [])   if a in cvars]

                # Skip nodes where this crew has no arcs at all (no constraint needed)
                if not out_arcs and not in_arcs:
                    continue

                out_expr = gp.quicksum(cvars[a] for a in out_arcs) if out_arcs else 0.0
                in_expr  = gp.quicksum(cvars[a] for a in in_arcs)  if in_arcs  else 0.0

                if node == home_depot:
                    # Source: exactly one unit departs from home depot (slide 27)
                    constr = self.model.addConstr(out_expr - in_expr == 1,
                        name=f"fb_depot_{c.id}")
                elif node == home_horizon:
                    # Sink: exactly one unit absorbed at home horizon (slide 27)
                    constr = self.model.addConstr(in_expr - out_expr == 1,
                        name=f"fb_horizon_{c.id}")
                else:
                    # Flow conservation at all other nodes (including other bases' t=0 nodes)
                    constr = self.model.addConstr(out_expr == in_expr,
                        name=f"fb_{c.id}_{node.airport}_{node.time}")

                self.flow_constrs[c.id][node] = constr

        if self.verbose:
            if skipped_crew:
                print(f"    Skipped {skipped_crew} crew with no arcs (isolated bases)")
            print(f"    Flow balance constraints added  ({_t.time()-t0:.1f}s)")

        # ── Constraint 2': Coverage (slide 28) ───────────────────────────────
        for f in self.cov_flights:
            fl_arcs = [a for a in self._arcs_by_flight.get(f.id, [])
                       if a.arc_type == 'flight']
            if not fl_arcs:
                continue
            cov_expr = gp.quicksum(
                self.arc_var[c.id][arc]
                for c in active_crew
                for arc in fl_arcs
                if arc in self.arc_var[c.id]
            )
            constr = self.model.addConstr(
                cov_expr + self.slack_var[f.id] >= f.min_crew,
                name=f"cov_{f.id}",
            )
            self.coverage_constrs[f.id] = constr

        self.model.update()
        if self.verbose:
            print(f"    Coverage constraints: {len(self.coverage_constrs)}/{len(self.cov_flights)}  "
                  f"({_t.time()-t0:.1f}s)")
            print(f"    Model: {self.model.NumVars:,} vars, "
                  f"{self.model.NumConstrs:,} constrs  ({_t.time()-t0:.1f}s)")

    def _add_arc_var_for_crew(self, crew_id: int, arc: Arc):
        if arc in self.arc_var.get(crew_id, {}):
            return
        c = self.crew_by_id.get(crew_id)
        if c is None:
            return
        # Re-check reachability here as a guard (the primary gate is
        # _crew_can_use_arc in _make_arc; this handles arcs added via other
        # paths such as DDD refinement).  The home-spine bypass lives in
        # _crew_can_use_arc so it applies consistently everywhere.
        if not self._crew_can_use_arc(c, arc):
            return
        if crew_id not in self.arc_var:
            self.arc_var[crew_id] = {}
        var = self.model.addVar(lb=0.0, ub=1.0, obj=arc.cost,
                                vtype=GRB.CONTINUOUS,
                                name=f"x_{crew_id}_{arc.id}")
        self.model.update()  # flush so chgCoeff sees the new variable immediately
        self.arc_var[crew_id][arc] = var

        # Wire into flow balance constraints with correct signs.
        #
        # build_model writes constraints as:
        #   depot   node:   out_expr - in_expr == +1   -> arc outgoing here -> coeff = +1
        #   horizon node:   in_expr  - out_expr == +1  -> arc incoming here -> coeff = +1
        #   other   nodes:  out_expr - in_expr  ==  0  -> arc outgoing     -> coeff = +1
        #                                               -> arc incoming     -> coeff = -1
        #
        # The old code had -1 at arc.start and +1 at arc.end — exactly backwards —
        # which corrupted every new arc's flow contribution during DDD refinement
        # and caused Iter 1 to be infeasible.
        cf = self.flow_constrs.get(crew_id, {})
        home_horizon = Node(c.base, self.horizon_end)

        # Arc is outgoing at its start node -> always +1
        if arc.start in cf:
            self.model.chgCoeff(cf[arc.start], var, +1)

        # Arc is incoming at its end node.
        # Horizon constraint is written as (in - out == 1) so incoming arc is +1 there too.
        # All other constraints are (out - in == RHS) so incoming arc is -1.
        if arc.end in cf:
            coeff = +1 if arc.end == home_horizon else -1
            self.model.chgCoeff(cf[arc.end], var, coeff)

        # Wire into coverage
        if arc.arc_type == 'flight' and arc.flight_id in self.coverage_constrs:
            self.model.chgCoeff(self.coverage_constrs[arc.flight_id], var, 1)

    def _add_flow_constr_for_crew(self, crew_id: int, node: Node):
        c = self.crew_by_id[crew_id]
        cvars = self.arc_var.get(crew_id, {})
        out_arcs = [a for a in self.arcs_from.get(node, []) if a in cvars]
        in_arcs  = [a for a in self.arcs_to.get(node, [])   if a in cvars]

        # Skip nodes where this crew has no arcs (no constraint needed yet;
        # _add_arc_var_for_crew will wire into existing constraints when arcs appear)
        if not out_arcs and not in_arcs:
            return

        out_expr = gp.quicksum(cvars[a] for a in out_arcs) if out_arcs else 0.0
        in_expr  = gp.quicksum(cvars[a] for a in in_arcs)  if in_arcs  else 0.0
        home_depot   = Node(c.base, DEPOT_START)
        home_horizon = Node(c.base, self.horizon_end)

        # NOTE: the old code had an extra branch:
        #   elif node.time == DEPOT_START and node.airport != c.base:
        #       constr = self.model.addConstr(out_expr == 0)
        # This was WRONG: DEPOT_START == 0 == midnight, so any real flight
        # departing at minute 0 from a non-home airport would have its outgoing
        # flow zeroed out, making coverage of those flights impossible.
        # The home-depot source and home-horizon sink are the only special nodes;
        # everything else is plain flow conservation.
        if node == home_depot:
            constr = self.model.addConstr(out_expr - in_expr == 1,
                                          name=f"fb_depot_{crew_id}")
        elif node == home_horizon:
            constr = self.model.addConstr(in_expr - out_expr == 1,
                                          name=f"fb_horizon_{crew_id}")
        else:
            constr = self.model.addConstr(out_expr == in_expr,
                                          name=f"fb_{crew_id}_{node.airport}_{node.time}")
        if crew_id not in self.flow_constrs:
            self.flow_constrs[crew_id] = {}
        self.flow_constrs[crew_id][node] = constr

    # ── DDD Solve loop (slides 31–32) ─────────────────────────────────────────

    def inspect_violations(self) -> list[tuple[str, int]]:
        """
        Slide 31–32: detect arcs where |t'_snap - t_true| > Δ_bucket,
        or turnaround infeasible in current discretisation.
        """
        eps = 1e-4
        active_arcs: set[Arc] = set()
        for cvars in self.arc_var.values():
            for arc, var in cvars.items():
                if arc.arc_type in ('flight', 'deadhead'):
                    try:
                        if var.X > eps:
                            active_arcs.add(arc)
                    except AttributeError:
                        pass

        violations = []
        for arc in active_arcs:
            ap, true_t = arc.end.airport, arc.true_end
            snap_node = self._find_node_at_or_after(ap, arc.end.time)
            if snap_node is None or abs(snap_node.time - true_t) > self.time_bucket:
                violations.append((ap, true_t))

            # Turnaround check
            for next_arc in self.arcs_from.get(arc.end, []):
                if next_arc.arc_type not in ('flight', 'deadhead'):
                    continue
                if next_arc not in active_arcs:
                    continue
                f = next((fl for fl in self.flights if fl.id == next_arc.flight_id), None)
                if f and arc.true_end + DELTA_TA > f.dep_min:
                    violations.append((arc.end.airport, arc.true_end + DELTA_TA))

        result = []
        seen: set[tuple[str, int]] = set()
        for ap, t in violations:
            existing = self._find_node_at_or_after(ap, t)
            if (existing is None or existing.time != t) and (ap, t) not in seen:
                result.append((ap, t))
                seen.add((ap, t))
        return result

    def make_integer(self):
        """Constraint 4' (slide 30): switch to binary/integer."""
        for cvars in self.arc_var.values():
            for var in cvars.values():
                var.VType = GRB.BINARY
        for var in self.slack_var.values():
            var.VType = GRB.INTEGER
        self.model.setParam("OutputFlag", int(self.verbose))
        self.model.update()

    def solve(self, max_iter: int = DDD_MAX_ITER) -> dict:
        """
        DDD main loop (slides 31–32):
        1. Solve LP
        2. Detect violations
        3. Refine: insert nodes, bisect wait arcs, expose new arcs
        4. Repeat until no violations
        5. Switch to MIP (binary), solve with MIPGap=0.01, TimeLimit=300s
        """
        print(f"\n  === DDD Solve (window {self.win.idx}) ===")
        solved = False

        for it in range(max_iter):
            self.model.setObjective(
                gp.quicksum(
                    arc.cost * var
                    for cvars in self.arc_var.values()
                    for arc, var in cvars.items()
                ) + gp.quicksum(C_UNC_EFFECTIVE * v for v in self.slack_var.values()),
                GRB.MINIMIZE,
            )
            self.model.optimize()

            if self.model.Status != GRB.OPTIMAL:
                print(f"  Iter {it:3d}: INFEASIBLE/UNBOUNDED (status={self.model.Status}) — stopping")
                if self.model.Status == GRB.INFEASIBLE:
                    self.model.computeIIS()
                    iis_constrs = [(c.ConstrName, c.IISConstr)
                                   for c in self.model.getConstrs() if c.IISConstr]
                    iis_vars    = [(v.VarName, v.IISLB, v.IISUB)
                                   for v in self.model.getVars() if v.IISLB or v.IISUB]
                    print(f"  IIS: {len(iis_constrs)} constraints, {len(iis_vars)} variables")
                    for name, _ in iis_constrs[:20]:
                        print(f"    CONSTR: {name}")
                    for name, lb, ub in iis_vars[:10]:
                        print(f"    VAR:    {name}  (lb={lb}, ub={ub})")
                break

            obj = self.model.ObjVal
            violations = self.inspect_violations()
            n_viol = len(violations)
            print(f"  Iter {it:3d}: LP obj={obj:,.1f}  violations={n_viol}")

            if not violations:
                print("  LP converged → switching to MIP...")
                solved = True
                break

            # Cap at 500, sort by airport activity (slide 31)
            if n_viol > DDD_MAX_VIOLATIONS:
                ap_activity: dict[str, int] = defaultdict(int)
                for cvars in self.arc_var.values():
                    for arc, var in cvars.items():
                        if arc.arc_type in ('flight', 'deadhead'):
                            try:
                                if var.X > 1e-4:
                                    ap_activity[arc.end.airport] += 1
                            except AttributeError:
                                pass
                violations.sort(key=lambda v: -ap_activity.get(v[0], 0))
                violations = violations[:DDD_MAX_VIOLATIONS]

            for ap, t in violations:
                self.add_node(ap, t)
            self.model.update()

        if not solved:
            print("  Warning: DDD did not fully converge; solving MIP on current network.")

        # MIP phase (slide 32)
        self.make_integer()
        self.model.setObjective(
            gp.quicksum(
                arc.cost * var
                for cvars in self.arc_var.values()
                for arc, var in cvars.items()
            ) + gp.quicksum(C_UNC_EFFECTIVE * v for v in self.slack_var.values()),
            GRB.MINIMIZE,
        )
        self.model.setParam("MIPGap", 0.01)
        self.model.setParam("TimeLimit", 300)
        self.model.optimize()

        return self.extract_solution()

    # ── Solution extraction (slides 33–34) ────────────────────────────────────

    def extract_solution(self) -> dict:
        eps = 1e-4
        if self.model.Status not in (GRB.OPTIMAL, GRB.TIME_LIMIT, GRB.SUBOPTIMAL) \
                or self.model.SolCount == 0:
            print(f"  No solution available (status={self.model.Status}). "
                  "All flights marked uncovered.")
            return {
                "status": "infeasible",
                "cost": None,
                "flight_cost": 0.0, "deadhead_cost": 0.0, "wait_cost": 0.0,
                "uncovered_slots": sum(f.min_crew for f in self.cov_flights),
                "uncovered_flights": [(f, f.min_crew) for f in self.cov_flights],
                "routes": [],
                "num_flights": len(self.cov_flights),
                "covered_flights": 0,
            }

        obj = self.model.ObjVal

        crew_active: dict[int, list[Arc]] = {}
        for c in getattr(self, 'active_crew', self.crew):
            if c.id not in self.arc_var:
                continue
            active = [a for a, v in self.arc_var[c.id].items() if v.X > eps]
            if active:
                crew_active[c.id] = active

        uncovered = []
        for f in self.cov_flights:
            if f.id in self.slack_var:
                sv = self.slack_var[f.id].X
                if sv > eps:
                    uncovered.append((f, sv))

        flight_cost = dh_cost = wait_cost = 0.0
        for cvars in self.arc_var.values():
            for arc, var in cvars.items():
                v = var.X
                if v <= eps:
                    continue
                if arc.arc_type == 'flight':
                    flight_cost += arc.cost * v
                elif arc.arc_type == 'deadhead':
                    dh_cost += arc.cost * v
                elif arc.arc_type == 'wait':
                    wait_cost += arc.cost * v

        routes = self._extract_routes(crew_active)

        return {
            "status": ("optimal" if self.model.Status == GRB.OPTIMAL else "suboptimal"),
            "cost": obj,
            "flight_cost": flight_cost,
            "deadhead_cost": dh_cost,
            "wait_cost": wait_cost,
            "uncovered_slots": sum(s for _, s in uncovered),
            "uncovered_flights": uncovered,
            "routes": routes,
            "num_flights": len(self.cov_flights),
            "covered_flights": len(self.cov_flights) - len(uncovered),
        }

    def _extract_routes(self, crew_active: dict[int, list[Arc]]) -> list[dict]:
        """
        Slide 33: Route(c) = {ℓ ∈ A*_c | ℓ.type ≠ wait}
        Preference: flight > deadhead > wait.
        """
        routes = []
        type_priority = {'flight': 0, 'deadhead': 1, 'wait': 2}

        for crew_id, active_arcs in crew_active.items():
            c = self.crew_by_id[crew_id]
            # Build out-adjacency, prefer flight over deadhead over wait
            out: dict[Node, Arc] = {}
            for arc in active_arcs:
                existing = out.get(arc.start)
                if existing is None or (type_priority.get(arc.arc_type, 9)
                                        < type_priority.get(existing.arc_type, 9)):
                    out[arc.start] = arc

            curr = Node(c.base, DEPOT_START)
            legs = []
            visited: set[Node] = set()

            for _ in range(1000):
                if curr in visited or curr.time >= self.horizon_end:
                    break
                visited.add(curr)
                arc = out.get(curr)
                if arc is None:
                    break
                if arc.arc_type != 'wait':
                    legs.append({
                        "type":      arc.arc_type,
                        "from":      arc.start.airport,
                        "to":        arc.end.airport,
                        "dep":       arc.start.time,
                        "arr":       arc.true_end,
                        "flight_id": arc.flight_id,
                    })
                curr = arc.end

            if legs:
                routes.append({
                    "crew_id": crew_id,
                    "base":    c.base,
                    "legs":    legs,
                })

        return routes

    # ── Carry-over (slides 22–23) ──────────────────────────────────────────────

    def compute_carry_over(self) -> tuple[dict[str, dict[str, int]], dict[int, ClockState]]:
        """
        Slide 22: position n_{b,k}^{w+1} = |{c ∈ C_b : loc_w(c) = k}|
        Slide 23: worst-case clock state Γ^{w+1}_c = argmax_{c ∈ C_b} d_work_c
        Falls back to home-base positions if no solution is available.
        """
        eps = 1e-4
        t_commit = self.win.t_commit

        # Default: all crew at home base with zero clocks (used if no solution)
        no_solution = self.model is None or self.model.Status not in (
            GRB.OPTIMAL, GRB.TIME_LIMIT, GRB.SUBOPTIMAL
        ) or self.model.SolCount == 0

        # Map crew_id → last node at or before t_commit
        crew_pos: dict[int, str] = {}
        crew_clock: dict[int, ClockState] = {}

        for c in self.crew:
            cvars = self.arc_var.get(c.id, {})
            last_airport = c.base
            last_d_work = 0
            last_h_home = 0

            if not no_solution:
                try:
                    active_arcs = sorted(
                        [a for a, v in cvars.items() if v.X > eps],
                        key=lambda a: a.true_end
                    )
                    for arc in active_arcs:
                        if arc.end.time <= t_commit:
                            last_airport = arc.end.airport
                            if arc.arc_type == 'flight':
                                last_d_work += 1
                            elif arc.arc_type == 'wait' and arc.end.airport == c.base:
                                last_h_home += (arc.end.time - arc.start.time)
                except AttributeError:
                    pass  # no solution available; keep defaults

            crew_pos[c.id] = last_airport
            crew_clock[c.id] = ClockState(
                t_reset=0,
                d_work=last_d_work,
                h_home=last_h_home,
            )

        # n_{b,k}^{w+1} (slide 22)
        positions: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for c in self.crew:
            positions[c.base][crew_pos[c.id]] += 1

        # Worst-case clock per crew (slide 23): argmax d_work, tiebreak by t_reset then h_home
        worst_clocks: dict[int, ClockState] = {}
        for c in self.crew:
            # In individual model each crew member has their own clock; just carry it forward
            worst_clocks[c.id] = crew_clock[c.id]

        return dict(positions), worst_clocks


# ─────────────────────────────────────────────────────────────────────────────
# RESULT SERIALISATION
# ─────────────────────────────────────────────────────────────────────────────

def save_result(
    result: dict,
    cov_flights: list[Flight],
    crew: list[CrewMember],
    airline: str,
    window_idx: int,
    out_dir: str = ".",
):
    """Save window result to JSON for downstream visualisation."""
    payload = {
        "meta": {
            "airline": airline,
            "window": window_idx,
            "solve_status": result.get("status", "unknown"),
            "total_cost": result.get("cost") or 0.0,
            "flight_cost": result.get("flight_cost", 0.0),
            "deadhead_cost": result.get("deadhead_cost", 0.0),
            "wait_cost": result.get("wait_cost", 0.0),
            "uncovered_slots": result.get("uncovered_slots", 0.0),
            "num_flights": result.get("num_flights", 0),
            "covered_flights": result.get("covered_flights", 0),
        },
        "crew": [{"id": c.id, "base": c.base} for c in crew],
        "flights": [
            {
                "id": f.id, "flight_num": f.flight_num,
                "origin": f.origin, "dest": f.dest,
                "dep_min": f.dep_min, "arr_min": f.arr_min,
                "duration": f.duration, "min_crew": f.min_crew,
            }
            for f in cov_flights
        ],
        "routes": result.get("routes", []),
        "uncovered_flights": [
            {
                "flight_num": f.flight_num, "origin": f.origin, "dest": f.dest,
                "dep_min": f.dep_min, "arr_min": f.arr_min, "missing_slots": slots,
            }
            for f, slots in result.get("uncovered_flights", [])
        ],
    }

    fname = os.path.join(out_dir, f"result_{airline}_w{window_idx:02d}.json")
    with open(fname, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(f"  Saved → {fname}")
    return fname


# ─────────────────────────────────────────────────────────────────────────────
# PER-AIRLINE ROLLING-HORIZON SOLVER
# ─────────────────────────────────────────────────────────────────────────────

def solve_airline(
    airline: str,
    flights: list[Flight],
    coverage_days: int,
    out_dir: str = ".",
    verbose: bool = True,
) -> list[dict]:
    """
    Run full rolling-horizon DDD for one airline.

    Args:
        coverage_days: length of the planning period (flights with dep < coverage_days*1440
                       get coverage constraints). Windows roll across this period.
    Returns list of per-window result dicts.
    """
    import time as _t

    print(f"\n{'='*70}")
    print(f"AIRLINE: {airline}  |  {len(flights)} flights  |  coverage={coverage_days}d")
    print(f"{'='*70}")

    # Build windows across the coverage period (slide 20).
    # If coverage_days < T_DAYS_COMMIT we still run at least one window.
    effective_days = max(coverage_days, T_DAYS_COMMIT)
    windows = build_windows(effective_days)
    print(f"  Rolling horizon: {len(windows)} windows "
          f"(T_solve={T_DAYS_SOLVE}d, T_commit={T_DAYS_COMMIT}d, T_tail={T_DAYS_TAIL}d)")

    # Crew pool sized on all flights in the airline dataset
    crew = size_crew_bases(flights)

    carry_positions: dict[str, dict[str, int]] = {}
    carry_clocks: dict[int, ClockState] = {}
    all_results = []

    for win in windows:
        t0 = _t.time()
        f_win, f_cov = slice_flights(flights, win)

        if not f_cov:
            print(f"\n  Window {win.idx}: no coverage flights — skipping")
            continue

        print(f"\n  Window {win.idx}: {len(f_win)} total, {len(f_cov)} need coverage  "
              f"[t={win.t_start//1440}d – commit={win.t_commit//1440}d – hor={win.t_hor//1440}d]")

        net = CrewDDDNetwork(
            flights=f_win,
            cov_flights=f_cov,
            crew=crew,
            win=win,
            time_bucket=DELTA_BUCKET,
            carry_positions=carry_positions,
            carry_clocks=carry_clocks,
            verbose=verbose,
        )
        net.build_initial_network()
        net.build_model()
        result = net.solve()

        # Carry-over for next window (slides 22–23)
        carry_positions, carry_clocks = net.compute_carry_over()

        t1 = _t.time()
        cost_str = f"{result['cost']:,.1f}" if result.get('cost') is not None else "N/A"
        print(f"\n  Window {win.idx} summary: status={result['status']} "
              f"cost={cost_str}  "
              f"covered={result.get('covered_flights', 0)}/{result.get('num_flights', 0)}  "
              f"time={t1-t0:.1f}s")

        save_result(result, f_cov, crew, airline, win.idx, out_dir)
        all_results.append({"window": win.idx, **result})

    return all_results


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main(
    csv_path: str,
    days: int = 3,
    out_dir: str = ".",
    verbose: bool = True,
    airlines_filter: Optional[list[str]] = None,
):
    """
    Load flights → split by airline → solve each independently
    with a rolling-horizon DDD.

    Args:
        csv_path       : path to flights_enriched.csv
        days           : planning period (coverage obligation days)
        out_dir        : directory for JSON result files
        verbose        : print detailed DDD iteration info
        airlines_filter: if given, only solve these carriers (e.g. ['AA', 'DL'])
    """
    import time as _t
    os.makedirs(out_dir, exist_ok=True)
    t_global = _t.time()
    horizon_days = days + T_DAYS_TAIL

    print(f"Loading flights from {csv_path}...")
    print(f"  Planning period: {days} days  |  Horizon: {horizon_days} days")

    flights_by_airline, week_start = parse_flights_by_airline(
        csv_path, days=days, horizon_days=horizon_days
    )

    if airlines_filter:
        flights_by_airline = {
            al: fl for al, fl in flights_by_airline.items()
            if al in airlines_filter
        }

    print(f"\nWeek start : {week_start.date()}")
    print(f"Airlines   : {sorted(flights_by_airline)}")

    all_airline_results: dict[str, list[dict]] = {}

    for airline in sorted(flights_by_airline):
        flights = flights_by_airline[airline]
        if not flights:
            continue
        results = solve_airline(
            airline=airline,
            flights=flights,
            coverage_days=days,
            out_dir=out_dir,
            verbose=verbose,
        )
        all_airline_results[airline] = results

    t_end = _t.time()

    # ── Aggregate summary ──────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print("AGGREGATE SUMMARY")
    print(f"{'='*70}")
    print(f"Total wall time: {t_end - t_global:.1f}s")
    for airline, results in all_airline_results.items():
        total_cost    = sum(r.get('cost') or 0 for r in results)
        total_flights = sum(r.get('num_flights', 0) for r in results)
        covered       = sum(r.get('covered_flights', 0) for r in results)
        uncovered     = sum(r.get('uncovered_slots', 0) for r in results)
        print(f"  {airline:4s}: windows={len(results)}  "
              f"flights={total_flights}  covered={covered}  "
              f"uncovered_slots={uncovered:.1f}  cost={total_cost:,.1f}")

    return all_airline_results


if __name__ == "__main__":
    import sys, csv as _csv
    from collections import Counter as _Counter

    path = sys.argv[1] if len(sys.argv) > 1 else "data/flights_enriched.csv"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    # Auto-detect the airline with the fewest flights in the CSV (fastest to solve)
    with open(path, encoding="utf-8") as _f:
        _counts = _Counter(row["OP_CARRIER"].strip()
                           for row in _csv.DictReader(_f)
                           if row.get("OP_CARRIER", "").strip())
    smallest = _counts.most_common()[-1][0]
    print(f"Airline with fewest flights: {smallest} ({_counts[smallest]:,} rows) — running that only.")

    main(path, days=days, out_dir="results", verbose=True, airlines_filter=[smallest])