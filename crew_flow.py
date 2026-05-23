"""
Cabin Crew Pairing via Integer Flow + DDD
==========================================
Solves the minimum-cost crew pairing problem for US domestic flights.

Problem:
  - Every flight must be staffed with at least MIN_CABIN_CREW crew members
  - Crew start and end at their home base airport
  - Crew can deadhead (ride as passenger) to reposition
  - Crew wait at airports between flights (layover cost if overnight)
  - Minimise: flight-hour costs + deadhead costs + layover costs

Method:
  - Build a time-expanded network: nodes = (airport, time_bucket)
  - Arcs: flight | deadhead | wait
  - Variables: integer FLOW per arc (not per crew member)
      f_work[arc] = # crew working this arc
      f_dh[arc]   = # crew deadheading this arc
      f_wait[arc] = # crew waiting on this arc
  - Flow balance per base: supply = crew_count[base] at depot,
                           demand = crew_count[base] at horizon
  - Coverage: f_work[flight_arc] >= flight.min_crew
  - Deadhead = surplus flow above min_crew (priced separately)
  - Solve LP relaxation → check violations → refine → repeat (DDD)
  - Once LP converges, solve as MIP (integer flows)

Stage 2 (roster assignment):
  - After solving, decompose flows into individual crew routes
  - Assign named crew IDs to routes by base via flow decomposition
  - Each crew member gets one route starting and ending at their home base

Key advantages over individual-variable formulation:
  - Variables: O(|arcs|) instead of O(|crew| × |arcs|)
  - Constraints: O(|nodes|) instead of O(|crew| × |nodes|)
  - Crew identity restored in Stage 2 (flow decomposition) — trivial post-solve
  - Same optimality guarantees; same result file format

Data:
  - flights_enriched_copy.csv (BTS On-Time + FAA registry join)
  - Uses first 3 days, all airports as bases
"""

from __future__ import annotations
import csv
import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import gurobipy as gp
from gurobipy import GRB

# ─────────────────────────────────────────────
# CONFIGURATION  (identical to crew_ddd.py)
# ─────────────────────────────────────────────
DAYS_TO_SOLVE      = 3
RETURN_WINDOW      = 7
RANDOM_SEED        = 42
MIN_CREW_PER_BASE  = 5
MAX_CREW_PER_BASE  = 40
TIME_BUCKET        = 15
MIN_TURNAROUND     = 45
MIN_REST           = 8 * 60
OVERNIGHT_THRESHOLD = 4 * 60

COST_FLIGHT_HOUR   = 100.0
COST_DEADHEAD_BASE = 20.0
COST_LAYOVER_MIN   = 0.5
COST_OVERNIGHT     = 500.0
COST_UNCOVERED     = 1e7

MAX_DUTY_MINUTES   = 14 * 60
MAX_DUTY_DAYS      = 5

FARE_BASE          = 50.0
FARE_PER_MILE      = 0.15
LF_LOW_THRESHOLD   = 0.75
LF_HIGH_THRESHOLD  = 0.90

DEPOT_TIME_START   = 0
LARGE              = int(1e9)


# ─────────────────────────────────────────────
# DATA STRUCTURES  (unchanged from crew_ddd.py)
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class Flight:
    id: int
    origin: str
    dest: str
    dep_min: int
    arr_min: int
    duration: int
    min_crew: int
    flight_num: str
    distance: float = 0.0
    seats: float    = 150.0

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
    true_end: int
    cost: float
    arc_type: str       # 'flight' | 'deadhead' | 'wait'
    flight_id: int | None = None

    @property
    def is_wait(self):
        return self.arc_type == 'wait'

    def __hash__(self):
        return self.id

    def __eq__(self, other):
        return isinstance(other, Arc) and self.id == other.id


# ─────────────────────────────────────────────
# CSV PARSING  (unchanged)
# ─────────────────────────────────────────────

def parse_hhmm(s: str) -> int:
    s = s.strip().zfill(4)
    return int(s[:2]) * 60 + int(s[2:])

def parse_flights(filepath: str, days: int, horizon_days: int | None = None) -> tuple[list[Flight], datetime]:
    if horizon_days is None:
        horizon_days = days

    flights = []
    fid = 0
    week_start = None
    date_fmt_options = ["%m/%d/%Y %I:%M:%S %p", "%Y-%m-%d", "%m/%d/%Y"]

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
            if day_offset >= horizon_days or day_offset < 0:
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
                seats=float(row['SEATS_RESOLVED'].strip()) if row.get('SEATS_RESOLVED', '').strip() else 150.0,
            )
            object.__setattr__(flight, 'needs_coverage', day_offset < days)
            flights.append(flight)
            fid += 1

    if week_start is None:
        raise ValueError("No valid flights found in CSV.")

    n_cov  = sum(1 for f in flights if getattr(f, 'needs_coverage', True))
    n_repo = len(flights) - n_cov
    print(f"Loaded {len(flights)} flights from {week_start.date()}  "
          f"({n_cov} need coverage, {n_repo} return-window repositioning arcs)")
    return flights, week_start


# ─────────────────────────────────────────────
# CREW BASE ASSIGNMENT  (unchanged)
# ─────────────────────────────────────────────

def assign_crew_bases(flights: list[Flight], seed: int = RANDOM_SEED) -> list[CrewMember]:
    rng = random.Random(seed)
    airports = sorted(set(f.origin for f in flights) | set(f.dest for f in flights))

    demand_minutes: dict[str, float] = defaultdict(float)
    for f in flights:
        demand_minutes[f.origin] += f.min_crew * f.duration

    horizon_days = max(f.arr_min for f in flights) / 1440 if flights else 3
    duty_minutes_per_crew = 480 * horizon_days

    base_counts: dict[str, int] = {}
    for ap in airports:
        demand = demand_minutes.get(ap, 0)
        needed = math.ceil((demand / duty_minutes_per_crew) * 1.5) if demand > 0 else MIN_CREW_PER_BASE
        noisy = int(rng.gauss(needed, max(1, needed * 0.10)))
        base_counts[ap] = max(MIN_CREW_PER_BASE, noisy)

    crew_list: list[CrewMember] = []
    cid = 0
    for ap in airports:
        for _ in range(base_counts[ap]):
            crew_list.append(CrewMember(id=cid, base=ap))
            cid += 1

    total = len(crew_list)
    total_demand = sum(demand_minutes.values())
    total_available = sum(base_counts[ap] * duty_minutes_per_crew for ap in airports)
    print(f"Created {total:,} individual crew members across {len(airports)} bases")
    print(f"  Total crew-minutes needed:    {total_demand:,.0f}")
    print(f"  Available crew-minutes:       {total_available:,.0f}")
    print(f"  Coverage ratio:               {total_available / total_demand:.2f}x")
    return crew_list


