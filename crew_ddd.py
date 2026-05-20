"""
Cabin Crew Pairing via Dynamic Discretisation Discovery (DDD)
=============================================================
Solves the minimum-cost crew pairing problem for US domestic flights.

Problem:
  - Every flight must be staffed with at least MIN_CABIN_CREW crew members
  - Crew start and end at their home base airport
  - Crew can deadhead (ride as passenger) to reposition
  - Crew wait at airports between flights (layover cost if overnight)
  - Minimise: flight-hour costs + deadhead costs + layover costs

Method (DDD):
  - Build a time-expanded network: nodes = (airport, time_bucket)
  - Arcs: flight | deadhead | wait | return-to-base
  - Solve LP relaxation → check time-window violations → refine network → repeat
  - Once LP converges, solve as MIP for integer assignment

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
# CONFIGURATION
# ─────────────────────────────────────────────
DAYS_TO_SOLVE = 3            # Planning horizon (days from week start)
RANDOM_SEED   = 42           # For reproducible crew pool sizes
MIN_CREW_PER_BASE = 5        # Minimum crew assigned to any base
MAX_CREW_PER_BASE = 40       # Maximum crew assigned to any base
TIME_BUCKET   = 15           # Minutes per time discretisation step (initial)
MIN_TURNAROUND = 45          # Min minutes between arriving and departing at same airport
MIN_REST      = 8 * 60       # Min rest before next duty (minutes) — 8 hours
OVERNIGHT_THRESHOLD = 4*60   # Layover longer than this = overnight (extra cost)

# Cost weights (arbitrary units — tune to taste)
COST_FLIGHT_HOUR   = 100.0   # per minute of flight time worked
COST_DEADHEAD      = 60.0    # per minute of deadhead flight
COST_LAYOVER_MIN   = 0.5     # per minute of layover
COST_OVERNIGHT     = 500.0   # flat penalty per overnight stay away from base
COST_UNCOVERED     = 1e7     # penalty per uncovered crew slot (infeasibility penalty)

DEPOT_TIME_START = 0         # Week starts at minute 0
LARGE = int(1e9)


# ─────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class Flight:
    id: int
    origin: str
    dest: str
    dep_min: int        # departure time in minutes from week start
    arr_min: int        # arrival time in minutes from week start
    duration: int       # flight time in minutes
    min_crew: int       # minimum cabin crew required
    flight_num: str

@dataclass(frozen=True)
class Node:
    airport: str
    time: int           # minutes from week start (discretised)

    def __lt__(self, other):
        return (self.time, self.airport) < (other.time, other.airport)

@dataclass
class Arc:
    id: int
    start: Node
    end: Node
    true_end: int       # actual time arc ends (before rounding to network node)
    cost: float
    arc_type: str       # 'flight' | 'deadhead' | 'wait' | 'return'
    flight_id: int | None = None  # for flight/deadhead arcs

    @property
    def is_wait(self):
        return self.arc_type == 'wait'

    def __hash__(self):
        return self.id

    def __eq__(self, other):
        return isinstance(other, Arc) and self.id == other.id


# ─────────────────────────────────────────────
# CSV PARSING
# ─────────────────────────────────────────────

def parse_hhmm(s: str) -> int:
    """Convert HHMM string to minutes since midnight."""
    s = s.strip().zfill(4)
    return int(s[:2]) * 60 + int(s[2:])

def parse_flights(filepath: str, days: int) -> tuple[list[Flight], datetime]:
    """
    Load flights from BTS CSV. 
    Returns (flight list, week_start datetime).
    Only loads non-cancelled flights within the first `days` days.
    """
    flights = []
    fid = 0
    week_start = None
    date_fmt_options = ["%m/%d/%Y %I:%M:%S %p", "%Y-%m-%d", "%m/%d/%Y"]

    with open(filepath, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Skip cancelled
            try:
                if float(row.get('CANCELLED', 0)) >= 1.0:
                    continue
            except ValueError:
                continue

            # Parse date
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
            if day_offset >= days or day_offset < 0:
                continue

            # Parse times
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

            # Handle overnight flights
            if arr_min_day < dep_min_day:
                arr_min = dep_min + int(elapsed)
            else:
                arr_min = day_offset * 1440 + arr_min_day

            # Sanity check
            if arr_min <= dep_min:
                arr_min = dep_min + max(1, int(elapsed))

            flights.append(Flight(
                id=fid,
                origin=row['ORIGIN'].strip(),
                dest=row['DEST'].strip(),
                dep_min=dep_min,
                arr_min=arr_min,
                duration=arr_min - dep_min,
                min_crew=max(1, min_crew),
                flight_num=row.get('OP_CARRIER_FL_NUM', str(fid)).strip(),
            ))
            fid += 1

    if week_start is None:
        raise ValueError("No valid flights found in CSV.")

    print(f"Loaded {len(flights)} flights over {days} days from {week_start.date()}")
    return flights, week_start


# ─────────────────────────────────────────────
# CREW BASE ASSIGNMENT
# ─────────────────────────────────────────────

def assign_crew_bases(flights: list[Flight], seed: int = RANDOM_SEED) -> dict[str, int]:
    """
    Assign a random crew pool to every airport that appears in the flight data.
    Returns {airport: crew_count}.
    """
    rng = random.Random(seed)
    airports = sorted(set(f.origin for f in flights) | set(f.dest for f in flights))
    # Busier airports get proportionally more crew
    dep_counts = defaultdict(int)
    for f in flights:
        dep_counts[f.origin] += 1

    max_deps = max(dep_counts.values()) if dep_counts else 1
    base_crew = {}
    for ap in airports:
        ratio = dep_counts.get(ap, 0) / max_deps
        # Scale between MIN and MAX, add noise
        mean = MIN_CREW_PER_BASE + ratio * (MAX_CREW_PER_BASE - MIN_CREW_PER_BASE)
        crew = int(rng.gauss(mean, mean * 0.15))
        crew = max(MIN_CREW_PER_BASE, min(MAX_CREW_PER_BASE, crew))
        base_crew[ap] = crew

    total = sum(base_crew.values())
    print(f"Assigned crew across {len(airports)} bases. Total pool: {total}")
    return base_crew


# ─────────────────────────────────────────────
# DDD NETWORK
# ─────────────────────────────────────────────

class CrewDDDNetwork:
    """
    Time-expanded network for cabin crew pairing via DDD.

    Nodes: (airport, time_bucket)
    Arc types:
      flight   — crew works this flight leg
      deadhead — crew rides as passenger on this flight leg
      wait     — crew waits at airport (may incur layover / overnight cost)
      return   — crew returns to base at end of horizon
    """

    def __init__(
        self,
        flights: list[Flight],
        base_crew: dict[str, int],
        horizon_end: int,
        time_bucket: int = TIME_BUCKET,
        verbose: bool = True,
    ):
        self.flights = flights
        self.flights_by_id = {f.id: f for f in flights}
        self.base_crew = base_crew
        self.airports = sorted(base_crew.keys())
        self.horizon_end = horizon_end
        self.time_bucket = time_bucket
        self.verbose = verbose

        # Nodes and arcs
        self.nodes: set[Node] = set()
        self.nodes_by_airport: dict[str, list[Node]] = defaultdict(list)  # sorted by time
        self.arcs: set[Arc] = set()
        self._arc_counter = 0
        self.arcs_from: dict[Node, list[Arc]] = defaultdict(list)
        self.arcs_to: dict[Node, list[Arc]] = defaultdict(list)
        # One waiting arc chain per airport — keyed by start node
        self.wait_arc_by_start: dict[Node, Arc] = {}

        # Gurobi model
        self.model: gp.Model | None = None
        self.arc_var: dict[Arc, gp.Var] = {}
        self.slack_var: dict[int, gp.Var] = {}   # per-flight uncovered slack

        # Constraint handles (for incremental updates)
        self.flow_constrs: dict[Node, gp.Constr] = {}
        self.coverage_constrs: dict[int, gp.Constr] = {}  # flight_id -> constr
        self.base_supply_constrs: dict[str, gp.Constr] = {}
        self.base_return_constrs: dict[str, gp.Constr] = {}

    # ── Node management ──────────────────────

    def _bucket(self, t: int) -> int:
        return (t // self.time_bucket) * self.time_bucket

    def _find_node(self, airport: str, time: int, round_down=True) -> Node | None:
        nodes = self.nodes_by_airport[airport]
        if not nodes:
            return None
        times = [n.time for n in nodes]
        import bisect
        idx = bisect.bisect_right(times, time) - 1
        if round_down:
            return nodes[idx] if idx >= 0 else None
        else:
            idx2 = bisect.bisect_left(times, time)
            return nodes[idx2] if idx2 < len(nodes) else None

    def _find_node_at_or_after(self, airport: str, time: int) -> Node | None:
        import bisect
        nodes = self.nodes_by_airport[airport]
        if not nodes:
            return None
        times = [n.time for n in nodes]
        idx = bisect.bisect_left(times, time)
        return nodes[idx] if idx < len(nodes) else None

    def add_node(self, airport: str, time: int) -> Node:
        """Add a node to the network, creating linking arcs."""
        import bisect
        node = Node(airport=airport, time=time)
        if node in self.nodes:
            return node

        self.nodes.add(node)
        times = [n.time for n in self.nodes_by_airport[airport]]
        idx = bisect.bisect_left(times, time)
        self.nodes_by_airport[airport].insert(idx, node)

        # Wire up flow balance if model exists
        if self.model is not None and node not in self.flow_constrs:
            self._add_flow_constr(node)

        self._rewire_wait_arcs(airport, node)
        self._create_flight_arcs_from(node)
        self._create_flight_arcs_to(node)
        return node

    def _rewire_wait_arcs(self, airport: str, new_node: Node):
        """
        Insert new_node into the waiting-arc chain for this airport.
        The chain ensures flow can idle at an airport across time.
        """
        import bisect
        nodes = self.nodes_by_airport[airport]
        times = [n.time for n in nodes]
        pos = bisect.bisect_left(times, new_node.time)

        prev_node = nodes[pos - 1] if pos > 0 else None
        next_node = nodes[pos + 1] if pos + 1 < len(nodes) else None

        # Remove any existing wait arc from prev_node that spans past new_node
        if prev_node and prev_node in self.wait_arc_by_start:
            old_arc = self.wait_arc_by_start[prev_node]
            if old_arc.true_end >= new_node.time:
                self._remove_arc(old_arc)
                del self.wait_arc_by_start[prev_node]
            # Create wait arc prev -> new
            self._create_wait_arc(prev_node, new_node)

        # Create wait arc new -> next (or to horizon end if no next)
        if next_node:
            self._create_wait_arc(new_node, next_node)
        else:
            # Sink to horizon
            sink = self._get_or_create_horizon_node(airport)
            if sink and sink != new_node:
                self._create_wait_arc(new_node, sink)

    def _get_or_create_horizon_node(self, airport: str) -> Node | None:
        """Return the terminal node at the horizon for this airport."""
        # Already exists?
        existing = self._find_node(airport, self.horizon_end)
        if existing and existing.time == self.horizon_end:
            return existing
        return None  # will be created during init

    def _create_wait_arc(self, from_node: Node, to_node: Node) -> Arc:
        wait_minutes = to_node.time - from_node.time
        overnight = 1 if wait_minutes >= OVERNIGHT_THRESHOLD else 0
        cost = wait_minutes * COST_LAYOVER_MIN + overnight * COST_OVERNIGHT
        arc = self._make_arc(from_node, to_node, to_node.time, cost, 'wait')
        self.wait_arc_by_start[from_node] = arc
        return arc

    def _create_flight_arcs_from(self, node: Node):
        """Create flight and deadhead arcs from this node for feasible flights."""
        for f in self.flights:
            if f.origin != node.airport:
                continue
            if f.dep_min < node.time:
                continue
            # Crew must have enough turnaround time to board
            if f.dep_min - node.time < 0:
                continue
            # Can crew get back to depot from dest?
            arr_node = self._find_node(f.dest, f.arr_min)
            if arr_node is None:
                continue
            # Flight arc
            self._make_arc(node, arr_node, f.arr_min,
                           f.duration * COST_FLIGHT_HOUR, 'flight', f.id)
            # Deadhead arc
            self._make_arc(node, arr_node, f.arr_min,
                           f.duration * COST_DEADHEAD, 'deadhead', f.id)

    def _create_flight_arcs_to(self, node: Node):
        """Create flight and deadhead arcs into this node for flights arriving here."""
        for f in self.flights:
            if f.dest != node.airport:
                continue
            # Find a departure node that could board this flight
            dep_node = self._find_node(f.origin, f.dep_min)
            if dep_node is None:
                continue
            if dep_node.time > f.dep_min:
                continue
            self._make_arc(dep_node, node, f.arr_min,
                           f.duration * COST_FLIGHT_HOUR, 'flight', f.id)
            self._make_arc(dep_node, node, f.arr_min,
                           f.duration * COST_DEADHEAD, 'deadhead', f.id)

    def _make_arc(self, start: Node, end: Node, true_end: int,
                  cost: float, arc_type: str, flight_id: int | None = None) -> Arc:
        # Deduplicate: don't add arc if identical one exists
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

        if self.model is not None:
            self._add_arc_var(arc)

        return arc

    def _remove_arc(self, arc: Arc):
        self.arcs.discard(arc)
        self.arcs_from[arc.start] = [a for a in self.arcs_from[arc.start] if a != arc]
        self.arcs_to[arc.end] = [a for a in self.arcs_to[arc.end] if a != arc]
        if self.model is not None and arc in self.arc_var:
            self.model.remove(self.arc_var[arc])
            del self.arc_var[arc]

    # ── Initial network ───────────────────────

    def _build_flight_index(self):
        """Pre-index flights by airport for O(1) lookup instead of O(N) scan."""
        self._flights_from: dict[str, list[Flight]] = defaultdict(list)
        self._flights_to:   dict[str, list[Flight]] = defaultdict(list)
        for f in self.flights:
            self._flights_from[f.origin].append(f)
            self._flights_to[f.dest].append(f)

    def build_initial_network(self):
        """
        Seed the network efficiently:
          - Depot (t=0) and horizon (t=end) nodes for every airport
          - One bucketed dep/arr node per flight, added in bulk (no per-node arc scan)
          - Wait-arc chains built in a single sorted pass
          - Flight + deadhead arcs created in one O(flights) pass using the index
        """
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

        # 2. Collect all bucketed times needed, add nodes in bulk
        needed: dict[str, set[int]] = defaultdict(set)
        for f in self.flights:
            needed[f.origin].add(self._bucket(f.dep_min))
            needed[f.dest].add(self._bucket(f.arr_min) + self.time_bucket)

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

        # 3. Build wait-arc chains in a single sorted pass per airport
        for ap in self.airports:
            nodes = self.nodes_by_airport[ap]
            for i in range(len(nodes) - 1):
                self._create_wait_arc(nodes[i], nodes[i + 1])
        print(f"  Wait arcs built  ({_t.time()-t0:.1f}s)")

        # 4. Flight + deadhead arcs — one pass, O(flights)
        n_arcs = 0
        for f in self.flights:
            dep_node = self._find_node(f.origin, f.dep_min)
            arr_node = self._find_node_at_or_after(f.dest, f.arr_min)
            if dep_node and arr_node:
                self._make_arc(dep_node, arr_node, f.arr_min,
                               f.duration * COST_FLIGHT_HOUR, 'flight', f.id)
                self._make_arc(dep_node, arr_node, f.arr_min,
                               f.duration * COST_DEADHEAD, 'deadhead', f.id)
                n_arcs += 2
        print(f"  Flight/deadhead arcs: {n_arcs}  ({_t.time()-t0:.1f}s)")
        print(f"  Initial network: {len(self.nodes)} nodes, {len(self.arcs)} arcs  "
              f"(total {_t.time()-t0:.1f}s)")

    # ── Gurobi model ─────────────────────────

    def build_model(self):
        self.model = gp.Model("CrewPairing_DDD")
        self.model.setParam("OutputFlag", 0)

        # Variables for each arc
        for arc in self.arcs:
            self._add_arc_var(arc)

        # Slack variables for each flight (infeasibility penalty)
        for f in self.flights:
            self.slack_var[f.id] = self.model.addVar(
                lb=0, ub=f.min_crew,
                obj=COST_UNCOVERED,
                vtype=GRB.CONTINUOUS,
                name=f"slack_{f.id}"
            )

        # Flow balance at every node
        for node in self.nodes:
            self._add_flow_constr(node)

        # Coverage: each flight must have min_crew crew assigned
        for f in self.flights:
            flight_arcs = [a for a in self.arcs
                           if a.flight_id == f.id and a.arc_type == 'flight']
            if flight_arcs:
                expr = gp.quicksum(self.arc_var[a] for a in flight_arcs)
                self.coverage_constrs[f.id] = self.model.addConstr(
                    expr + self.slack_var[f.id] >= f.min_crew,
                    name=f"cov_{f.id}"
                )

        # Base supply: at most base_crew[ap] crew leave base at time 0
        for ap in self.airports:
            start_node = Node(airport=ap, time=DEPOT_TIME_START)
            out_arcs = self.arcs_from.get(start_node, [])
            if out_arcs:
                expr = gp.quicksum(self.arc_var[a] for a in out_arcs)
                self.base_supply_constrs[ap] = self.model.addConstr(
                    expr <= self.base_crew[ap],
                    name=f"supply_{ap}"
                )

        self.model.update()
        print(f"  Model built: {self.model.NumVars} vars, {self.model.NumConstrs} constrs")

    def _add_arc_var(self, arc: Arc):
        var = self.model.addVar(
            lb=0,
            obj=arc.cost,
            vtype=GRB.CONTINUOUS,  # LP relaxation first
            name=f"arc_{arc.id}_{arc.arc_type}"
        )
        self.arc_var[arc] = var

        # Update flow balance constraints if they exist
        if arc.start in self.flow_constrs:
            self.model.chgCoeff(self.flow_constrs[arc.start], var, -1)
        if arc.end in self.flow_constrs:
            self.model.chgCoeff(self.flow_constrs[arc.end], var, 1)

        # Update coverage constraint
        if arc.flight_id is not None and arc.arc_type == 'flight':
            if arc.flight_id in self.coverage_constrs:
                self.model.chgCoeff(self.coverage_constrs[arc.flight_id], var, 1)

        # Update base supply constraint
        if arc.start.time == DEPOT_TIME_START and arc.start.airport in self.base_supply_constrs:
            self.model.chgCoeff(self.base_supply_constrs[arc.start.airport], var, 1)

    def _add_flow_constr(self, node: Node):
        in_arcs  = self.arcs_to.get(node, [])
        out_arcs = self.arcs_from.get(node, [])
        in_expr  = gp.quicksum(self.arc_var[a] for a in in_arcs  if a in self.arc_var)
        out_expr = gp.quicksum(self.arc_var[a] for a in out_arcs if a in self.arc_var)

        # Depot start nodes: out - in = supply (net outflow = crew count)
        # Horizon end nodes: in - out = supply (net inflow = crew count)
        # All others: in = out (flow conservation)
        if node.time == DEPOT_TIME_START:
            constr = self.model.addConstr(out_expr - in_expr >= 0, name=f"flow_{node.airport}_{node.time}")
        elif node.time == self.horizon_end:
            constr = self.model.addConstr(in_expr - out_expr >= 0, name=f"flow_{node.airport}_{node.time}")
        else:
            constr = self.model.addConstr(in_expr == out_expr, name=f"flow_{node.airport}_{node.time}")

        self.flow_constrs[node] = constr

    def set_objective(self):
        self.model.setObjective(
            gp.quicksum(arc.cost * self.arc_var[arc] for arc in self.arcs if arc in self.arc_var)
            + gp.quicksum(COST_UNCOVERED * self.slack_var[f.id] for f in self.flights),
            GRB.MINIMIZE
        )

    # ── Solve / DDD loop ──────────────────────

    def solve_lp(self) -> float:
        self.set_objective()
        self.model.optimize()
        if self.model.Status == GRB.OPTIMAL:
            return self.model.ObjVal
        return float('inf')

    def inspect_violations(self) -> list[tuple[str, int]]:
        """
        Trace active flight arcs through their true times.
        Returns (airport, true_time) pairs where the network needs a finer node.

        A violation occurs when:
        (a) The arc's true arrival time is not yet a node in the network
            (network is too coarse — solver can "teleport" crew in time), OR
        (b) Two consecutive active arcs would require a turnaround that
            the current discretisation allows but reality does not.
        """
        violations = []
        eps = 1e-4

        active_flight_arcs = [
            arc for arc in self.arcs
            if arc.arc_type in ('flight', 'deadhead')
            and arc in self.arc_var
        ]

        for arc in active_flight_arcs:
            try:
                val = self.arc_var[arc].X
            except AttributeError:
                continue
            if val < eps:
                continue

            # Violation (a): true arrival not represented as a node
            true_ap = arc.end.airport
            true_t  = arc.true_end
            existing = self._find_node(true_ap, true_t)
            # If no node exists at true_end, or closest node is > time_bucket away
            if existing is None or abs(existing.time - true_t) > self.time_bucket:
                violations.append((true_ap, true_t))

            # Violation (b): turnaround infeasibility with next active arc
            for next_arc in self.arcs_from.get(arc.end, []):
                if next_arc.arc_type not in ('flight', 'deadhead'):
                    continue
                if next_arc not in self.arc_var:
                    continue
                try:
                    nval = self.arc_var[next_arc].X
                except AttributeError:
                    continue
                if nval < eps:
                    continue
                f = self.flights_by_id.get(next_arc.flight_id)
                if f and arc.true_end + MIN_TURNAROUND > f.dep_min:
                    # Need a node that separates these two arcs properly
                    violations.append((arc.end.airport, arc.true_end + MIN_TURNAROUND))

        # Deduplicate and filter out nodes already in network at exact time
        result = []
        for ap, t in set(violations):
            existing = self._find_node(ap, t)
            if existing is None or existing.time != t:
                result.append((ap, t))
        return result

    def make_integer(self):
        """Switch all variables to integer/binary."""
        for var in self.arc_var.values():
            var.VType = GRB.INTEGER
        for var in self.slack_var.values():
            var.VType = GRB.INTEGER
        self.model.setParam("OutputFlag", 1)
        self.model.update()

    def solve(self, max_iter: int = 200) -> dict:
        """
        Main DDD solve loop.
        1. Solve LP relaxation
        2. Check violations → add nodes
        3. Repeat until no violations
        4. Solve MIP
        """
        print("\n=== DDD Solve Loop ===")
        solved = False
        for iteration in range(max_iter):
            obj = self.solve_lp()
            print(f"  Iter {iteration:3d}: LP obj = {obj:,.1f}", end="")

            if self.model.Status != GRB.OPTIMAL:
                print(" [INFEASIBLE/UNBOUNDED — stopping]")
                break

            violations = self.inspect_violations()
            print(f"  |  violations = {len(violations)}")

            if not violations:
                print("  LP converged. Switching to MIP...")
                solved = True
                break

            # Add refinement nodes
            for ap, t in violations:
                self.add_node(ap, t)
            self.model.update()

        if not solved:
            print("  Warning: DDD loop did not fully converge. Solving MIP on current network.")

        # MIP solve
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
            return {"status": "infeasible", "cost": None, "routes": [], "uncovered": []}

        obj = self.model.ObjVal

        # Collect active arcs
        active: dict[Arc, float] = {}
        for arc, var in self.arc_var.items():
            try:
                if var.X > eps:
                    active[arc] = var.X
            except AttributeError:
                pass

        # Uncovered flights
        uncovered = []
        for f in self.flights:
            if f.id in self.slack_var:
                try:
                    sv = self.slack_var[f.id].X
                    if sv > eps:
                        uncovered.append((f, sv))
                except AttributeError:
                    pass

        # Cost breakdown
        flight_cost = sum(arc.cost * v for arc, v in active.items() if arc.arc_type == 'flight')
        dh_cost     = sum(arc.cost * v for arc, v in active.items() if arc.arc_type == 'deadhead')
        wait_cost   = sum(arc.cost * v for arc, v in active.items() if arc.arc_type == 'wait')

        # Simple route extraction: trace chains from depot nodes
        routes = self._trace_routes(active)

        return {
            "status": "optimal" if self.model.Status == GRB.OPTIMAL else "suboptimal",
            "cost": obj,
            "flight_cost": flight_cost,
            "deadhead_cost": dh_cost,
            "wait_cost": wait_cost,
            "uncovered_slots": sum(v for _, v in uncovered),
            "uncovered_flights": uncovered,
            "routes": routes,
            "num_flights": len(self.flights),
            "covered_flights": len(self.flights) - len(uncovered),
        }

    def _trace_routes(self, active: dict[Arc, float]) -> list[dict]:
        """
        Decompose active flow into individual crew routes using greedy path extraction.
        Starts from depot nodes, follows highest-flow non-wait arcs.
        """
        routes = []
        eps = 1e-4

        # Mutable residual flow
        residual = {arc: flow for arc, flow in active.items() if flow > eps}

        def pop_best_arc(node: Node) -> tuple[Arc, float] | None:
            candidates = [
                (arc, residual[arc])
                for arc in self.arcs_from.get(node, [])
                if arc in residual and residual[arc] > eps and arc.arc_type != 'wait'
            ]
            if not candidates:
                return None
            best = max(candidates, key=lambda x: x[1])
            return best

        # Gather depot start nodes that have outflow
        depot_nodes = sorted(
            [n for n in self.nodes if n.time == DEPOT_TIME_START],
            key=lambda n: n.airport
        )

        for depot_node in depot_nodes:
            while True:
                result = pop_best_arc(depot_node)
                if result is None:
                    break
                first_arc, flow = result
                residual[first_arc] = residual.get(first_arc, 0) - flow
                if residual[first_arc] <= eps:
                    del residual[first_arc]

                route = {
                    "base": depot_node.airport,
                    "legs": [],
                    "crew_count": max(1, round(flow)),
                }
                route["legs"].append({
                    "type": first_arc.arc_type,
                    "from": first_arc.start.airport,
                    "to": first_arc.end.airport,
                    "dep": first_arc.start.time,
                    "arr": first_arc.true_end,
                    "flight_id": first_arc.flight_id,
                })
                curr = first_arc.end

                for _ in range(200):
                    if curr.time >= self.horizon_end:
                        break
                    nxt = pop_best_arc(curr)
                    if nxt is None:
                        break
                    arc, f = nxt
                    residual[arc] = residual.get(arc, 0) - f
                    if residual[arc] <= eps:
                        del residual[arc]
                    route["legs"].append({
                        "type": arc.arc_type,
                        "from": arc.start.airport,
                        "to": arc.end.airport,
                        "dep": arc.start.time,
                        "arr": arc.true_end,
                        "flight_id": arc.flight_id,
                    })
                    curr = arc.end

                if route["legs"]:
                    routes.append(route)

        return routes


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def _network_cache_path(csv_path: str, days: int) -> str:
    """Return a cache filename based on the input file and day count."""
    import hashlib, os
    base = os.path.splitext(os.path.basename(csv_path))[0]
    return f"{base}_d{days}_network.pkl"


def main(csv_path: str, days: int = DAYS_TO_SOLVE, use_cache: bool = True):
    import time as _time
    import pickle, os

    t0 = _time.time()

    cache_path = _network_cache_path(csv_path, days)

    # ── Try loading cached network ──────────
    if use_cache and os.path.exists(cache_path):
        print(f"Loading cached network from {cache_path} ...")
        with open(cache_path, "rb") as fh:
            net = pickle.load(fh)
        print(f"  Loaded: {len(net.nodes)} nodes, {len(net.arcs)} arcs  "
              f"({_time.time()-t0:.1f}s)")
    else:
        # 1. Load data
        flights, week_start = parse_flights(csv_path, days)
        if not flights:
            print("No flights loaded. Check CSV path and format.")
            return

        horizon_end = days * 1440  # minutes

        # 2. Assign crew
        base_crew = assign_crew_bases(flights)

        # 3. Build network
        net = CrewDDDNetwork(
            flights=flights,
            base_crew=base_crew,
            horizon_end=horizon_end,
            time_bucket=TIME_BUCKET,
            verbose=True,
        )
        net.build_initial_network()

        # Save network (without Gurobi model — it can't be pickled)
        if use_cache:
            print(f"Saving network to {cache_path} ...")
            with open(cache_path, "wb") as fh:
                pickle.dump(net, fh, protocol=pickle.HIGHEST_PROTOCOL)
            print(f"  Saved ({os.path.getsize(cache_path)/1e6:.1f} MB)")

    # 4. Build LP model (always rebuilt — Gurobi objects aren't serialisable)
    net.build_model()

    # 5. Solve (DDD + MIP)
    result = net.solve(max_iter=50)

    t1 = _time.time()

    # 6. Report
    print("\n" + "="*60)
    print("SOLUTION SUMMARY")
    print("="*60)
    print(f"Status          : {result['status']}")
    print(f"Total cost      : {result['cost']:,.1f}" if result['cost'] else "No solution")
    print(f"  Flight hours  : {result.get('flight_cost', 0):,.1f}")
    print(f"  Deadhead      : {result.get('deadhead_cost', 0):,.1f}")
    print(f"  Layover/wait  : {result.get('wait_cost', 0):,.1f}")
    print(f"Flights         : {result.get('num_flights', 0)}")
    print(f"Covered         : {result.get('covered_flights', 0)}")
    print(f"Uncovered slots : {result.get('uncovered_slots', 0):.1f}")
    print(f"Routes extracted: {len(result.get('routes', []))}")
    print(f"Solve time      : {t1-t0:.1f}s")

    if result.get('uncovered_flights'):
        print(f"\nUncovered flights (first 10):")
        for f, slots in result['uncovered_flights'][:10]:
            print(f"  Flight {f.flight_num}: {f.origin}->{f.dest}  "
                  f"dep={f.dep_min//60:02d}:{f.dep_min%60:02d}  "
                  f"need {f.min_crew} crew, missing {slots:.1f}")

    print("\nSample routes (first 5):")
    for i, route in enumerate(result['routes'][:5]):
        print(f"\n  Route {i+1} | Base: {route['base']} | ~{route['crew_count']} crew")
        for leg in route['legs']:
            dep_h, dep_m = divmod(leg['dep'], 60)
            arr_h, arr_m = divmod(leg['arr'], 60)
            day_dep = dep_h // 24
            day_arr = arr_h // 24
            print(f"    [{leg['type']:8s}] {leg['from']} -> {leg['to']}  "
                  f"Day{day_dep+1} {dep_h%24:02d}:{dep_m:02d} -> "
                  f"Day{day_arr+1} {arr_h%24:02d}:{arr_m:02d}")

    return result, net


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "data/flights_enriched.csv"
    main(path, days=DAYS_TO_SOLVE)
