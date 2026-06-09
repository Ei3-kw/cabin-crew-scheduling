"""
Cabin Crew Pairing via Dynamic Discretisation Discovery (DDD)
=============================================================
Implements the formulation from slides_copy_2.pdf exactly.

Key additions vs original:
  - Per-airline planning: flights split by OP_CARRIER; solver runs independently
    for each airline (separate crew pools, networks, and models).
  - Rolling horizon: windows W of length T_days=7 with T_commit=3 committed days
    and T_tail=7 return tail; carry-over of crew positions and clock states.
    Each window's depot is at t=win.t_start (not always 0) so the network truly
    rolls forward — no crew teleport back to minute 0 between windows.
  - No-teleport crew continuity: carry_positions are unpacked into per-crew start
    airports so a crew member who ended window w at airport K starts window w+1
    at K, not at their home base.  Flow balance sources/sinks are set accordingly.
  - Reachability pruning: Fwd/Bwd Dijkstra from depot/horizon, arcs violating
    d_work or d_away excluded.
  - Turnaround snapping: arrival end of flight arc snapped to earliest node at
    dest satisfying the MIN_TURNAROUND gap (slide 17).
  - Home-break clocks: per-crew state (t_reset, d_work, h_home) carried forward
    across windows (slide 23).
  - Bases: every airport with flights both TO and FROM in the chosen airline
    is a full crew base; there is no separate satellite class.
  - Cost constants aligned with slide 10 exactly.

Sets & notation (slide 8–12):
  A      All airports
  B ⊆ A  Crew home bases (= every airport with flights to & from)
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
  s_min=3, s_max=120
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

# Away-from-base limit — defined here (ahead of the duty/rest block) because the
# rolling-horizon tail is derived from it. A crew may be away from its home base for
# at most D_AWAY days before it must return ("return window").
D_AWAY      = 4              # max days away from home base
DELTA_AWAY  = D_AWAY * 1440  # in minutes

# Rolling horizon (slide 20)
T_DAYS_SOLVE   = 7    # solve window length in days
T_DAYS_COMMIT  = 3    # days committed per step
# Return tail: the horizon must reach far enough past the solve window that a crew
# departing at the END of the solve window can still be routed home within its
# away-limit. That required distance is exactly D_AWAY (a later departure has no
# claim on this window — the next window owns it). So tail = D_AWAY, plus a 1-day
# buffer for time-bucket snapping and backward-reachability slack.
#   horizon = (T_SOLVE + T_TAIL) days = 7 + 5 = 12  (was a hand-set 7 -> 14)
TAIL_BUFFER_DAYS = 1
T_DAYS_TAIL    = D_AWAY + TAIL_BUFFER_DAYS   # = 5
T_LOOKAHEAD    = T_DAYS_SOLVE - T_DAYS_COMMIT  # = 4 days overlap

# Crew limits (slide 9)
S_MIN = 3             # minimum crew at any base 3
S_MAX = 120           # maximum crew at any single base (raised to allow ORD to be
                      # correctly staffed: ORD carries ~49% of all flights)
RANDOM_SEED = 42

# ── Optional: randomized minimum cabin crew ──────────────────────────────────
# When RANDOM_MIN_CREW is True, the CSV's MIN_CABIN_CREW (the FAA 14 CFR 121.391
# value derived from seat count) is IGNORED and the minimum crew per flight is drawn
# at random instead. The draw is CONSTANT for a given flight number (per airline)
# across all of its occurrences — i.e. the same scheduled flight on different days
# always carries the same crew requirement — and is fully reproducible via
# RANDOM_SEED. Inclusive bounds.
RANDOM_MIN_CREW      = False   # toggle: draw min crew randomly instead of from CSV
RANDOM_MIN_CREW_MIN  = 1       # inclusive lower bound for the draw
RANDOM_MIN_CREW_MAX  = 3       # inclusive upper bound for the draw

# Crew base assignment (slide 12)
MIN_CREW_PER_BASE = S_MIN              # minimum crew at any base (alias of S_MIN)

# Crew utilisation discount for tau_duty (slide 12 denominator).
# The raw formula tau = 8h * horizon_days assumes each crew works 8h every day,
# but home-break (48h), consecutive-day limits (D_WORK=3), and turnaround rest
# (DELTA_REST=8h) mean realistic utilisation is ~55% of theoretical maximum.
# Without this discount, ORD (49% of all flights) gets sized at ~17 crew when it
# needs ~60-70 to satisfy concurrent demand with legal rest intervals.
CREW_UTILISATION = 0.55

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
D_WORK      = 3       # max consecutive duty days
# D_AWAY / DELTA_AWAY are defined above the rolling-horizon block (the return tail
# is derived from D_AWAY).


C_UNC_EFFECTIVE = 10**8


# DDD solver (slide 11)
DELTA_BUCKET = 15     # initial time-bucket (min)
DDD_MAX_VIOLATIONS = 500
DDD_MAX_ITER = 200

# Opportunity cost model
FARE_BASE      = 50.0
FARE_PER_MILE  = 0.15
LF_LOW  = 0.75
LF_HIGH = 0.90

LARGE = int(1e9)

# Overnight threshold (4 h = 240 min)
OVERNIGHT_THRESHOLD = 4 * 60

# Home-return deadhead discount.
# A deadhead arc whose destination is the crew's home base is discounted by this
# fraction relative to the standard deadhead cost.  This incentivises regional /
# regional crew (e.g. ALO, ATW) to route home rather than be sent to yet another
# outstation — without changing the hard constraints.  Set to 0.0 to disable.
C_DH_HOME_RETURN_DISCOUNT = 0.30   # 30 % cheaper to deadhead home


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
    t_reset: int            = 0      # last reset time (min from week start)
    d_work: int             = 0      # consecutive work-days since last rest
    h_home: int             = 0      # home-wait accumulated since last departure (min)
    t_last_home_return: int = -LARGE # absolute minute of last arrival at home base;
                                     # -LARGE means never returned (no prior break needed)
    away_since: int = -LARGE         # absolute minute the crew last departed home (start
                                     # of the current away spell); -LARGE = at/based home
    home_break_until: int = -LARGE   # if the last COMPLETED away-trip reached the D_AWAY
                                     # cap, the crew owes a 48h home break until this
                                     # minute; -LARGE = no break owed (short trips)


# ─────────────────────────────────────────────────────────────────────────────
# CSV PARSING  (now carries airline column)
# ─────────────────────────────────────────────────────────────────────────────

def parse_hhmm(s: str) -> int:
    s = s.strip().zfill(4)
    return int(s[:2]) * 60 + int(s[2:])


# Cache so every occurrence of the same (airline, flight number) gets the SAME draw.
_RANDOM_MIN_CREW_CACHE: dict[tuple[str, str], int] = {}

def random_min_crew(airline: str, flight_num: str) -> int:
    """Random minimum cabin crew for a flight, held CONSTANT across every occurrence
    of the same (airline, flight number) — the same scheduled flight on different days
    always returns the same value. Deterministic / reproducible via RANDOM_SEED: the
    draw is keyed on (seed, airline, flight number), so it does not depend on CSV row
    order or on how many times the flight appears. Bounds come from
    RANDOM_MIN_CREW_MIN / RANDOM_MIN_CREW_MAX (inclusive)."""
    key = (airline, flight_num)
    val = _RANDOM_MIN_CREW_CACHE.get(key)
    if val is None:
        rng = random.Random(f"{RANDOM_SEED}|{airline}|{flight_num}")
        val = rng.randint(RANDOM_MIN_CREW_MIN, RANDOM_MIN_CREW_MAX)
        _RANDOM_MIN_CREW_CACHE[key] = val
    return val


def parse_flights_by_airline(
    filepath: str,
    days: int,
    horizon_days: Optional[int] = None,
    random_min_crew_override: Optional[bool] = None,
) -> tuple[dict[str, list[Flight]], datetime]:
    """
    Load flights grouped by operating carrier (OP_CARRIER).

    min_crew source:
        - default (random_min_crew_override is None): use the module flag
          RANDOM_MIN_CREW. When False, read MIN_CABIN_CREW from the CSV (FAA
          121.391). When True, draw a random value that is constant per
          (airline, flight number) via random_min_crew().
        - pass random_min_crew_override=True/False to force it for this call.

    Returns:
        flights_by_airline : {airline_code: [Flight, ...]}
        week_start         : datetime of the first flight date
    """
    use_random = RANDOM_MIN_CREW if random_min_crew_override is None else random_min_crew_override
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
                # min_crew from CSV only when not randomizing; otherwise drawn below
                # so the MIN_CABIN_CREW column is not required for randomized runs.
                min_crew = 1 if use_random else int(float(row['MIN_CABIN_CREW'].strip()))
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
            flight_num = row.get('OP_CARRIER_FL_NUM', str(fid)).strip()

            # Randomized min crew: constant per (airline, flight number) across days.
            if use_random:
                min_crew = random_min_crew(airline, flight_num)

            flight = Flight(
                id=fid,
                origin=row['ORIGIN'].strip(),
                dest=row['DEST'].strip(),
                dep_min=dep_min,
                arr_min=arr_min,
                duration=arr_min - dep_min,
                min_crew=max(1, min_crew),
                flight_num=flight_num,
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
# CREW BASE SIZING  (slide 12)
# n_p = max(n_min, ceil( (Σ_{f: f.orig=p} m_f * d_f / τ_duty) * 1.5 + noise ))
# ─────────────────────────────────────────────────────────────────────────────

def assign_crew_bases(
    flights: list[Flight],
    seed: int = RANDOM_SEED,
) -> list[CrewMember]:
    """
    Build the crew pool.

    Base set B: every airport the chosen airline operates flights both TO and
    FROM (i.e. it appears as an origin AND as a destination).  There is no
    separate satellite / hub distinction any more — every such airport is a full
    crew base.

    Per-base crew count:
        n = max(S_MIN, min(S_MAX, ceil(max(n_demand, n_peak) * 1.5) + noise))
      n_demand = (Sum_{f: orig=p} m_f * d_f) / tau_duty   (duration-weighted demand)
      n_peak   = max simultaneous crew load over the horizon (peak concurrent)
      tau_duty = 8h/day * horizon_days * CREW_UTILISATION
    """
    rng = random.Random(seed)

    origins = set(f.origin for f in flights)
    dests   = set(f.dest   for f in flights)
    # A base is any airport with flights both to AND from it in this airline.
    bases = sorted(origins & dests)

    # Demand:  Sum_{f: orig=p} m_f * d_f   (slide 12 numerator)
    demand_minutes: dict[str, float] = defaultdict(float)
    for f in flights:
        demand_minutes[f.origin] += f.min_crew * f.duration

    # Peak concurrent crew demand per airport (dep = +crew, arr = -crew sweep).
    # NOTE: deltas are weighted by m_f, so peak already reflects multi-crew flights.
    events_by_ap: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for f in flights:
        events_by_ap[f.origin].append((f.dep_min, +f.min_crew))
        events_by_ap[f.origin].append((f.arr_min, -f.min_crew))
    peak_concurrent: dict[str, int] = defaultdict(int)
    for ap, evts in events_by_ap.items():
        cur = pk = 0
        for _, delta in sorted(evts):
            cur += delta
            pk = max(pk, cur)
        peak_concurrent[ap] = pk

    # Co-location floor: the m_f crew on a single flight must ALL be at the same
    # airport at the same instant and depart together.  A base therefore must be
    # able to field at least max(m_f) crew simultaneously to launch its largest
    # originating flight.  This is the constraint multi-crew adds that pure volume
    # scaling misses: with the seat -> FA rule, a station serving a 200-seat jet
    # needs >=4 co-located crew, while a 50-seat regional spoke needs only 1.
    coloc_floor: dict[str, int] = defaultdict(int)
    for f in flights:
        coloc_floor[f.origin] = max(coloc_floor[f.origin], f.min_crew)

    horizon_days = max(f.arr_min for f in flights) / 1440 if flights else 3
    tau_duty = 8 * 60 * max(1.0, horizon_days) * CREW_UTILISATION

    base_counts: dict[str, int] = {}
    for ap in bases:
        demand   = demand_minutes.get(ap, 0.0)
        peak     = peak_concurrent.get(ap, 0)
        n_demand = math.ceil((demand / tau_duty) * 1.5) if demand > 0 else 0
        n_peak   = math.ceil(peak * 1.5)
        # Rotation-aware floor.  A self-sustaining base needs ~MIN_CREW_PER_BASE
        # rotation "slots" (one crewing a flight, others resting / returning home),
        # and EACH slot must be filled by max(m_f) crew because that many fly
        # together on the largest flight.  So the floor scales with crew-per-flight:
        #   min_crew=1 -> 3   (identical to the original flat floor)
        #   min_crew=2 -> 6,  min_crew=4 -> 12, ...
        # This is what makes the count respond at LOW-VOLUME spokes, where n_demand
        # and n_peak both fall at/under the old flat 3 and a doubling of m_f was
        # otherwise invisible.  (n_peak already captures the instantaneous
        # co-location spike; this captures the sustained throughput it misses.)
        floor    = MIN_CREW_PER_BASE * coloc_floor.get(ap, 1)
        needed   = max(n_demand, n_peak, floor)
        noisy    = int(rng.gauss(needed, max(1, needed * 0.10)))
        base_counts[ap] = max(floor, min(S_MAX, noisy))

    # ── Build CrewMember list ─────────────────────────────────────────────────
    crew_list: list[CrewMember] = []
    cid = 0
    for ap in bases:
        for _ in range(base_counts[ap]):
            crew_list.append(CrewMember(id=cid, base=ap))
            cid += 1

    # ── Diagnostics ───────────────────────────────────────────────────────────
    total           = len(crew_list)
    total_demand    = sum(demand_minutes.values())
    total_available = sum(base_counts[ap] * tau_duty for ap in bases)
    print(f"  Created {total:,} crew at {len(bases)} bases "
          f"(every airport with flights to & from in this airline)")
    print(f"  Utilisation discount      :  {CREW_UTILISATION:.0%}  "
          f"(tau_duty = {tau_duty/60:.0f}h per crew over {horizon_days:.0f}d horizon)")
    print(f"  Total crew-minutes needed :  {total_demand:,.0f}")
    print(f"  Available crew-minutes    :  {total_available:,.0f}")
    print(f"  Coverage ratio            :  {total_available / max(1, total_demand):.2f}x")

    top_bases = sorted(base_counts, key=lambda x: -base_counts[x])[:10]
    print(f"  Top bases by crew count:")
    for ap in top_bases:
        print(f"    {ap}: {base_counts[ap]} crew  "
              f"(peak concurrent={peak_concurrent.get(ap, 0)}, "
              f"demand={demand_minutes.get(ap, 0):,.0f} min)")

    return crew_list


# Backwards-compatible alias so existing call sites need no changes.
size_crew_bases = assign_crew_bases


# ─────────────────────────────────────────────────────────────────────────────
# OPPORTUNITY COST MODEL  (slide 10: c_dh = 20/min + opp_cost)
# ─────────────────────────────────────────────────────────────────────────────

def _opp_cost_scale(lf: float) -> float:
    if lf <= LF_LOW:
        return 0.0
    if lf >= LF_HIGH:
        return 1.0
    return (lf - LF_LOW) / (LF_HIGH - LF_LOW)


def deadhead_cost(f: Flight, home_base: str = "") -> float:
    """c_dh^a for deadhead arc on flight f (slide 10).

    If home_base is provided and f.dest == home_base, apply a cost discount so
    that the solver prefers routing regional / spoke crew home over sending
    them to yet another outstation.  The discount is purely a cost signal; the
    hard flow-balance and d_away constraints remain unchanged.
    """
    base = f.duration * C_DH
    fare = FARE_BASE + FARE_PER_MILE * max(0.0, f.distance)
    opp  = fare * _opp_cost_scale(f.load_factor)
    cost = base + opp
    if home_base and f.dest == home_base:
        cost *= (1.0 - C_DH_HOME_RETURN_DISCOUNT)
    return cost


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
    base_airport: str = "",
    init_dwork: int = 0,
    init_t_last_home: int = -1,
) -> set[Node]:
    """
    R_b^w = Fwd_b(G_w) ∩ Bwd_b(G_w)  (slide 18).
    Fwd: Dijkstra from depot nodes.
    Bwd: Dijkstra backwards from horizon nodes.
    Clock state (d_work, t_since_home) tracked; arcs that violate limits pruned.

    d_away is tracked as time elapsed since the crew last touched their home
    base airport, not as elapsed time from the current node.  This means a crew
    who returns home mid-route resets their away clock to zero, which is the
    correct semantics for the D_AWAY=4d constraint.

    init_dwork        : carry-over consecutive work-days at window start.
    init_t_last_home  : absolute minute of last home-base touch before this window
                        (-1 means crew started this window at home, clock resets to
                        depot time).  Used to correctly consume the D_AWAY budget
                        for crew who are away at the start of the window.
    """
    INF = float('inf')
    node_set = set(nodes)

    def dijkstra_fwd(sources: list[Node]) -> set[Node]:
        dist = {n: INF for n in node_set}
        # State: (cost, d_work_DAYS, t_last_home, last_fly_day, node)
        #   d_work_DAYS  : consecutive DUTY DAYS since the last home touch
        #   last_fly_day : calendar day (//1440) of the most recent flight, or -1
        # FIX: d_work counts DUTY DAYS, not flight legs.  Several flights on the
        # same calendar day are ONE duty day (this matches compute_carry_over,
        # which counts distinct departure days).  The previous version did
        # `new_dw = dw + 1` per flight arc, so a regional crew flying 3-4 short
        # legs in a single day hit D_WORK within that one day and had the rest of
        # its day (and onward connections) pruned -- a major source of stranding.
        pq = []
        for s in sources:
            dist[s] = 0.0
            # Honour carry-over away clock.
            if not base_airport or s.airport == base_airport or init_t_last_home < 0:
                t_last_home = s.time
            else:
                t_last_home = init_t_last_home
            heapq.heappush(pq, (0.0, init_dwork, t_last_home, -1, s))
        visited: dict[Node, tuple] = {}
        while pq:
            d, dw, tlh, lfd, u = heapq.heappop(pq)
            if u in visited:
                continue
            visited[u] = (dw, tlh, lfd)
            for arc in arcs_from.get(u, []):
                v = arc.end
                if v not in node_set:
                    continue
                # Reset away clock when crew returns to home base
                if base_airport and arc.end.airport == base_airport:
                    new_tlh = arc.true_end
                else:
                    new_tlh = tlh

                # d_work: consecutive DUTY DAYS (reset on a home wait/touch)
                new_lfd = lfd
                if arc.arc_type == 'flight':
                    dep_day = arc.start.time // 1440
                    if dep_day != lfd:
                        new_dw = dw + 1          # first flight of a new calendar day
                        new_lfd = dep_day
                    else:
                        new_dw = dw              # same day -> same duty day
                elif (arc.arc_type == 'wait' and base_airport
                      and arc.end.airport == base_airport):
                    new_dw = 0                   # rest at home resets the streak
                    new_lfd = -1
                else:
                    new_dw = dw
                if new_dw > D_WORK:
                    continue

                # d_away: time since last home touch must not exceed DELTA_AWAY.
                #
                # FIX (stranding away from base): the old prune dropped EVERY
                # flight/deadhead arc once time_away exceeded the cap — including the
                # arc that would carry the crew HOME.  A crew that drifted even one
                # leg over the limit therefore had no legal move and was parked at a
                # spoke for days (e.g. 10.5d at MLI), which both wastes the crew and
                # shows up as a d_away violation that never resolves until a later
                # window happens to open a path.
                #
                # We now keep any arc whose destination IS the home base reachable
                # regardless of time_away, so a crew is ALWAYS able to route home
                # (arriving at most one leg late).  Arcs that travel further away are
                # still pruned, which bounds the overage to a single home-bound leg.
                time_away = arc.true_end - new_tlh
                if (arc.arc_type in ('flight', 'deadhead')
                        and time_away > DELTA_AWAY
                        and arc.end.airport != base_airport):
                    continue

                nd = d + arc.cost
                if nd < dist.get(v, INF):
                    dist[v] = nd
                    heapq.heappush(pq, (nd, new_dw, new_tlh, new_lfd, v))
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
        carry_crew_pos: Optional[dict[int, str]] = None,
        verbose: bool = True,
    ):
        self.flights    = flights
        self.cov_set    = {f.id for f in cov_flights}
        self.cov_flights = cov_flights
        self.crew       = crew
        self.crew_by_id = {c.id: c for c in crew}
        self.win        = win
        self.horizon_end = win.t_hor
        self.depot_start = win.t_start   # window-relative depot (rolls with each window)
        self.time_bucket = time_bucket
        self.verbose    = verbose

        # Carry-over from previous window (slide 22–23)
        self.carry_positions: dict[str, dict[str, int]] = carry_positions or {}
        self.carry_clocks: dict[int, ClockState] = carry_clocks or {}

        # Per-crew start airport at win.t_start (derived from carry_positions).
        # crew_start_airport[c.id] = airport where crew c begins this window.
        # For window 0 (or no carry-over), crew start at their home base.
        # This prevents teleportation: a crew member who ended the previous window
        # at airport K will start the new window at K, not at their home base.
        #
        # Fix 2: When carry_crew_pos is provided (per-crew-id map returned by
        # compute_carry_over), use it directly instead of the queue-based unpacking
        # below.  The queue approach was non-deterministic: if crew 171 and crew 200
        # are both based at ORD but ended at SGF and ORD respectively, the queue
        # assigned positions by enumeration order within the base group rather than
        # by crew ID — so crew 171 could silently receive ORD and crew 200 SGF,
        # causing the "teleport" the viewer reports as "not CTS".
        self.crew_start_airport: dict[int, str] = {}
        if carry_crew_pos:
            # Authoritative per-ID map: use directly with home-base fallback
            for c in crew:
                self.crew_start_airport[c.id] = carry_crew_pos.get(c.id, c.base)
        elif carry_positions:
            # Legacy fallback: queue-based unpacking (kept for backwards compat
            # with any call site that doesn't pass carry_crew_pos).
            # NOTE: this path is only reached when carry_crew_pos is None, which
            # should not happen when solve_airline calls compute_carry_over.
            base_pos_queues: dict[str, list[str]] = {}
            for base, pos_counts in carry_positions.items():
                q: list[str] = []
                for airport, cnt in pos_counts.items():
                    q.extend([airport] * int(cnt))
                base_pos_queues[base] = q
            base_pos_idx: dict[str, int] = defaultdict(int)
            for c in crew:
                q = base_pos_queues.get(c.base, [])
                idx = base_pos_idx[c.base]
                if idx < len(q):
                    self.crew_start_airport[c.id] = q[idx]
                    base_pos_idx[c.base] = idx + 1
                else:
                    self.crew_start_airport[c.id] = c.base
        else:
            for c in crew:
                self.crew_start_airport[c.id] = c.base

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
        # Warm start: routes from the previous (overlapping) window, used as a
        # Gurobi MIP start.  Consecutive windows commit 3 days but solve 7, so they
        # share most flights and the prior solution is a strong incumbent.
        self._warm_start_routes: list[dict] = []

    def set_warm_start(self, routes: list[dict]):
        """Provide the previous window's routes to seed this window's MIP start."""
        self._warm_start_routes = routes or []

    def _apply_warm_start(self):
        """Seed Var.Start from the previous window's flight/deadhead assignments.

        Matched by (crew_id, arc_type, flight_id) — flight_id is the stable global
        Flight.id, so a crew that flew flight X last window is pointed at the same
        flight arc here if X is still in this window's horizon.  This is a PARTIAL
        start (only flight/deadhead arcs); Gurobi completes the wait arcs.  Any
        assignment that doesn't fit this window (crew now starts elsewhere) is
        simply ignored by Gurobi, so a stale start can never make the model wrong.
        """
        if not self._warm_start_routes:
            return
        n_set = 0
        for r in self._warm_start_routes:
            cid = r.get("crew_id")
            cvars = self.arc_var.get(cid)
            if not cvars:
                continue
            lut = {}
            for arc, var in cvars.items():
                if arc.flight_id is not None:
                    lut[(arc.arc_type, arc.flight_id)] = var
            for leg in r.get("legs", []):
                fid = leg.get("flight_id")
                if fid is None:
                    continue
                var = lut.get((leg.get("type"), fid))
                if var is not None:
                    var.Start = 1.0
                    n_set += 1
        if n_set:
            self.model.update()
            if self.verbose:
                print(f"  Warm start: seeded {n_set} flight/deadhead "
                      f"assignments from the previous window")

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

        # 1. Depot (t=depot_start) and horizon nodes (slide 14).
        #    Use self.depot_start = win.t_start so the depot rolls with the window.
        #    Each crew member gets a depot node at their actual start airport
        #    (which may differ from home base if carry-over places them elsewhere).
        #    Horizon nodes are always at the crew's home base so return-home flow
        #    is enforced over the full window.
        base_airports = set(c.base for c in self.crew)
        start_airports = set(self.crew_start_airport.values())
        for ap in base_airports | start_airports:
            self._add_node_sorted(Node(ap, self.depot_start))
            self._add_node_sorted(Node(ap, self.horizon_end))

        # 2. Insert event nodes for every flight (slide 14).
        #    Origin gets a departure-time node so flight arcs can depart from it.
        #    Destination gets an arrival-time node so _snap_arrival has something
        #    to snap to — it searches for the first node at dest with
        #    time >= arr_min + DELTA_TA.
        #
        #    Previous code inserted Node(f.dest, f.dep_min) — the departure time
        #    at the *destination* — which is semantically wrong.  For flights whose
        #    arr_min + 45 fell after every such dep-time node at the dest airport,
        #    _snap_arrival returned None and the arc was silently dropped, making
        #    those flights structurally uncoverable regardless of crew supply.
        #
        #    We also insert the snapped node (arr_min + DELTA_TA) directly so that
        #    _snap_arrival always finds an exact match on first call and the wait-arc
        #    chain is already present for it.  This node sits in the wait-arc chain
        #    because step 3 builds arcs between consecutive sorted nodes, so there
        #    is always a path forward to the horizon.
        for f in self.flights:
            self._add_node_sorted(Node(f.origin, f.dep_min))
            self._add_node_sorted(Node(f.dest,   f.arr_min))
            # Also pre-insert the turnaround-snapped node so snap_arrival hits exactly.
            snap_t = f.arr_min + DELTA_TA
            if snap_t <= self.horizon_end:
                self._add_node_sorted(Node(f.dest, snap_t))

        if self.verbose:
            print(f"    Nodes after dep/arr: {len(self.nodes)}  ({_t.time()-t0:.1f}s)")

        # 3. Wait arcs: chain consecutive nodes at each airport (slide 15).
        #    Every airport's timeline now ends at its horizon node (for bases)
        #    so wait arcs fully connect each node to the horizon.
        base_airports_set = set(c.base for c in self.crew)
        for ap in self.nodes_by_airport:
            ap_nodes = self.nodes_by_airport[ap]
            home_base_here = ap if ap in base_airports_set else ""
            for i in range(len(ap_nodes) - 1):
                self._make_wait_arc(ap_nodes[i], ap_nodes[i+1], home_base=home_base_here)

        if self.verbose:
            print(f"    Wait arcs built  ({_t.time()-t0:.1f}s)")

        # 4. Initialise duty clocks at depots
        for ap in self.nodes_by_airport:
            depot_node = Node(ap, self.depot_start)
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

    def _make_wait_arc(self, frm: Node, to: Node, home_base: str = "") -> Arc:
        dt = to.time - frm.time
        if home_base and frm.airport == home_base:
            # Home wait is free: mandatory rest at base carries no per-diem or
            # overnight penalty.  This removes cost pressure that would otherwise
            # discourage the solver from routing crew home at all.
            cost = 0.0
        else:
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

        Home-spine wait arcs (both endpoints at the crew's home airport, or at their
        start airport for this window) bypass the reachability check entirely.
        They form the depot->...->horizon chain every crew member must traverse.
        """
        if arc.arc_type == 'wait':
            ap = arc.start.airport
            if ap == crew.base or ap == self.crew_start_airport.get(crew.id, crew.base):
                return True
        return arc in self._base_reachable_arcs.get(crew.id, set())

    def _compute_base_reachability(self):
        """
        Compute reachable arc sets, one per distinct crew CLOCK-GROUP rather than
        one per base.

        FIX (stranding): the previous version computed a single reachable set per
        base using the WORST-CASE d_work across all crew at that base
        (max_dwork = max over C_b).  That poisoned fresh, home-based crew with a
        stranded member's carry-over clock: a base with even one crew at
        d_work = D_WORK had its entire reachable arc set pruned as if every crew
        were maxed out, so the fresh crew could not be deployed and silently
        dropped out of every later window.

        Now each crew member's reachability is computed from THEIR OWN start
        airport and carry-over clock (init_dwork, init_t_last_home).  Crew sharing
        an identical (base, start_airport, init_dwork, init_t_last_home) signature
        share a single Dijkstra, so the call count stays close to |B| in the
        common case (most crew start at home with a zero clock).

        self._base_reachable_arcs is now keyed by CREW ID (not base).
        """
        import time as _t
        t0 = _t.time()
        self._base_reachable_arcs: dict[int, set[Arc]] = {}   # keyed by crew.id

        node_list   = list(self.nodes)
        arcs_from_d = dict(self.arcs_from)
        arcs_to_d   = dict(self.arcs_to)

        group_cache: dict[tuple, set[Arc]] = {}
        n_dijkstra = 0
        for c in self.crew:
            start_ap = self.crew_start_airport.get(c.id, c.base)
            clock    = self.carry_clocks.get(c.id, ClockState())
            init_dwork = clock.d_work
            # Away clock only matters if the crew actually starts away from home.
            if start_ap != c.base and clock.t_last_home_return >= 0:
                init_tlh = clock.t_last_home_return
            else:
                init_tlh = -1
            key = (c.base, start_ap, init_dwork, init_tlh)

            cached = group_cache.get(key)
            if cached is not None:
                self._base_reachable_arcs[c.id] = cached
                continue

            depot_node   = Node(start_ap, self.depot_start)
            horizon_node = Node(c.base, self.horizon_end)
            if depot_node not in self.nodes or horizon_node not in self.nodes:
                group_cache[key] = set()
                self._base_reachable_arcs[c.id] = set()
                continue

            reachable_nodes = compute_reachable(
                nodes=node_list,
                arcs_from=arcs_from_d,
                arcs_to=arcs_to_d,
                depot_nodes=[depot_node],
                horizon_nodes=[horizon_node],
                t_hor=self.horizon_end,
                base_airport=c.base,
                init_dwork=init_dwork,
                init_t_last_home=init_tlh,
            )
            n_dijkstra += 1
            reachable_arcs = {
                arc for arc in self.arcs
                if arc.start in reachable_nodes and arc.end in reachable_nodes
            }
            group_cache[key] = reachable_arcs
            self._base_reachable_arcs[c.id] = reachable_arcs

        if self.verbose:
            n_groups = len(group_cache)
            avg_arcs = sum(len(v) for v in group_cache.values()) / max(1, n_groups)
            print(f"    Reachability: {len(self.crew)} crew in {n_groups} clock-groups, "
                  f"{n_dijkstra} Dijkstra calls, avg {avg_arcs:.0f}/{len(self.arcs)} "
                  f"arcs reachable per group  ({_t.time()-t0:.1f}s)")

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

        # Step 1: Remove stale spanning wait arc before recomputing reachability
        # so Dijkstra does not traverse an arc that is about to be split.
        if prev_node and prev_node in self.wait_arc_by_start:
            old_arc = self.wait_arc_by_start[prev_node]
            if old_arc.end.time >= new_node.time:
                self._remove_arc(old_arc)
                del self.wait_arc_by_start[prev_node]

        # Step 2: Recompute reachability after removing the stale arc.
        # new_node has no arcs yet so Dijkstra won't visit it -- that is fine;
        # home-spine wait arcs bypass the reachability check in _crew_can_use_arc.
        if self.model is not None and self._base_reachable_arcs:
            _sv, self.verbose = self.verbose, False
            self._compute_base_reachability()
            self.verbose = _sv

        # Step 3: Pre-create placeholder flow balance constraints for new_node.
        # CRITICAL: these must exist before _make_wait_arc so that
        # _add_arc_var_for_crew's chgCoeff calls for arc.end == new_node land
        # on a real constraint instead of being silently dropped.
        # Placeholder "0 == 0" is correct: new_node is never a depot or horizon
        # (those exist from network build time), so its RHS is always 0.
        # The actual arc variables are chgCoeff'd in as the wait/flight arcs run.
        if self.model is not None:
            for cid in list(self.flow_constrs):
                if new_node not in self.flow_constrs[cid]:
                    constr = self.model.addConstr(
                        0.0 == 0.0,
                        name=f"fb_{cid}_{new_node.airport}_{new_node.time}"
                    )
                    self.flow_constrs[cid][new_node] = constr

        # Step 4: Create replacement wait arcs.
        # Reachability is fresh and flow constraints exist for new_node,
        # so _add_arc_var_for_crew can correctly wire variable coefficients.
        _base_airports = set(c.base for c in self.crew)
        _hb = airport if airport in _base_airports else ""
        if prev_node:
            self._make_wait_arc(prev_node, new_node, home_base=_hb)
        if next_node:
            self._make_wait_arc(new_node, next_node, home_base=_hb)

        # Step 5: Expose new flight/deadhead arcs at new_node.
        self._expose_flight_arcs_from(new_node)
        self._expose_flight_arcs_to(new_node)

        # Step 6: Refresh reachability so subsequent add_node calls see the
        # newly reachable flight arcs.
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

        # ── Stranded-crew recovery (runs BEFORE reachability) ──────────────────
        # A crew carried to an off-base airport is truly stranded only if
        # Node(start_ap, depot_start) has NO arcs at all in self.arcs_from (not
        # even a wait arc).  Those crew are reset to their home base.  This must
        # happen BEFORE _compute_base_reachability so the per-crew reachable set
        # is computed from the corrected start airport.
        n_stranded_reset = 0
        for c in self.crew:
            start_ap = self.crew_start_airport.get(c.id, c.base)
            if start_ap == c.base:
                continue
            depot_node = Node(start_ap, self.depot_start)
            if not bool(self.arcs_from.get(depot_node)):
                self.crew_start_airport[c.id] = c.base
                n_stranded_reset += 1
        if self.verbose and n_stranded_reset:
            print(f"    Stranded-crew reset: {n_stranded_reset} crew returned to home base "
                  f"(carry-over airport has no arcs at all in this window's graph)")

        # ── Per-crew reachability (prune variables before creation) ───────────
        self._base_reachable_arcs = {}
        self._compute_base_reachability()

        # ── Active crew: drop crew whose base is structurally isolated ──────────
        # Two-tier check:
        #
        # Tier 1 — Topologically isolated: base has zero reachable flight OR
        #   deadhead arcs.  These crew can only sit on home-spine wait arcs and
        #   contribute nothing.  This catches airports like COU/GRR/IND that appear
        #   as origin airports in the data but have no connecting service in this
        #   window's time-expanded graph.
        #
        # Tier 2 — Coverage-unreachable: base has some flight arcs but none of
        #   them belong to F_cov (e.g. all reachable flights are in the lookahead
        #   tail beyond t_commit).  These crew also contribute zero to coverage
        #   constraints and would only bloat the model.
        #
        # Both tiers are dropped.  The diagnostic message distinguishes them.
        coverage_flight_ids = {f.id for f in self.cov_flights}
        active_crew = []
        dropped_isolated = 0   # tier 1: no flight/dh arcs at all
        dropped_no_cov   = 0   # tier 2: has arcs but can't reach any cov flight
        for c in self.crew:
            reachable = self._base_reachable_arcs.get(c.id, set())
            flight_dh_arcs = [a for a in reachable
                              if a.arc_type in ('flight', 'deadhead')]
            if not flight_dh_arcs:
                dropped_isolated += 1
                continue
            can_cover = any(
                a.arc_type == 'flight' and a.flight_id in coverage_flight_ids
                for a in flight_dh_arcs
            )
            if can_cover:
                active_crew.append(c)
            else:
                dropped_no_cov += 1
        self.active_crew = active_crew
        if self.verbose:
            if dropped_isolated:
                print(f"    Dropped {dropped_isolated} crew from isolated bases "
                      f"(no reachable flight/deadhead arcs in this window)")
            if dropped_no_cov:
                print(f"    Dropped {dropped_no_cov} crew unreachable from any "
                      f"coverage flight (arcs exist but not into F_cov)")
            total_dropped = dropped_isolated + dropped_no_cov
            if total_dropped:
                print(f"    {len(active_crew)} active crew of {len(self.crew)} total "
                      f"({total_dropped} dropped)")

        # ── Variables: x_{c,a} ∈ [0,1] (slide 25) ───────────────────────────
        # Arc cost is adjusted per crew: a deadhead arc whose destination is the
        # crew's home base is discounted by C_DH_HOME_RETURN_DISCOUNT so the solver
        # prefers routing regional / spoke crew home over sending them onward.
        # The discount is encoded in each variable's obj coefficient so it flows
        # through to the Gurobi objective without changing the shared arc.cost.
        total_vars = 0
        # Pre-build flight lookup: flight_id -> Flight object, for home-return check
        _flight_by_id: dict[int, Flight] = {f.id: f for f in self.flights}
        for c in active_crew:
            self.arc_var[c.id] = {}
            self.flow_constrs[c.id] = {}
            for arc in self.arcs:
                if self._crew_can_use_arc(c, arc):
                    cost = arc.cost
                    # Home-return deadhead discount
                    if (arc.arc_type == 'deadhead'
                            and arc.flight_id is not None
                            and C_DH_HOME_RETURN_DISCOUNT > 0):
                        fl = _flight_by_id.get(arc.flight_id)
                        if fl is not None and fl.dest == c.base:
                            cost *= (1.0 - C_DH_HOME_RETURN_DISCOUNT)
                    var = self.model.addVar(
                        lb=0.0, ub=1.0, obj=cost,
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

            # Crew starts at their carry-over position (may differ from home base)
            crew_depot   = Node(self.crew_start_airport[c.id], self.depot_start)
            home_horizon = Node(c.base, self.horizon_end)

            for node in node_list:
                out_arcs = [a for a in self.arcs_from.get(node, []) if a in cvars]
                in_arcs  = [a for a in self.arcs_to.get(node, [])   if a in cvars]

                # Skip nodes where this crew has no arcs at all (no constraint needed)
                if not out_arcs and not in_arcs:
                    continue

                out_expr = gp.quicksum(cvars[a] for a in out_arcs) if out_arcs else 0.0
                in_expr  = gp.quicksum(cvars[a] for a in in_arcs)  if in_arcs  else 0.0

                if node == crew_depot:
                    # Source: exactly one unit departs from crew's start depot (slide 27)
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
        # Always add the constraint even when fl_arcs is empty.
        # When empty: slack_var >= min_crew forces the full uncoverage penalty.
        # Old code had `if not fl_arcs: continue` which left the slack
        # unconstrained so Gurobi set it to 0 for free — flight looked covered.
        n_no_arcs = 0
        for f in self.cov_flights:
            fl_arcs = [a for a in self._arcs_by_flight.get(f.id, [])
                       if a.arc_type == 'flight']
            if fl_arcs:
                cov_expr = gp.quicksum(
                    self.arc_var[c.id][arc]
                    for c in active_crew
                    for arc in fl_arcs
                    if arc in self.arc_var[c.id]
                )
            else:
                cov_expr = 0.0
                n_no_arcs += 1
            constr = self.model.addConstr(
                cov_expr + self.slack_var[f.id] >= f.min_crew,
                name=f"cov_{f.id}",
            )
            self.coverage_constrs[f.id] = constr
        if self.verbose and n_no_arcs:
            print(f"    WARNING: {n_no_arcs} coverage flights have no reachable "
                  f"flight arcs — will be uncovered (slack-only constraint added)")

        # ── Constraint 3': Home-break (slide 11: Δ_hb = 48 h) ──────────────────
        # For each crew c and each flight/deadhead arc A departing from c's home
        # base b at time t_dep: if the crew uses A, they must NOT have used any
        # inbound arc to b within the preceding DELTA_HB minutes.
        #
        #   x_{c,A} + Σ_{I : I.dest=b, t_dep - Δ_hb ≤ I.arr < t_dep} x_{c,I} ≤ 1
        #
        # We only add this for arcs whose origin is the crew's home base (not for
        # intermediate stops at the base, which are covered differently).
        n_hb_constrs = 0
        base_airports = set(c.base for c in active_crew)
        # Pre-index: for each base, sorted list of (true_end, arc) for inbound arcs
        base_inbound_arcs: dict[str, list[tuple[int, Arc]]] = {b: [] for b in base_airports}
        for arc in self.arcs:
            if arc.arc_type in ('flight', 'deadhead') and arc.end.airport in base_airports:
                base_inbound_arcs[arc.end.airport].append((arc.true_end, arc))
        for b in base_inbound_arcs:
            base_inbound_arcs[b].sort(key=lambda x: (x[0], x[1].id))

        import bisect as _bisect
        # Pre-index per-crew arcs by airport to avoid O(arcs^2) scans.
        # crew_dep_from_base[cid]  = sorted list of (dep_time, arc) departing c.base
        # crew_inb_to_base[cid]    = sorted list of (true_end, arc) arriving  c.base
        # Built once here; used in the constraint loop below.
        crew_dep_idx: dict[int, list[tuple[int, Arc]]] = {}
        crew_inb_idx: dict[int, list[tuple[int, Arc]]] = {}
        for c in active_crew:
            cvars = self.arc_var[c.id]
            deps, inbs = [], []
            for arc in cvars:
                if arc.arc_type not in ('flight', 'deadhead'):
                    continue
                if arc.start.airport == c.base:
                    deps.append((arc.start.time, arc))
                if arc.end.airport == c.base:
                    inbs.append((arc.true_end, arc))
            deps.sort(key=lambda x: (x[0], x[1].id))
            inbs.sort(key=lambda x: (x[0], x[1].id))
            crew_dep_idx[c.id] = deps
            crew_inb_idx[c.id] = inbs

        # Cache on self so _add_arc_var_for_crew can use them during DDD refinement
        self._crew_dep_idx = crew_dep_idx
        self._crew_inb_idx = crew_inb_idx

        for c in active_crew:
            cvars      = self.arc_var[c.id]
            dep_list   = crew_dep_idx[c.id]
            inb_list   = crew_inb_idx[c.id]
            if not dep_list or not inb_list:
                continue
            # Lower bound on when any away spell for this crew could have started.
            # An inbound arc can only END a trip that reached the D_AWAY cap if it
            # arrives >= DELTA_AWAY after this bound; otherwise the trip was short
            # and owes no 48h break, so we skip the coupling entirely.
            _start_ap = self.crew_start_airport.get(c.id, c.base)
            _pc = self.carry_clocks.get(c.id, ClockState())
            if _start_ap != c.base:
                trip_start_lb = _pc.away_since if _pc.away_since > -LARGE else self.win.t_start
            else:
                trip_start_lb = self.depot_start
            for t_dep, dep_arc in dep_list:
                t_lo   = t_dep - DELTA_HB
                lo_idx = _bisect.bisect_left(inb_list, (t_lo,))
                hi_idx = _bisect.bisect_left(inb_list, (t_dep,))
                recent_inbound = [arc for te, arc in inb_list[lo_idx:hi_idx]
                                  if te - trip_start_lb >= DELTA_AWAY]
                if not recent_inbound:
                    continue
                self.model.addConstr(
                    cvars[dep_arc]
                    + gp.quicksum(cvars[ia] for ia in recent_inbound)
                    <= 1,
                    name=f"hb_{c.id}_{dep_arc.id}",
                )
                n_hb_constrs += 1

        self.model.update()
        if self.verbose:
            if n_hb_constrs:
                print(f"    Home-break constraints: {n_hb_constrs}  ({_t.time()-t0:.1f}s)")
            print(f"    Coverage constraints: {len(self.coverage_constrs)}/{len(self.cov_flights)}  "
                  f"({_t.time()-t0:.1f}s)")
            print(f"    Model: {self.model.NumVars:,} vars, "
                  f"{self.model.NumConstrs:,} constrs  ({_t.time()-t0:.1f}s)")

        # ── Constraint 3'': Cross-window home-break carry-over ─────────────────
        # Within-window hb_ constraints only cover the case where both the
        # inbound-to-home arc AND the next departure-from-home arc are visible
        # in the same window. When a crew returns home near the end of window W
        # and departs early in window W+1, the inbound arc belongs to W's network
        # and is invisible here. We fix this by using the carried-over
        # t_last_home_return clock: if that timestamp is within DELTA_HB of the
        # window start, any departure from home base before
        # (t_last_home_return + DELTA_HB) is blocked.
        #
        # For each such crew c:
        #   Σ_{A: A.start.airport=c.base, A.start.time < t_last_home_return+Δ_hb} x_{c,A} = 0
        #
        # We express this as x_{c,A} ≤ 0 per arc (ub=0) rather than a summed
        # constraint so that DDD arc additions are handled correctly — new arcs
        # with dep < embargo_end are simply never assigned a variable (ub=0 is
        # handled at variable-creation time via _crew_can_use_arc extension below).
        n_xw_hb = 0
        for c in active_crew:
            clock = self.carry_clocks.get(c.id)
            if clock is None:
                continue
            # Embargo ONLY crew who finished a trip that hit the D_AWAY cap and so
            # owe a 48h home break.  Short round-trips set home_break_until = -LARGE
            # and are never embargoed (this is what was killing window-1 coverage).
            embargo_end = clock.home_break_until
            if embargo_end <= -LARGE:
                continue
            if embargo_end <= self.win.t_start:
                # Break already completed before this window started; nothing to block
                continue
            # Crew has an outstanding home-break obligation that runs into this window.
            # Even if they start the window at their home base (e.g. arrived home 7h
            # before the window boundary), the embargo still applies — they haven't
            # served the full 48h yet.  The home-spine wait arc keeps them in place
            # until embargo_end, but we still need to zero-out departure arcs before
            # that time so the LP/MIP can't schedule a departure before the break ends.
            cvars = self.arc_var[c.id]
            blocked = [
                arc for arc in cvars
                if arc.arc_type in ('flight', 'deadhead')
                and arc.start.airport == c.base
                and arc.start.time < embargo_end
            ]
            for arc in blocked:
                self.model.addConstr(
                    cvars[arc] == 0,
                    name=f"xwhb_{c.id}_{arc.id}",
                )
                n_xw_hb += 1

        # Store embargo data on self so _add_arc_var_for_crew can enforce it
        # for arcs added during DDD refinement.
        self._xw_hb_embargo: dict[int, int] = {}  # crew_id -> embargo_end (absolute min)
        for c in active_crew:
            clock = self.carry_clocks.get(c.id)
            if clock is None:
                continue
            embargo_end = clock.home_break_until
            if embargo_end <= -LARGE:
                continue
            if embargo_end > self.win.t_start:
                self._xw_hb_embargo[c.id] = embargo_end

        self.model.update()
        if self.verbose and n_xw_hb:
            print(f"    Cross-window home-break constraints: {n_xw_hb}  "
                  f"({len(self._xw_hb_embargo)} crew under embargo)  "
                  f"({_t.time()-t0:.1f}s)")

        # NOTE: d_away (4-day return window) is enforced structurally by the
        # clock-constrained Dijkstra in compute_reachable / _compute_base_reachability.
        # Arcs that cannot be part of any d_away-feasible path are excluded from
        # _base_reachable_arcs and therefore never get variables.  No additional LP
        # constraint rows are needed: the Dijkstra already prunes the arc set to only
        # paths that include a home-return within DELTA_AWAY.

    def _add_arc_var_for_crew(self, crew_id: int, arc: Arc):
        if arc in self.arc_var.get(crew_id, {}):
            return
        c = self.crew_by_id.get(crew_id)
        if c is None:
            return
        # Delegate reachability check (including home-spine bypass) to
        # _crew_can_use_arc so the logic lives in exactly one place.
        if not self._crew_can_use_arc(c, arc):
            return
        if crew_id not in self.arc_var:
            self.arc_var[crew_id] = {}

        # Cross-window home-break embargo: if this arc departs from the crew's
        # home base before their carry-over embargo_end, fix its ub to 0.
        embargo_end = getattr(self, '_xw_hb_embargo', {}).get(crew_id, -1)
        is_embargoed = (
            embargo_end > 0
            and arc.arc_type in ('flight', 'deadhead')
            and arc.start.airport == c.base
            and arc.start.time < embargo_end
        )

        ub = 0.0 if is_embargoed else 1.0

        # Home-return deadhead discount (same logic as in build_model variable creation)
        cost = arc.cost
        if (arc.arc_type == 'deadhead'
                and arc.flight_id is not None
                and C_DH_HOME_RETURN_DISCOUNT > 0):
            fl = next((f for f in self.flights if f.id == arc.flight_id), None)
            if fl is not None and fl.dest == c.base:
                cost *= (1.0 - C_DH_HOME_RETURN_DISCOUNT)

        var = self.model.addVar(lb=0.0, ub=ub, obj=cost,
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

        # Wire into home-break constraints (Δ_hb = 48 h).
        # When a new arc is added for crew c we may need new hb constraints:
        # (a) new arc departs c.base  → check recent inbound arcs for c
        # (b) new arc arrives  c.base → check upcoming departures from c.base for c
        # Use the pre-built sorted indices (crew_dep_idx / crew_inb_idx) when
        # available to avoid O(arcs) scans; fall back to a linear scan only on
        # the first DDD iteration before those indices exist.
        if arc.arc_type in ('flight', 'deadhead'):
            import bisect as _bisect
            cvars = self.arc_var[crew_id]

            if arc.start.airport == c.base:
                # (a) departing home base
                t_dep = arc.start.time
                t_lo  = t_dep - DELTA_HB
                inb_list = getattr(self, '_crew_inb_idx', {}).get(crew_id)
                if inb_list is not None:
                    lo_idx = _bisect.bisect_left(inb_list, (t_lo,))
                    hi_idx = _bisect.bisect_left(inb_list, (t_dep,))
                    recent = [a for _, a in inb_list[lo_idx:hi_idx] if a in cvars and a != arc]
                else:
                    recent = [
                        a for a in cvars
                        if a != arc and a.arc_type in ('flight', 'deadhead')
                        and a.end.airport == c.base and t_lo <= a.true_end < t_dep
                    ]
                if recent:
                    self.model.addConstr(
                        var + gp.quicksum(cvars[ia] for ia in recent) <= 1,
                        name=f"hb_{crew_id}_{arc.id}",
                    )

            if arc.end.airport == c.base:
                # (b) arriving home base
                t_arr = arc.true_end
                t_hi  = t_arr + DELTA_HB
                dep_list = getattr(self, '_crew_dep_idx', {}).get(crew_id)
                if dep_list is not None:
                    lo_idx = _bisect.bisect_left(dep_list, (t_arr,))
                    hi_idx = _bisect.bisect_left(dep_list, (t_hi,))
                    upcoming = [a for _, a in dep_list[lo_idx:hi_idx] if a in cvars and a != arc]
                else:
                    upcoming = [
                        a for a in cvars
                        if a != arc and a.arc_type in ('flight', 'deadhead')
                        and a.start.airport == c.base and t_arr <= a.start.time < t_hi
                    ]
                if upcoming:
                    self.model.addConstr(
                        var + gp.quicksum(cvars[da] for da in upcoming) <= 1,
                        name=f"hb_{crew_id}_{arc.id}_ret",
                    )

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
        crew_depot   = Node(self.crew_start_airport[crew_id], self.depot_start)
        home_horizon = Node(c.base, self.horizon_end)

        # NOTE: the old code had an extra branch:
        #   elif node.time == DEPOT_START and node.airport != c.base:
        #       constr = self.model.addConstr(out_expr == 0)
        # This was WRONG and is removed. The crew_depot source and home_horizon sink
        # are the only special nodes; everything else is plain flow conservation.
        if node == crew_depot:
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
        5. Switch to MIP (binary), solve with MIPGap=0.01, TimeLimit=600s
        """
        print(f"\n  === DDD Solve (window {self.win.idx}) ===")
        solved = False

        for it in range(max_iter):
            self.model.setObjective(
                gp.quicksum(
                    var.Obj * var
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
                var.Obj * var
                for cvars in self.arc_var.values()
                for arc, var in cvars.items()
            ) + gp.quicksum(C_UNC_EFFECTIVE * v for v in self.slack_var.values()),
            GRB.MINIMIZE,
        )
        self.model.setParam("MIPGap", 0.01)
        self.model.setParam("TimeLimit", 600)
        # Speed hints for the (highly symmetric) per-crew MIP.  Crew at the same
        # base — and the m_f interchangeable FAs on each flight — create large
        # orbits that stall branch-and-bound; these help without touching the
        # formulation (per-crew variables are retained, so routes are recoverable).
        #   Symmetry=2 : aggressive symmetry detection/handling
        #   MIPFocus=1 : prioritise finding good feasible incumbents (we care about
        #                coverage, not a proof of optimality, and we hit the limit)
        #   Threads=0  : use all available cores (explicit, in case it was limited)
        self.model.setParam("Symmetry", 2)
        self.model.setParam("MIPFocus", 1)
        self.model.setParam("Threads", 0)
        # Seed the previous window's solution as a MIP start (no-op for window 0).
        self._apply_warm_start()
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

            # Start from the crew's actual position at the beginning of this window
            start_ap = self.crew_start_airport.get(crew_id, c.base)
            curr = Node(start_ap, self.depot_start)
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

    def compute_carry_over(self) -> tuple[dict[str, dict[str, int]], dict[int, ClockState], dict[int, str]]:
        """
        Slide 22: position n_{b,k}^{w+1} = |{c ∈ C_b : loc_w(c) = k}|
        Slide 23: worst-case clock state Γ^{w+1}_c = argmax_{c ∈ C_b} d_work_c
        Falls back to home-base positions if no solution is available.

        Returns a third element: crew_pos — the per-crew-id airport map used to
        build `positions`.  The caller passes this directly into the next window's
        CrewDDDNetwork.__init__ as `carry_crew_pos` so crew_start_airport is
        assigned deterministically by crew ID rather than by queue order within a
        base.  This eliminates the teleportation bug where two crew from the same
        base end up at different airports and then get their positions swapped by
        the queue-based unpacking in __init__.
        """
        eps = 1e-4
        t_commit = self.win.t_commit
        t_hor    = self.win.t_hor

        # Default: all crew at home base with zero clocks (used if no solution)
        no_solution = self.model is None or self.model.Status not in (
            GRB.OPTIMAL, GRB.TIME_LIMIT, GRB.SUBOPTIMAL
        ) or self.model.SolCount == 0

        # Map crew_id → carry-over position and clock
        crew_pos: dict[int, str] = {}
        crew_clock: dict[int, ClockState] = {}

        for c in self.crew:
            cvars = self.arc_var.get(c.id, {})
            last_airport = c.base
            last_d_work = 0
            last_h_home = 0
            last_home_return = -LARGE  # absolute time of last arrival at home base
            carried_away_since = -LARGE
            home_break_until = -LARGE

            if not no_solution:
                try:
                    active_arcs = sorted(
                        [a for a, v in cvars.items() if v.X > eps],
                        key=lambda a: a.true_end
                    )

                    # Walk arcs up to t_commit to get the committed position.
                    # Use true_end (actual arrival time) for clock accounting, not
                    # the snapped arc.end.time, so the away budget is exact.
                    #
                    # FIX (d_work counting): d_work tracks consecutive duty DAYS,
                    # not individual flight legs.  Multiple flights on the same day
                    # all count as one duty day.  A rest gap of >= DELTA_REST
                    # (8 h) between the last flight and the next event resets the
                    # consecutive-day counter.  Without this fix, a crew doing 5
                    # flights over 3 days carries d_work=5 into the next window,
                    # which causes Fwd Dijkstra to immediately prune all arcs from
                    # their carry-over position (d_work=5 > D_WORK=3), triggering
                    # the stranded-crew reset and teleporting them home.
                    last_flight_true_end: int = -1   # true_end of most recent flight arc
                    duty_days_set: set[int] = set()  # calendar days with any flight
                    last_d_work = 0  # reset below after full walk

                    # Away-spell tracking for the conditional 48h home break.  A trip
                    # owes a 48h break ONLY if it reached the D_AWAY (4-day) cap; short
                    # round-trips take the normal 8h rest and stay free to fly.  If the
                    # crew began this window already away, inherit when their current
                    # trip started so its full length is measured.
                    start_ap = self.crew_start_airport.get(c.id, c.base)
                    prev_clock = self.carry_clocks.get(c.id, ClockState())

                    # ── d_away cross-window continuity ─────────────────────────
                    # Seed last_home_return from the carried-in clock so the away
                    # budget (arc.true_end - t_last_home_return) is CONTINUOUS
                    # across window boundaries.
                    #
                    # BUG (fixed here): last_home_return was initialised to -LARGE
                    # every window and only updated from arcs whose end.airport is
                    # the base.  A crew that began the window AT home and departed
                    # produced no committed arc *ending* at base, so last_home_return
                    # stayed -LARGE.  The next window then saw t_last_home_return < 0,
                    # took the `init_t_last_home < 0` branch in compute_reachable, and
                    # RESET the away clock to that window's t_start — handing the crew
                    # a fresh 4-day budget at every boundary.  Away-spells therefore
                    # leaked up to (commit-period + D_AWAY) days (observed 5-6 day
                    # spells against a 4-day cap), which in turn forced clustered 48h
                    # home breaks that stranded small bases (e.g. MHK #6141 uncovered).
                    if start_ap == c.base:
                        # Crew is at home at the window start: their last home touch is
                        # at least t_start.  Forward home-wait arcs below bump this up
                        # toward the actual departure time when they exist.
                        last_home_return = max(prev_clock.t_last_home_return,
                                               self.win.t_start)
                    else:
                        # Crew starts away: inherit the previous window's last home
                        # touch so the budget keeps counting down, not restarting.
                        last_home_return = prev_clock.t_last_home_return

                    if start_ap != c.base:
                        away_since = (prev_clock.away_since
                                      if prev_clock.away_since > -LARGE
                                      else self.win.t_start)
                    else:
                        away_since = -LARGE
                    last_long_trip_return = -LARGE   # return of most recent trip that
                                                     # reached the D_AWAY cap

                    for arc in active_arcs:
                        if arc.end.time > t_commit:
                            break
                        last_airport = arc.end.airport
                        # Departing home starts an away spell; arriving home ends it.
                        if (arc.arc_type in ('flight', 'deadhead')
                                and arc.start.airport == c.base
                                and arc.end.airport != c.base
                                and away_since <= -LARGE):
                            away_since = arc.start.time
                        if arc.end.airport == c.base and away_since > -LARGE:
                            if arc.true_end - away_since >= DELTA_AWAY:
                                last_long_trip_return = max(last_long_trip_return,
                                                            arc.true_end)
                            away_since = -LARGE
                        if arc.arc_type == 'flight':
                            # Count duty day by which calendar day the flight DEPARTS.
                            # Two flights on the same calendar day = 1 duty day.
                            dep_day = arc.start.time // 1440
                            duty_days_set.add(dep_day)
                            last_flight_true_end = max(last_flight_true_end, arc.true_end)
                        elif arc.arc_type == 'wait' and arc.end.airport == c.base:
                            last_h_home += (arc.end.time - arc.start.time)
                            # A rest wait of >= DELTA_REST at home resets consecutive days.
                            if arc.end.time - arc.start.time >= DELTA_REST:
                                duty_days_set.clear()
                                last_flight_true_end = -1
                        if arc.end.airport == c.base:
                            last_home_return = max(last_home_return, arc.true_end)

                    # If the crew had a rest gap of >= DELTA_REST between their last
                    # flight and t_commit (i.e. they were resting at t_commit), the
                    # consecutive-day counter resets to 0 for the next window.
                    # This matches the network semantics: a wait arc >= DELTA_REST
                    # sets min_duty_at[to] = 0 in _make_wait_arc.
                    if (last_flight_true_end >= 0
                            and (t_commit - last_flight_true_end) >= DELTA_REST):
                        last_d_work = 0   # rest completed before window boundary
                    else:
                        last_d_work = len(duty_days_set)

                    # ── Tail scan for last home return ─────────────────────────
                    # The committed-arc walk above only tracks home returns with
                    # arc.end.time <= t_commit.  If the crew planned a home return
                    # in the tail (t_commit < end.time <= t_hor), that timestamp
                    # was never recorded so the next window saw init_t_last_home=-1
                    # and granted a fresh D_AWAY budget — allowing indefinite non-
                    # return.  Fix: also scan tail arcs for home arrivals.
                    # We do NOT update last_airport from tail arcs (that would
                    # teleport the crew to a planned-but-not-yet-committed position);
                    # we only update last_home_return so the away clock is correct.
                    for arc in active_arcs:
                        if arc.end.time <= t_commit:
                            continue   # already handled in the committed walk
                        if arc.end.airport == c.base:
                            last_home_return = max(last_home_return, arc.true_end)

                    # Conditional home break: owed ONLY when the last completed away
                    # trip reached the D_AWAY cap (short trips owe nothing).
                    if last_long_trip_return > -LARGE:
                        home_break_until = last_long_trip_return + DELTA_HB
                    else:
                        home_break_until = -LARGE
                    # Carry the ongoing away-spell start if still away at t_commit.
                    carried_away_since = away_since if last_airport != c.base else -LARGE

                    # NOTE: we do NOT look into the tail (t > t_commit) to find a
                    # planned home return and pre-emptively mark the crew as home.
                    # That was the old "key fix" and it caused teleportation: the
                    # tail return flight was stripped by save_combined_result's
                    # dep >= t_commit filter, so the crew appeared at home with no
                    # recorded flight.  Instead we carry the true committed position
                    # (wherever they are at t_commit) and let the next window plan
                    # the actual return with a real committed leg.

                except AttributeError:
                    pass  # no solution available; keep defaults

            crew_pos[c.id] = last_airport
            crew_clock[c.id] = ClockState(
                t_reset=0,
                d_work=last_d_work,
                h_home=last_h_home,
                t_last_home_return=last_home_return,
                away_since=carried_away_since,
                home_break_until=home_break_until,
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

        return dict(positions), worst_clocks, crew_pos


# ─────────────────────────────────────────────────────────────────────────────
# RESULT SERIALISATION  (single combined JSON across all windows)
# ─────────────────────────────────────────────────────────────────────────────

def save_combined_result(
    window_results: list[dict],   # list of (result, cov_flights, win) triples
    crew: list[CrewMember],
    airline: str,
    out_dir: str = ".",
    coverage_days: int = 0,
):
    """Merge all rolling-horizon windows into one JSON file.

    Key invariant: flights[i].id == i (0-based, globally sequential across all
    windows).  Route leg flight_ids are remapped to this global index so the
    downstream visualiser can do a direct array lookup.

    Each coverage flight appears exactly once — from the window that committed
    it (win.t_start ≤ dep_min < win.t_commit).  Routes from every window are
    concatenated; per-crew routes are stitched together so the same crew_id
    appears as a single continuous route entry with legs from all windows in
    chronological order.
    """
    # ── 1. Collect all committed coverage flights (deduplicated by flight.id) ──
    # A flight is "committed" in the window where its dep_min falls before t_commit.
    # Because windows overlap, the same flight could appear in multiple windows'
    # cov_flights; we keep only the first occurrence (lowest window index).
    all_cov_flights: list[Flight] = []
    seen_fids: set[int] = set()
    for entry in window_results:
        for f in entry["cov_flights"]:
            if f.id not in seen_fids:
                seen_fids.add(f.id)
                all_cov_flights.append(f)

    # Sort by departure time for a clean sequential id assignment
    all_cov_flights.sort(key=lambda f: (f.dep_min, f.id))
    global_id_remap: dict[int, int] = {f.id: i for i, f in enumerate(all_cov_flights)}

    # ── 2. Stitch per-crew routes across windows ──────────────────────────────
    # crew_id → list of legs (globally remapped, in time order)
    crew_legs: dict[int, list[dict]] = {}
    crew_base: dict[int, str] = {c.id: c.base for c in crew}

    for entry in window_results:
        result = entry["result"]
        id_remap = entry["id_remap"]   # internal id -> window-local 0-based
        t_commit = entry["win"].t_commit  # only commit legs before this boundary
        # Compose: internal -> window-local -> global
        for r in result.get("routes", []):
            cid = r["crew_id"]
            if cid not in crew_legs:
                crew_legs[cid] = []
            for leg in r.get("legs", []):
                # Fix 1: Drop tail legs that fall at or beyond the commit boundary.
                # _extract_routes walks the full arc chain including lookahead/tail
                # arcs beyond t_commit. Without this filter, tail legs from window W
                # appear in the final route even though they will be re-planned (and
                # possibly reassigned) by window W+1. The canonical symptom is a
                # deadhead like SGF->ORD appearing out-of-order several days after the
                # crew has already been dispatched from ORD in the next window.
                if leg.get("dep", 0) >= t_commit:
                    continue
                new_leg = dict(leg)
                fid = leg.get("flight_id")
                if fid is not None:
                    # Remap: window-local -> global sequential
                    # id_remap maps internal->local; global_id_remap maps internal->global
                    # We stored the internal id in entry["internal_id_remap"] (see below)
                    internal_fid = entry["local_to_internal"].get(fid, fid)
                    new_leg["flight_id"] = global_id_remap.get(internal_fid, fid)
                new_leg.setdefault("window", entry["window_idx"])
                crew_legs[cid].append(new_leg)

    # Sort each crew's legs by departure time and deduplicate
    routes: list[dict] = []
    _type_priority = {'flight': 0, 'deadhead': 1}
    for cid, legs in crew_legs.items():
        legs.sort(key=lambda l: l.get("dep", 0))
        # Deduplicate legs that appear in overlapping window tails.
        # Key excludes type — same flight appears as 'deadhead' in one window's
        # tail and 'flight' in the next window's commit. Keep 'flight' over 'deadhead'.
        best: dict[tuple, dict] = {}
        for leg in legs:
            key = (leg.get("from"), leg.get("to"),
                   leg.get("dep"), leg.get("arr"))
            existing = best.get(key)
            if existing is None:
                best[key] = leg
            else:
                if (_type_priority.get(leg.get("type"), 9)
                        < _type_priority.get(existing.get("type"), 9)):
                    best[key] = leg
        deduped = sorted(best.values(), key=lambda l: l.get("dep", 0))
        if deduped:
            routes.append({
                "crew_id": cid,
                "base": crew_base.get(cid, ""),
                "crew_count": 1,
                "legs": deduped,
            })

    # ── 3. Aggregate uncovered flights (deduplicated) ──────────────────────────
    uncovered_map: dict[int, float] = {}
    for entry in window_results:
        for f, slots in entry["result"].get("uncovered_flights", []):
            if f.id not in uncovered_map:
                uncovered_map[f.id] = slots

    uncovered_flights_out = []
    for f in all_cov_flights:
        if f.id in uncovered_map:
            uncovered_flights_out.append({
                "flight_num": f.flight_num, "origin": f.origin, "dest": f.dest,
                "dep_min": f.dep_min, "arr_min": f.arr_min,
                "missing_slots": uncovered_map[f.id],
            })

    # ── 4. Aggregate cost totals ───────────────────────────────────────────────
    total_cost     = sum((e["result"].get("cost") or 0.0) for e in window_results)
    flight_cost    = sum(e["result"].get("flight_cost", 0.0)   for e in window_results)
    deadhead_cost  = sum(e["result"].get("deadhead_cost", 0.0) for e in window_results)
    wait_cost      = sum(e["result"].get("wait_cost", 0.0)     for e in window_results)
    uncov_slots    = sum(uncovered_map.values())
    num_flights    = len(all_cov_flights)
    covered        = num_flights - len(uncovered_map)
    horizon_end    = max((e["win"].t_hor for e in window_results), default=0)
    statuses       = [e["result"].get("status", "unknown") for e in window_results]
    overall_status = "optimal" if all(s == "optimal" for s in statuses) else "suboptimal"

    payload = {
        "meta": {
            "days": coverage_days,
            "horizon_end": horizon_end,
            "num_windows": len(window_results),
            "solve_status": overall_status,
            "total_cost": total_cost,
            "flight_cost": flight_cost,
            "deadhead_cost": deadhead_cost,
            "wait_cost": wait_cost,
            "uncovered_slots": uncov_slots,
            "num_flights": num_flights,
            "covered_flights": covered,
        },
        "crew": [{"id": c.id, "base": c.base} for c in crew],
        "flights": [
            {
                "id": i,
                "flight_num": f.flight_num,
                "origin": f.origin, "dest": f.dest,
                "dep_min": f.dep_min, "arr_min": f.arr_min,
                "duration": f.duration, "min_crew": f.min_crew,
            }
            for i, f in enumerate(all_cov_flights)
        ],
        "routes": routes,
        "uncovered_flights": uncovered_flights_out,
    }

    fname = os.path.join(out_dir, f"result_{airline}.json")
    with open(fname, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(f"  Saved combined → {fname}  "
          f"({num_flights} flights, {len(routes)} crew routes, "
          f"{len(window_results)} windows merged)")
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
    carry_crew_pos: dict[int, str] = {}   # Fix 2: per-crew-id airport from previous window
    prev_routes: list[dict] = []          # previous window's routes -> MIP warm start
    all_results = []
    window_entries: list[dict] = []   # accumulate for combined save

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
            carry_crew_pos=carry_crew_pos or None,   # Fix 2: deterministic per-crew positions
            verbose=verbose,
        )
        net.build_initial_network()
        net.build_model()
        net.set_warm_start(prev_routes)     # seed from previous window (no-op on window 0)
        result = net.solve()
        prev_routes = result.get("routes", []) or []

        # Carry-over for next window (slides 22–23)
        # Fix 2: unpack the third return value (per-crew-id airport map)
        carry_positions, carry_clocks, carry_crew_pos = net.compute_carry_over()

        t1 = _t.time()
        cost_str = f"{result['cost']:,.1f}" if result.get('cost') is not None else "N/A"
        print(f"\n  Window {win.idx} summary: status={result['status']} "
              f"cost={cost_str}  "
              f"covered={result.get('covered_flights', 0)}/{result.get('num_flights', 0)}  "
              f"time={t1-t0:.1f}s")

        # Build the local->internal flight id reverse map for combined serialisation.
        # save_combined_result needs to convert local (0-based per window) leg flight_ids
        # back to the original internal ids so they can be remapped to global ids.
        local_id_remap   = {f.id: i for i, f in enumerate(f_cov)}      # internal -> local
        local_to_internal = {v: k for k, v in local_id_remap.items()}  # local -> internal

        window_entries.append({
            "window_idx":        win.idx,
            "win":               win,
            "result":            result,
            "cov_flights":       f_cov,
            "id_remap":          local_id_remap,
            "local_to_internal": local_to_internal,
        })
        all_results.append({"window": win.idx, **result})

    # Save all windows merged into one JSON
    if window_entries:
        save_combined_result(
            window_results=window_entries,
            crew=crew,
            airline=airline,
            out_dir=out_dir,
            coverage_days=coverage_days,
        )

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
    random_min_crew: Optional[bool] = None,
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
        random_min_crew: None -> use module flag RANDOM_MIN_CREW; True/False to force
                         drawing min cabin crew randomly (constant per flight number)
                         vs. reading MIN_CABIN_CREW from the CSV.
    """
    import time as _t
    os.makedirs(out_dir, exist_ok=True)
    t_global = _t.time()
    horizon_days = days + T_DAYS_TAIL

    use_random = RANDOM_MIN_CREW if random_min_crew is None else random_min_crew
    if use_random:
        crew_src = (f"RANDOM per flight number "
                    f"(range {RANDOM_MIN_CREW_MIN}-{RANDOM_MIN_CREW_MAX}, "
                    f"seed {RANDOM_SEED})")
    else:
        crew_src = "from CSV MIN_CABIN_CREW"
    print(f"Loading flights from {csv_path}...")
    print(f"  Planning period: {days} days  |  Horizon: {horizon_days} days")
    print(f"  Min cabin crew : {crew_src}")

    flights_by_airline, week_start = parse_flights_by_airline(
        csv_path, days=days, horizon_days=horizon_days,
        random_min_crew_override=use_random,
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
    import sys, csv as _csv, argparse
    from collections import Counter as _Counter

    ap = argparse.ArgumentParser(
        description="Rolling-horizon DDD crew scheduler.")
    ap.add_argument("path", nargs="?", default="data/flights_enriched.csv",
                    help="path to flights_enriched.csv (default: %(default)s)")
    ap.add_argument("days", nargs="?", type=int, default=30,
                    help="planning/coverage period in days (default: %(default)s)")
    ap.add_argument("airline", nargs="?", default=None,
                    help="airline code (e.g. AA) or list number; prompts if omitted")
    # ── randomized minimum cabin crew ──
    ap.add_argument("--random-min-crew", action="store_true",
                    help="draw min cabin crew randomly instead of reading "
                         "MIN_CABIN_CREW from the CSV; the draw is constant per "
                         "flight number across all of its occurrences")
    ap.add_argument("--random-min-crew-min", type=int, default=RANDOM_MIN_CREW_MIN,
                    metavar="N", help="inclusive lower bound for the draw "
                                      "(default: %(default)s)")
    ap.add_argument("--random-min-crew-max", type=int, default=RANDOM_MIN_CREW_MAX,
                    metavar="N", help="inclusive upper bound for the draw "
                                      "(default: %(default)s)")
    ap.add_argument("--seed", type=int, default=RANDOM_SEED, metavar="N",
                    help="random seed for the min-crew draw (default: %(default)s)")
    args = ap.parse_args()

    if args.random_min_crew_min > args.random_min_crew_max:
        ap.error("--random-min-crew-min must be <= --random-min-crew-max")

    # Push CLI options into the module config that random_min_crew() reads.
    RANDOM_MIN_CREW_MIN = args.random_min_crew_min
    RANDOM_MIN_CREW_MAX = args.random_min_crew_max
    RANDOM_SEED         = args.seed

    path = args.path
    days = args.days

    # Tally flights per operating carrier.
    with open(path, encoding="utf-8") as _f:
        _counts = _Counter(row["OP_CARRIER"].strip()
                           for row in _csv.DictReader(_f)
                           if row.get("OP_CARRIER", "").strip())

    ranked = _counts.most_common()              # [(code, n), ...] high -> low
    print(f"\n{len(ranked)} airlines found in {path}:\n")
    for i, (code, n) in enumerate(ranked, 1):
        print(f"  [{i:2d}]  {code:4s}  {n:>9,} flights")

    # Airline may be given as a positional (code or list number); else prompt.
    choice = args.airline.strip() if args.airline else None
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

    print(f"\nSelected airline: {airline} ({_counts[airline]:,} flights)")
    main(path, days=days, out_dir="results", verbose=True,
         airlines_filter=[airline], random_min_crew=args.random_min_crew)