# ─────────────────────────────────────────────
# COST HELPERS  (unchanged)
# ─────────────────────────────────────────────

def estimated_fare(distance_miles: float) -> float:
    return FARE_BASE + FARE_PER_MILE * max(0.0, distance_miles)

def opportunity_cost_scale(load_factor: float) -> float:
    if load_factor <= LF_LOW_THRESHOLD:
        return 0.0
    if load_factor >= LF_HIGH_THRESHOLD:
        return 1.0
    return (load_factor - LF_LOW_THRESHOLD) / (LF_HIGH_THRESHOLD - LF_LOW_THRESHOLD)

def deadhead_cost(flight: Flight, load_factor: float | None = None) -> float:
    lf = load_factor if load_factor is not None else 0.82
    base = flight.duration * COST_DEADHEAD_BASE
    fare = estimated_fare(flight.distance)
    opp  = fare * opportunity_cost_scale(lf)
    return base + opp


# ─────────────────────────────────────────────
# FLOW NETWORK
# ─────────────────────────────────────────────

class CrewFlowNetwork:
    """
    Time-expanded network for cabin crew pairing via integer flow + DDD.

    Variables (per arc, not per crew member):
      f_work[arc] ∈ ℤ≥0   number of working crew on this arc
      f_dh[arc]   ∈ ℤ≥0   number of deadheading crew on this arc
      f_wait[arc] ∈ ℤ≥0   number of waiting crew on this arc
      slack[flt]  ∈ ℤ≥0   uncovered crew slots (penalised)

    Flow balance per base b at each node n:
      ∑_{a out of n} flow[a]  ==  ∑_{a into n} flow[a]
      ... with supply = crew_count[b] injected at (b, t=0)
              demand = crew_count[b] absorbed at (b, t=horizon)
      Flow from other bases is blocked at their depot nodes.

    Coverage:
      f_work[flight_arc] + slack[flt] >= flt.min_crew

    Deadhead:
      f_dh[arc] >= 0   (priced at deadhead cost, not flight cost)
      Total flow on a flight arc = f_work + f_dh

    After solving, Stage 2 flow decomposition assigns named crew IDs.
    """

    def __init__(
        self,
        flights: list[Flight],
        crew: list[CrewMember],
        horizon_end: int,
        flight_end: int | None = None,
        time_bucket: int = TIME_BUCKET,
        verbose: bool = True,
    ):
        self.flights = flights
        self.flights_by_id = {f.id: f for f in flights}
        self.crew = crew
        self.crew_by_id = {c.id: c for c in crew}

        # crew count per base
        self.base_crew: dict[str, int] = defaultdict(int)
        for c in crew:
            self.base_crew[c.base] += 1
        self.airports = sorted(self.base_crew.keys())

        self.horizon_end = horizon_end
        self.flight_end  = flight_end if flight_end is not None else horizon_end
        self.time_bucket = time_bucket
        self.verbose     = verbose

        # Network topology
        self.nodes:             set[Node]              = set()
        self.nodes_by_airport:  dict[str, list[Node]]  = defaultdict(list)
        self.arcs:              set[Arc]               = set()
        self._arc_counter       = 0
        self.arcs_from:         dict[Node, list[Arc]]  = defaultdict(list)
        self.arcs_to:           dict[Node, list[Arc]]  = defaultdict(list)
        self.wait_arc_by_start: dict[Node, Arc]        = {}
        self._arcs_by_flight:   dict[int, list[Arc]]   = defaultdict(list)
        self.min_duty_at:       dict[Node, int]        = {}

        # Gurobi model
        self.model: gp.Model | None = None

        # Flow variables: (base, arc) -> Var
        # Keyed per base so flow balance can be enforced independently per base.
        # O(|bases| × |arcs|) — much smaller than O(|crew| × |arcs|) because
        # |bases| << |crew| (typically 4-20 bases vs hundreds of crew members).
        self.var_work: dict[tuple[str, Arc], gp.Var] = {}   # working crew
        self.var_dh:   dict[tuple[str, Arc], gp.Var] = {}   # deadheading crew
        self.var_wait: dict[tuple[str, Arc], gp.Var] = {}   # waiting crew
        self.slack_var: dict[int, gp.Var] = {}               # coverage slack per flight

        # Constraints
        self.flow_constrs:     dict[str, dict[Node, gp.Constr]] = {}  # base -> node -> constr
        self.coverage_constrs: dict[int, gp.Constr] = {}              # flight_id -> constr

    # ── Pickle compatibility ──────────────────
    CACHE_VERSION = 1
    _ATTR_DEFAULTS: dict = {'min_duty_at': {}, '_arcs_by_flight': {}}

    def __getstate__(self):
        state = self.__dict__.copy()
        state['_cache_version'] = self.CACHE_VERSION
        for key in ('model', 'var_work', 'var_dh', 'var_wait',
                    'slack_var', 'flow_constrs', 'coverage_constrs'):
            state.pop(key, None)
        return state
        return state

    def __setstate__(self, state):
        import copy
        for key in ('var_work', 'var_dh', 'var_wait', 'slack_var',
                    'flow_constrs', 'coverage_constrs'):
            state.setdefault(key, {})
        state.setdefault('model', None)
        loaded_version = state.pop('_cache_version', 0)
        for attr, default in self._ATTR_DEFAULTS.items():
            if attr not in state:
                state[attr] = copy.deepcopy(default)
        self.__dict__.update(state)

    def _ensure_attrs(self):
        import copy
        for attr, default in self._ATTR_DEFAULTS.items():
            if not hasattr(self, attr):
                setattr(self, attr, copy.deepcopy(default))

    # ── Node helpers ─────────────────────────

    def _find_node(self, airport: str, time: int, round_down=True) -> Node | None:
        import bisect
        nodes = self.nodes_by_airport[airport]
        if not nodes:
            return None
        times = [n.time for n in nodes]
        if round_down:
            idx = bisect.bisect_right(times, time) - 1
            return nodes[idx] if idx >= 0 else None
        else:
            idx = bisect.bisect_left(times, time)
            return nodes[idx] if idx < len(nodes) else None

    def _find_node_at_or_after(self, airport: str, time: int) -> Node | None:
        import bisect
        nodes = self.nodes_by_airport[airport]
        if not nodes:
            return None
        times = [n.time for n in nodes]
        idx = bisect.bisect_left(times, time)
        return nodes[idx] if idx < len(nodes) else None

    # ── Arc creation ─────────────────────────

    def _make_arc(self, start: Node, end: Node, true_end: int,
                  cost: float, arc_type: str, flight_id: int | None = None) -> Arc:
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
            if not hasattr(self, '_arcs_by_flight'):
                self._arcs_by_flight = defaultdict(list)
            self._arcs_by_flight[flight_id].append(arc)

        # If model exists, add variables and wire constraints
        if self.model is not None:
            self._add_arc_vars(arc)

        return arc

    def _remove_arc(self, arc: Arc):
        self.arcs.discard(arc)
        self.arcs_from[arc.start] = [a for a in self.arcs_from[arc.start] if a != arc]
        self.arcs_to[arc.end]     = [a for a in self.arcs_to[arc.end]     if a != arc]
        if self.model is not None:
            for base in self.airports:
                for d in (self.var_work, self.var_dh, self.var_wait):
                    key = (base, arc)
                    if key in d:
                        self.model.remove(d.pop(key))

    def _create_wait_arc(self, from_node: Node, to_node: Node) -> Arc:
        wait_minutes = to_node.time - from_node.time
        overnight = 1 if wait_minutes >= OVERNIGHT_THRESHOLD else 0
        cost = wait_minutes * COST_LAYOVER_MIN + overnight * COST_OVERNIGHT
        arc = self._make_arc(from_node, to_node, to_node.time, cost, 'wait')
        self.wait_arc_by_start[from_node] = arc
        return arc

    def _build_flight_index(self):
        self._flights_from: dict[str, list[Flight]] = defaultdict(list)
        self._flights_to:   dict[str, list[Flight]] = defaultdict(list)
        for f in self.flights:
            self._flights_from[f.origin].append(f)
            self._flights_to[f.dest].append(f)

    # ── Initial network ───────────────────────

    def build_initial_network(self):
        import bisect, time as _t
        self._build_flight_index()
        print("Building initial network...")
        t0 = _t.time()

        # 1. Depot + horizon nodes
        for ap in self.airports:
            for t in (DEPOT_TIME_START, self.horizon_end):
                node = Node(airport=ap, time=t)
                self.nodes.add(node)
                self.nodes_by_airport[ap].append(node)
        print(f"  Depot/horizon nodes: {len(self.nodes)}  ({_t.time()-t0:.1f}s)")

        # 2. One node per exact flight dep/arr time
        needed: dict[str, set[int]] = defaultdict(set)
        for f in self.flights:
            needed[f.origin].add(f.dep_min)
            needed[f.dest].add(f.arr_min)

        for ap, times in needed.items():
            for t in sorted(times):
                node = Node(airport=ap, time=t)
                if node in self.nodes:
                    continue
                self.nodes.add(node)
                tlist = [n.time for n in self.nodes_by_airport[ap]]
                idx = bisect.bisect_left(tlist, t)
                self.nodes_by_airport[ap].insert(idx, node)
        print(f"  All nodes added: {len(self.nodes)}  ({_t.time()-t0:.1f}s)")

        # 3. Wait-arc chains
        for ap in self.airports:
            nodes = self.nodes_by_airport[ap]
            for i in range(len(nodes) - 1):
                self._create_wait_arc(nodes[i], nodes[i + 1])
        print(f"  Wait arcs built  ({_t.time()-t0:.1f}s)")

        # 4. Flight + deadhead arcs
        for ap in self.airports:
            depot = Node(airport=ap, time=DEPOT_TIME_START)
            self.min_duty_at[depot] = 0

        n_arcs = 0
        n_missing = 0
        n_pruned_duty = 0
        for f in sorted(self.flights, key=lambda x: x.dep_min):
            dep_node = self._find_node(f.origin, f.dep_min)
            arr_node = self._find_node_at_or_after(f.dest, f.arr_min)
            if not (dep_node and arr_node and dep_node.time <= f.dep_min):
                n_missing += 1
                continue

            duty_at_dep = self.min_duty_at.get(dep_node, 0)
            duty_after  = duty_at_dep + f.duration
            if duty_after > MAX_DUTY_MINUTES:
                n_pruned_duty += 1
                continue

            existing = self.min_duty_at.get(arr_node, MAX_DUTY_MINUTES + 1)
            if duty_after < existing:
                self.min_duty_at[arr_node] = duty_after

            dh_cost = deadhead_cost(f, load_factor=None)
            self._make_arc(dep_node, arr_node, f.arr_min,
                           f.duration * COST_FLIGHT_HOUR, 'flight', f.id)
            self._make_arc(dep_node, arr_node, f.arr_min,
                           dh_cost, 'deadhead', f.id)
            n_arcs += 2

        if n_missing:
            print(f"  WARNING: {n_missing} flights have no connectable arc!")
        if n_pruned_duty:
            print(f"  Duty-pruned: {n_pruned_duty} flights exceeded MAX_DUTY_MINUTES")
        print(f"  Flight/deadhead arcs: {n_arcs} ({n_arcs//2} flights)  ({_t.time()-t0:.1f}s)")
        print(f"  Initial network: {len(self.nodes)} nodes, {len(self.arcs)} arcs  "
              f"(total {_t.time()-t0:.1f}s)")

    # ── Node refinement (DDD) ─────────────────

    def add_node(self, airport: str, time: int) -> Node:
        import bisect
        node = Node(airport=airport, time=time)
        if node in self.nodes:
            return node

        self.nodes.add(node)
        times = [n.time for n in self.nodes_by_airport[airport]]
        idx = bisect.bisect_left(times, time)
        self.nodes_by_airport[airport].insert(idx, node)

        if self.model is not None:
            self._add_flow_constr_for_node(node)

        self._rewire_wait_arcs(airport, node)
        self._create_flight_arcs_from(node)
        self._create_flight_arcs_to(node)
        return node

    def _rewire_wait_arcs(self, airport: str, new_node: Node):
        import bisect
        nodes = self.nodes_by_airport[airport]
        times = [n.time for n in nodes]
        pos = bisect.bisect_left(times, new_node.time)

        prev_node = nodes[pos - 1] if pos > 0 else None
        next_node = nodes[pos + 1] if pos + 1 < len(nodes) else None

        if prev_node and prev_node in self.wait_arc_by_start:
            old_arc = self.wait_arc_by_start[prev_node]
            if old_arc.true_end >= new_node.time:
                self._remove_arc(old_arc)
                del self.wait_arc_by_start[prev_node]
            self._create_wait_arc(prev_node, new_node)

        if next_node:
            self._create_wait_arc(new_node, next_node)
        else:
            sink = self._find_node(airport, self.horizon_end)
            if sink and sink != new_node:
                self._create_wait_arc(new_node, sink)

    def _create_flight_arcs_from(self, node: Node):
        duty_so_far = self.min_duty_at.get(node, 0)
        for f in self.flights:
            if f.origin != node.airport or f.dep_min < node.time:
                continue
            duty_after = duty_so_far + f.duration
            if duty_after > MAX_DUTY_MINUTES:
                continue
            arr_node = self._find_node(f.dest, f.arr_min)
            if arr_node is None:
                continue
            existing_duty = self.min_duty_at.get(arr_node, MAX_DUTY_MINUTES + 1)
            if duty_after < existing_duty:
                self.min_duty_at[arr_node] = duty_after
            dh_cost = deadhead_cost(f)
            self._make_arc(node, arr_node, f.arr_min,
                           f.duration * COST_FLIGHT_HOUR, 'flight', f.id)
            self._make_arc(node, arr_node, f.arr_min, dh_cost, 'deadhead', f.id)

    def _create_flight_arcs_to(self, node: Node):
        for f in self.flights:
            if f.dest != node.airport:
                continue
            dep_node = self._find_node(f.origin, f.dep_min)
            if dep_node is None or dep_node.time > f.dep_min:
                continue
            duty_at_dep = self.min_duty_at.get(dep_node, 0)
            duty_after  = duty_at_dep + f.duration
            if duty_after > MAX_DUTY_MINUTES:
                continue
            existing_duty = self.min_duty_at.get(node, MAX_DUTY_MINUTES + 1)
            if duty_after < existing_duty:
                self.min_duty_at[node] = duty_after
            dh_cost = deadhead_cost(f)
            self._make_arc(dep_node, node, f.arr_min,
                           f.duration * COST_FLIGHT_HOUR, 'flight', f.id)
            self._make_arc(dep_node, node, f.arr_min, dh_cost, 'deadhead', f.id)

    # ── Gurobi model ──────────────────────────

    def build_model(self):
        """
        Build the LP/MIP with one integer flow variable per arc (not per crew).

        The total number of variables is O(|arcs|), independent of crew count.
        Flow balance is enforced per base airport at each node.
        """
        import time as _t
        self._ensure_attrs()
        t0 = _t.time()

        self.model = gp.Model("CrewPairing_Flow")
        self.model.setParam("OutputFlag", 0)

        arc_list  = list(self.arcs)
        node_list = list(self.nodes)
        n_arcs    = len(arc_list)
        n_nodes   = len(node_list)

        print(f"  Building flow model: {n_arcs} arcs, {n_nodes} nodes, "
              f"{len(self.airports)} bases  ({_t.time()-t0:.1f}s)")

        # ── Per-base flow variables ───────────────────────────────────────
        # One set of (work, dh, wait) variables per BASE per arc.
        # O(|bases| × |arcs|) — dramatically smaller than O(|crew| × |arcs|)
        # because |bases| is typically 4-20 while |crew| can be hundreds.
        for base in self.airports:
            for arc in arc_list:
                if arc.arc_type == 'flight':
                    self.var_work[(base, arc)] = self.model.addVar(
                        lb=0, obj=arc.cost, vtype=GRB.CONTINUOUS,
                        name=f"fw_{base}_{arc.id}")
                    f_obj = self.flights_by_id.get(arc.flight_id)
                    dh_c  = deadhead_cost(f_obj) if f_obj else arc.cost
                    self.var_dh[(base, arc)] = self.model.addVar(
                        lb=0, obj=dh_c, vtype=GRB.CONTINUOUS,
                        name=f"fd_{base}_{arc.id}")
                elif arc.arc_type == 'deadhead':
                    self.var_dh[(base, arc)] = self.model.addVar(
                        lb=0, obj=arc.cost, vtype=GRB.CONTINUOUS,
                        name=f"fd_{base}_{arc.id}")
                elif arc.arc_type == 'wait':
                    self.var_wait[(base, arc)] = self.model.addVar(
                        lb=0, obj=arc.cost, vtype=GRB.CONTINUOUS,
                        name=f"wt_{base}_{arc.id}")

        # Slack variables for coverage
        for f in self.flights:
            if not getattr(f, 'needs_coverage', True):
                continue
            self.slack_var[f.id] = self.model.addVar(
                lb=0, ub=float(f.min_crew),
                obj=COST_UNCOVERED,
                vtype=GRB.CONTINUOUS,
                name=f"slack_{f.id}"
            )

        self.model.update()
        total_vars = (len(self.var_work) + len(self.var_dh) +
                      len(self.var_wait) + len(self.slack_var))
        print(f"  Variables: {total_vars:,}  ({_t.time()-t0:.1f}s)")

        # ── Flow balance: one constraint per BASE per NODE ────────────────
        # For each base b and each node n:
        #   sum_{a leaving n} flow_b[a]  ==  sum_{a entering n} flow_b[a]
        # with supply  = base_crew[b] at depot   node (b, t=0)
        #      demand  = base_crew[b] at horizon node (b, t=end)
        # This ensures exactly base_crew[b] crew depart from and return to base b.
        for base in self.airports:
            supply  = self.base_crew[base]
            depot   = Node(airport=base, time=DEPOT_TIME_START)
            horizon = Node(airport=base, time=self.horizon_end)
            self.flow_constrs[base] = {}

            for node in node_list:
                out_expr = self._base_flow_out(base, node)
                in_expr  = self._base_flow_in(base, node)

                if node == depot:
                    constr = self.model.addConstr(
                        out_expr - in_expr == supply,
                        name=f"flow_{base}_{node.airport}_{node.time}_depot"
                    )
                elif node == horizon:
                    constr = self.model.addConstr(
                        in_expr - out_expr == supply,
                        name=f"flow_{base}_{node.airport}_{node.time}_horizon"
                    )
                else:
                    constr = self.model.addConstr(
                        out_expr == in_expr,
                        name=f"flow_{base}_{node.airport}_{node.time}"
                    )
                self.flow_constrs[base][node] = constr

        self.model.update()
        print(f"  Flow balance constraints: {self.model.NumConstrs:,}  ({_t.time()-t0:.1f}s)")

        # Coverage: sum over all bases of working flow >= min_crew
        arcs_by_flight = getattr(self, '_arcs_by_flight', {})
        for f in self.flights:
            if not getattr(f, 'needs_coverage', True):
                continue
            flight_arcs = [a for a in arcs_by_flight.get(f.id, [])
                           if a.arc_type == 'flight']
            if not flight_arcs:
                continue
            coverage_expr = gp.quicksum(
                self.var_work[(base, arc)]
                for base in self.airports
                for arc in flight_arcs
                if (base, arc) in self.var_work
            )
            constr = self.model.addConstr(
                coverage_expr + self.slack_var[f.id] >= f.min_crew,
                name=f"cov_{f.id}"
            )
            self.coverage_constrs[f.id] = constr

        self.model.update()
        print(f"  Coverage constraints: {len(self.coverage_constrs)}  ({_t.time()-t0:.1f}s)")
        print(f"  Model built: {self.model.NumVars:,} vars, "
              f"{self.model.NumConstrs:,} constrs  ({_t.time()-t0:.1f}s)")

    def _flow_on_arc_for_base(self, base: str, arc: Arc) -> gp.LinExpr | float:
        """Total flow on arc for a given base (work + dh, or just wait)."""
        parts = []
        wk = self.var_work.get((base, arc))
        dh = self.var_dh.get((base, arc))
        wt = self.var_wait.get((base, arc))
        if wk is not None: parts.append(wk)
        if dh is not None: parts.append(dh)
        if wt is not None: parts.append(wt)
        return gp.quicksum(parts) if parts else 0.0

    def _flow_out_expr(self, node: Node) -> gp.LinExpr | float:
        """Total outflow across ALL bases (for coverage expressions)."""
        arcs = self.arcs_from.get(node, [])
        parts = []
        for arc in arcs:
            for base in self.airports:
                for d in (self.var_work, self.var_dh, self.var_wait):
                    v = d.get((base, arc))
                    if v is not None:
                        parts.append(v)
        return gp.quicksum(parts) if parts else 0.0

    def _flow_in_expr(self, node: Node) -> gp.LinExpr | float:
        """Total inflow across ALL bases (for coverage expressions)."""
        arcs = self.arcs_to.get(node, [])
        parts = []
        for arc in arcs:
            for base in self.airports:
                for d in (self.var_work, self.var_dh, self.var_wait):
                    v = d.get((base, arc))
                    if v is not None:
                        parts.append(v)
        return gp.quicksum(parts) if parts else 0.0

    def _base_flow_out(self, base: str, node: Node) -> gp.LinExpr | float:
        parts = []
        for arc in self.arcs_from.get(node, []):
            for d in (self.var_work, self.var_dh, self.var_wait):
                v = d.get((base, arc))
                if v is not None:
                    parts.append(v)
        return gp.quicksum(parts) if parts else 0.0

    def _base_flow_in(self, base: str, node: Node) -> gp.LinExpr | float:
        parts = []
        for arc in self.arcs_to.get(node, []):
            for d in (self.var_work, self.var_dh, self.var_wait):
                v = d.get((base, arc))
                if v is not None:
                    parts.append(v)
        return gp.quicksum(parts) if parts else 0.0

    def _add_arc_vars(self, arc: Arc):
        """Add per-base variables for a newly created arc during DDD refinement."""
        for base in self.airports:
            self._add_arc_vars_for_base(base, arc)

    def _add_arc_vars_for_base(self, base: str, arc: Arc):
        """Add variables for one base on one arc, wiring into existing constraints."""
        node_constrs = self.flow_constrs.get(base, {})

        if arc.arc_type == 'flight':
            wk = self.model.addVar(lb=0, obj=arc.cost, vtype=GRB.CONTINUOUS,
                                   name=f"fw_{base}_{arc.id}")
            self.var_work[(base, arc)] = wk
            f = self.flights_by_id.get(arc.flight_id)
            dh_c = deadhead_cost(f) if f else arc.cost
            dh = self.model.addVar(lb=0, obj=dh_c, vtype=GRB.CONTINUOUS,
                                   name=f"fd_{base}_{arc.id}")
            self.var_dh[(base, arc)] = dh
            for var in (wk, dh):
                if arc.start in node_constrs:
                    self.model.chgCoeff(node_constrs[arc.start], var, 1)
                if arc.end in node_constrs:
                    self.model.chgCoeff(node_constrs[arc.end], var, -1)
            # Wire work var into coverage
            if arc.flight_id in self.coverage_constrs:
                self.model.chgCoeff(self.coverage_constrs[arc.flight_id], wk, 1)

        elif arc.arc_type == 'deadhead':
            dh = self.model.addVar(lb=0, obj=arc.cost, vtype=GRB.CONTINUOUS,
                                   name=f"fd_{base}_{arc.id}")
            self.var_dh[(base, arc)] = dh
            if arc.start in node_constrs:
                self.model.chgCoeff(node_constrs[arc.start], dh, 1)
            if arc.end in node_constrs:
                self.model.chgCoeff(node_constrs[arc.end], dh, -1)

        elif arc.arc_type == 'wait':
            wt = self.model.addVar(lb=0, obj=arc.cost, vtype=GRB.CONTINUOUS,
                                   name=f"wt_{base}_{arc.id}")
            self.var_wait[(base, arc)] = wt
            if arc.start in node_constrs:
                self.model.chgCoeff(node_constrs[arc.start], wt, 1)
            if arc.end in node_constrs:
                self.model.chgCoeff(node_constrs[arc.end], wt, -1)

    def _add_flow_constr_for_node(self, node: Node):
        """Add per-base flow balance constraints for a new node (DDD refinement)."""
        for base in self.airports:
            supply  = self.base_crew[base]
            depot   = Node(airport=base, time=DEPOT_TIME_START)
            horizon = Node(airport=base, time=self.horizon_end)

            out_expr = self._base_flow_out(base, node)
            in_expr  = self._base_flow_in(base, node)

            if node == depot:
                constr = self.model.addConstr(out_expr - in_expr == supply,
                                              name=f"flow_{base}_{node.airport}_{node.time}")
            elif node == horizon:
                constr = self.model.addConstr(in_expr - out_expr == supply,
                                              name=f"flow_{base}_{node.airport}_{node.time}")
            else:
                constr = self.model.addConstr(out_expr == in_expr,
                                              name=f"flow_{base}_{node.airport}_{node.time}")

            if base not in self.flow_constrs:
                self.flow_constrs[base] = {}
            self.flow_constrs[base][node] = constr

    # ── Solve / DDD loop ──────────────────────

    def set_objective(self):
        obj = (
            gp.quicksum(arc.cost * v for (base, arc), v in self.var_work.items())
            + gp.quicksum(arc.cost * v for (base, arc), v in self.var_dh.items())
            + gp.quicksum(arc.cost * v for (base, arc), v in self.var_wait.items())
            + gp.quicksum(COST_UNCOVERED * v for v in self.slack_var.values())
        )
        self.model.setObjective(obj, GRB.MINIMIZE)

    def solve_lp(self) -> float:
        self.set_objective()
        self.model.optimize()
        if self.model.Status == GRB.OPTIMAL:
            slack_cost = sum(COST_UNCOVERED * var.X
                             for var in self.slack_var.values() if var.X > 1e-4)
            real_cost  = self.model.ObjVal - slack_cost
            uncovered  = slack_cost / COST_UNCOVERED
            if slack_cost > 0:
                print(f"      (real routing cost: {real_cost:,.0f}  |  "
                      f"uncovered slots: {uncovered:.0f}  |  slack cost: {slack_cost:,.0f})")
            return self.model.ObjVal
        return float('inf')

    def inspect_violations(self) -> list[tuple[str, int]]:
        """Check active arcs for time-window and turnaround violations."""
        violations = []
        eps = 1e-4

        active_arcs: set[Arc] = set()
        for (base, arc), var in {**self.var_work, **self.var_dh}.items():
            try:
                if var.X > eps:
                    active_arcs.add(arc)
            except AttributeError:
                pass

        for arc in active_arcs:
            true_t   = arc.true_end
            existing = self._find_node(arc.end.airport, true_t)
            if existing is None or abs(existing.time - true_t) > self.time_bucket:
                violations.append((arc.end.airport, true_t))

            for next_arc in self.arcs_from.get(arc.end, []):
                if next_arc not in active_arcs:
                    continue
                f = self.flights_by_id.get(next_arc.flight_id)
                if f and arc.true_end + MIN_TURNAROUND > f.dep_min:
                    violations.append((arc.end.airport, arc.true_end + MIN_TURNAROUND))

        result = []
        for ap, t in set(violations):
            existing = self._find_node(ap, t)
            if existing is None or existing.time != t:
                result.append((ap, t))
        return result

    def make_integer(self):
        """Switch all flow variables to integer."""
        for d in (self.var_work, self.var_dh, self.var_wait):
            for var in d.values():
                var.VType = GRB.INTEGER
        for var in self.slack_var.values():
            var.VType = GRB.INTEGER
        self.model.setParam("OutputFlag", 1)
        self.model.update()
    def solve(self, max_iter: int = 200, max_violations_per_iter: int = 500) -> dict:
        print("\n=== DDD Solve Loop (Flow) ===")
        self._ensure_attrs()
        solved = False
        prev_violations = None

        for iteration in range(max_iter):
            obj = self.solve_lp()
            print(f"  Iter {iteration:3d}: LP obj = {obj:,.1f}", end="")

            if self.model.Status != GRB.OPTIMAL:
                print(" [INFEASIBLE/UNBOUNDED — stopping]")
                break

            violations = self.inspect_violations()
            n_total = len(violations)
            print(f"  |  violations = {n_total}", end="")

            if not violations:
                print()
                print("  LP converged. Switching to MIP...")
                solved = True
                break

            if prev_violations is not None and n_total > prev_violations * 1.5:
                print(f"  [WARNING: violations growing {prev_violations}→{n_total}]", end="")
            prev_violations = n_total

            if n_total > max_violations_per_iter:
                violations = violations[:max_violations_per_iter]
                print(f"  [capped to {max_violations_per_iter}]", end="")

            print()
            for ap, t in violations:
                self.add_node(ap, t)
            self.model.update()

        if not solved:
            print("  Warning: DDD loop did not fully converge. Solving MIP on current network.")

        self.make_integer()
        self.set_objective()
        self.model.setParam("MIPGap", 0.01)
        self.model.setParam("TimeLimit", 300)
        self.model.optimize()

        return self.extract_solution()

    # ── Solution extraction ───────────────────

    def extract_solution(self) -> dict:
        eps = 1e-4
        if self.model.Status not in (GRB.OPTIMAL, GRB.TIME_LIMIT, GRB.SUBOPTIMAL):
            return {"status": "infeasible", "cost": None, "routes": [],
                    "uncovered_flights": [], "uncovered_slots": 0}

        obj = self.model.ObjVal

        # Uncovered flights
        uncovered = []
        for f in self.flights:
            if f.id in self.slack_var:
                sv = self.slack_var[f.id].X
                if sv > eps:
                    uncovered.append((f, sv))

        # Cost breakdown
        flight_cost  = sum(arc.cost * v.X
                           for (base, arc), v in self.var_work.items() if v.X > eps)
        dh_base_cost = 0.0
        dh_opp_cost  = 0.0
        for (base, arc), v in self.var_dh.items():
            val = v.X
            if val <= eps:
                continue
            f = self.flights_by_id.get(arc.flight_id)
            if f:
                base_c = f.duration * COST_DEADHEAD_BASE * val
                opp    = (arc.cost - f.duration * COST_DEADHEAD_BASE) * val
                dh_base_cost += base_c
                dh_opp_cost  += opp
            else:
                dh_base_cost += arc.cost * val
        wait_cost = sum(arc.cost * v.X
                        for (base, arc), v in self.var_wait.items() if v.X > eps)

        # Stage 2: decompose flows into named-crew routes
        routes = self._decompose_routes()

        return {
            "status": "optimal" if self.model.Status == GRB.OPTIMAL else "suboptimal",
            "cost": obj,
            "flight_cost": flight_cost,
            "deadhead_cost": dh_base_cost + dh_opp_cost,
            "deadhead_base_cost": dh_base_cost,
            "deadhead_opp_cost": dh_opp_cost,
            "wait_cost": wait_cost,
            "uncovered_slots": sum(v for _, v in uncovered),
            "uncovered_flights": uncovered,
            "routes": routes,
            "num_flights": len([f for f in self.flights
                                 if getattr(f, 'needs_coverage', True)]),
            "covered_flights": len([f for f in self.flights
                                     if getattr(f, 'needs_coverage', True)]) - len(uncovered),
        }

    # ── Stage 2: flow decomposition ───────────

    def _decompose_routes(self) -> list[dict]:
        """
        Decompose integer arc flows into individual crew routes.

        Algorithm:
          For each base b:
            Build a residual flow graph with integer capacities from solution.
            Repeatedly trace paths from depot(b) to horizon(b), each consuming
            1 unit of flow. Each path becomes one crew member's route.
            Assign named crew IDs from this base's crew pool.

        This recovers full individual routes — each crew member starts and ends
        at their home base. Crew identity is fully preserved post-solve.
        """
        eps = 0.5  # integer rounding threshold

        # Build residual capacity per base: (base, arc) -> remaining integer flow
        residual: dict[tuple[str, Arc], int] = {}
        for (base, arc), var in self.var_work.items():
            val = round(var.X)
            if val > 0:
                residual[(base, arc)] = residual.get((base, arc), 0) + val
        for (base, arc), var in self.var_dh.items():
            val = round(var.X)
            if val > 0:
                residual[(base, arc)] = residual.get((base, arc), 0) + val
        for (base, arc), var in self.var_wait.items():
            val = round(var.X)
            if val > 0:
                residual[(base, arc)] = residual.get((base, arc), 0) + val

        # Track which (base, arc) pairs are working vs deadheading for leg labels
        arc_is_work_for_base: set[tuple[str, int]] = set()  # (base, arc.id)
        for (base, arc), var in self.var_work.items():
            if round(var.X) > 0:
                arc_is_work_for_base.add((base, arc.id))

        routes = []

        for base in self.airports:
            depot   = Node(airport=base, time=DEPOT_TIME_START)
            horizon = Node(airport=base, time=self.horizon_end)
            supply  = self.base_crew[base]

            base_crew_ids = [c.id for c in self.crew if c.base == base]
            crew_idx = 0

            # Build a base-specific residual for path tracing
            base_residual: dict[Arc, int] = {}
            for (b, arc), cap in residual.items():
                if b == base and cap > 0:
                    base_residual[arc] = base_residual.get(arc, 0) + cap

            for _ in range(supply):
                path_arcs = self._trace_path(depot, horizon, base_residual)
                if not path_arcs:
                    break

                for arc in path_arcs:
                    base_residual[arc] -= 1
                    if base_residual[arc] == 0:
                        del base_residual[arc]

                legs = []
                for arc in path_arcs:
                    if arc.arc_type == 'wait':
                        continue
                    if (base, arc.id) in arc_is_work_for_base:
                        leg_type = 'flight'
                    else:
                        leg_type = 'deadhead'
                    legs.append({
                        "type":      leg_type,
                        "from":      arc.start.airport,
                        "to":        arc.end.airport,
                        "dep":       arc.start.time,
                        "arr":       arc.true_end,
                        "flight_id": arc.flight_id,
                    })

                if legs and crew_idx < len(base_crew_ids):
                    routes.append({
                        "crew_id":    base_crew_ids[crew_idx],
                        "base":       base,
                        "crew_count": 1,
                        "legs":       legs,
                    })
                    crew_idx += 1

        return routes

    def _trace_path(
        self,
        source: Node,
        sink: Node,
        residual: dict[Arc, int],
    ) -> list[Arc] | None:
        """
        Greedy DFS from source to sink through residual flow graph.
        Returns list of arcs forming one unit-flow path, or None if none exists.
        Prefers flight > deadhead > wait arcs to produce meaningful pairings.
        """
        type_priority = {'flight': 0, 'deadhead': 1, 'wait': 2}
        path = []
        visited: set[Node] = set()
        stack = [(source, [])]

        while stack:
            node, current_path = stack.pop()
            if node in visited:
                continue
            visited.add(node)

            if node == sink:
                return current_path

            # Candidate arcs with remaining flow, sorted by preference
            candidates = [
                a for a in self.arcs_from.get(node, [])
                if residual.get(a, 0) > 0 and a.end not in visited
            ]
            candidates.sort(key=lambda a: (type_priority.get(a.arc_type, 9), a.end.time))

            for arc in reversed(candidates):  # reversed so best goes on top of stack
                stack.append((arc.end, current_path + [arc]))

        return None


# ─────────────────────────────────────────────
# RESULT SERIALISATION  (identical to crew_ddd.py)
# ─────────────────────────────────────────────

def save_result(result: dict, net: CrewFlowNetwork, out_path: str = "crew_result.json"):
    import json

    routed_crew_ids = {r["crew_id"] for r in result.get("routes", [])}
    planning_flights = [f for f in net.flights if getattr(f, 'needs_coverage', True)]

    payload = {
        "meta": {
            "days": net.flight_end // 1440,
            "horizon_end": net.horizon_end,
            "solve_status": result.get("status", "unknown"),
            "total_cost": result.get("cost") or 0.0,
            "flight_cost": result.get("flight_cost", 0.0),
            "deadhead_cost": result.get("deadhead_cost", 0.0),
            "wait_cost": result.get("wait_cost", 0.0),
            "uncovered_slots": result.get("uncovered_slots", 0.0),
            "num_flights": result.get("num_flights", 0),
            "covered_flights": result.get("covered_flights", 0),
        },
        "crew": [
            {"id": c.id, "base": c.base}
            for c in net.crew
        ],
        "flights": [
            {
                "id": f.id,
                "flight_num": f.flight_num,
                "origin": f.origin,
                "dest": f.dest,
                "dep_min": f.dep_min,
                "arr_min": f.arr_min,
                "duration": f.duration,
                "min_crew": f.min_crew,
            }
            for f in planning_flights
        ],
        "routes": result.get("routes", []),
        "uncovered_flights": [
            {
                "flight_num": f.flight_num,
                "origin": f.origin,
                "dest": f.dest,
                "dep_min": f.dep_min,
                "arr_min": f.arr_min,
                "missing_slots": slots,
            }
            for f, slots in result.get("uncovered_flights", [])
            if getattr(f, 'needs_coverage', True)
        ],
    }

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)

    n_routed  = len(routed_crew_ids)
    n_sitting = len(net.crew) - n_routed
    print(f"\nResult saved to {out_path}")
    print(f"  {n_routed} crew with routes, {n_sitting} crew sitting at base")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def _network_cache_path(csv_path: str, days: int) -> str:
    import os
    base = os.path.splitext(os.path.basename(csv_path))[0]
    return f"{base}_d{days}_r{RETURN_WINDOW}_flow_network.pkl"


def main(csv_path: str, days: int = DAYS_TO_SOLVE, use_cache: bool = True):
    import time as _time
    import pickle, os

    t0 = _time.time()
    cache_path = _network_cache_path(csv_path, days)

    if use_cache and os.path.exists(cache_path):
        print(f"Loading cached network from {cache_path} ...")
        with open(cache_path, "rb") as fh:
            net = pickle.load(fh)
        print(f"  Loaded: {len(net.nodes)} nodes, {len(net.arcs)} arcs  "
              f"({_time.time()-t0:.1f}s)")
    else:
        horizon_days = days + RETURN_WINDOW
        flights, week_start = parse_flights(csv_path, days, horizon_days=horizon_days)
        if not flights:
            print("No flights loaded. Check CSV path and format.")
            return

        flight_end  = days * 1440
        horizon_end = flight_end + RETURN_WINDOW * 1440
        print(f"Flight window : day 1–{days}  |  Return deadline: day {days + RETURN_WINDOW}")

        crew = assign_crew_bases(flights)

        net = CrewFlowNetwork(
            flights=flights,
            crew=crew,
            horizon_end=horizon_end,
            flight_end=flight_end,
            time_bucket=TIME_BUCKET,
            verbose=True,
        )
        net.build_initial_network()

        if use_cache:
            print(f"Saving network to {cache_path} ...")
            with open(cache_path, "wb") as fh:
                pickle.dump(net, fh, protocol=pickle.HIGHEST_PROTOCOL)
            print(f"  Saved ({os.path.getsize(cache_path)/1e6:.1f} MB)")

    net.build_model()
    result = net.solve(max_iter=50)

    t1 = _time.time()

    print("\n" + "="*60)
    print("SOLUTION SUMMARY")
    print("="*60)
    print(f"Status          : {result['status']}")
    print(f"Total cost      : {result['cost']:,.1f}" if result['cost'] else "No solution")
    print(f"  Flight hours  : {result.get('flight_cost', 0):,.1f}")
    print(f"  Deadhead      : {result.get('deadhead_cost', 0):,.1f}"
          f"  (base: {result.get('deadhead_base_cost', 0):,.1f}"
          f"  |  opp.cost: {result.get('deadhead_opp_cost', 0):,.1f})")
    print(f"  Layover/wait  : {result.get('wait_cost', 0):,.1f}")
    print(f"Flights         : {result.get('num_flights', 0)}")
    print(f"Covered         : {result.get('covered_flights', 0)}")
    print(f"Uncovered slots : {result.get('uncovered_slots', 0):.1f}")
    print(f"Individual routes: {len(result.get('routes', []))}")
    print(f"Solve time      : {t1-t0:.1f}s")

    if result.get('uncovered_flights'):
        print(f"\nUncovered flights:")
        for f, slots in result['uncovered_flights']:
            print(f"  Flight {f.flight_num}: {f.origin}->{f.dest}  "
                  f"dep={f.dep_min//60:02d}:{f.dep_min%60:02d}  "
                  f"need {f.min_crew} crew, missing {slots:.1f}")

    for i, route in enumerate(result['routes'][:20]):  # print first 20
        print(f"\n  Route {i+1} | Crew #{route['crew_id']} | Base: {route['base']}")
        for leg in route['legs']:
            dep_h, dep_m = divmod(leg['dep'], 60)
            arr_h, arr_m = divmod(leg['arr'], 60)
            day_dep = dep_h // 24
            day_arr = arr_h // 24
            print(f"    [{leg['type']:8s}] {leg['from']} -> {leg['to']}  "
                  f"Day{day_dep+1} {dep_h%24:02d}:{dep_m:02d} -> "
                  f"Day{day_arr+1} {arr_h%24:02d}:{arr_m:02d}")

    import os
    result_path = os.path.splitext(csv_path)[0] + "_result.json"
    save_result(result, net, out_path=result_path)

    return result, net


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "data/flights_enriched.csv"
    main(path, days=DAYS_TO_SOLVE)
