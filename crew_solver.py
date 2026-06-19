"""
Cabin Crew Pairing via a Time-Expanded Crew-Flow Network
========================================================

Sets & notation:
  A      All airports
  B ⊆ A  Crew home bases (= every airport with flights to & from)
  C      Crew members
  C_b    Crew at base b
  F      All flights
  F_cov  Flights requiring coverage (planning period only)
  W      Ordered solve windows (rolling horizon)

Parameters:
  δ_f    Flight duration (minutes)
  l_f    Passenger load factor ∈ [0,1]
  r_f    Required working crew count
  s_b    Crew supply at base b
  s_min=3
  c_fl   = 100 / min  (flight time worked)
  c_dh   = c_fl + fare (deadhead = labor + seat opp-cost, always > flight cost)
  c_wt   = 0.5 / min  (wait cost rate)
  c_ov   = 500        (overnight flat penalty, wait ≥ 4 h)
  c_unc  = 107        (penalty per uncovered slot)
  Δ_ta   = 45 min     (minimum turnaround)
  Δ_rest = 8 h        (minimum rest before next duty)
  Δ_duty = 14 h       (maximum on-duty time per day)
  Δ_hb   = 48 h       (minimum home break)
  d_work = 3 days     (max consecutive duty days)
  d_away = 4 days     (return window / away cap)
  T_days = 7 days     (solve window length)
  T_commit = 3 days   (committed per step)
  T_tail = 4 days     (return tail = d_away)
  Δ_bucket = 15 min   (node-snap tolerance)
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
# PARAMETERS
# ─────────────────────────────────────────────────────────────────────────────

# Duty / rest limits
DELTA_TA    = 45      # min turnaround between flights at same airport (min)
DELTA_REST  = 8 * 60  # min rest before next duty (min) = 480
DELTA_DUTY  = 14 * 60 # max on-duty time per day (min) = 840
DELTA_HB    = 48 * 60 # min home break (min) = 2880
D_WORK      = 3       # max consecutive duty days
D_AWAY      = 4       # return window in days
DELTA_AWAY  = D_AWAY * 1440  # in minutes

# Debug: trace one crew's seed/carry clock state across windows (env CREW_DEBUG_ID).
_DEBUG_CREW_ID = (int(os.environ["CREW_DEBUG_ID"])
                  if os.environ.get("CREW_DEBUG_ID") else None)

# Break-clock node-state expansion sentinels.
#   NO_STATE   : a base-graph node carries no break clock (pre-expansion).
#   SINK_EXPIRY: all horizon-time states collapse to this so the per-crew sink
#                stays a single node regardless of which clock state reaches it.
NO_STATE          = -(10 ** 15)
SINK_EXPIRY       = (10 ** 15)
# Crew is currently AT HOME in their initial break; away-clock has not started.
# break_expiry = 0 is safe: real expiries are always >= _bucket_day(DELTA_HB +
# DELTA_AWAY) = 8640, and _bucket_day(0) = 0, so the value never collides.
BREAK_IN_PROGRESS = 0

# Rolling horizon
T_DAYS_SOLVE   = 7    # solve window length in days
T_DAYS_COMMIT  = 3    # days committed per step
T_DAYS_TAIL    = D_AWAY    # return tail days
T_LOOKAHEAD    = T_DAYS_SOLVE - T_DAYS_COMMIT  # = 4 days overlap

# Crew limits
S_MIN = 3             # minimum crew at any base 3

RANDOM_SEED = 42069

# Crew utilisation discount for tau_duty (denominator).
# The raw formula tau = 8h * horizon_days assumes each crew works 8h every day,
# but home-break (48h), consecutive-day limits (D_WORK=3), and turnaround rest
# (DELTA_REST=8h) mean realistic utilisation is ~55% of theoretical maximum.
# Without this discount, ORD (49% of all flights) gets sized at ~17 crew when it
# needs ~60-70 to satisfy concurrent demand with legal rest intervals.
CREW_UTILISATION = 0.55

# Cost parameters
C_FL  = 100.0         # per minute of flight time worked
C_WT  = 0.5           # per minute of wait
C_OV  = 500.0         # flat penalty per overnight stay (wait ≥ 4 h)

# Senior crew cost rates (passed in via solve_airline / CrewNetwork when scheduling seniors)
C_FL_SENIOR = 420.0   # per minute of flight time for senior crew
C_WT_SENIOR = 1.0     # per minute of wait for senior crew

# Penalty per uncovered (committed) crew slot, large enough that the solver never
# trades a committed cover for routing cost.
C_UNC_EFFECTIVE = 10**8

# ── Soft look-ahead (seam) coverage ───────────────────────────────────────────
# Rolling-horizon seam: a flight in the first few hours of a commit window can only
# be covered if a crew is ALREADY positioned at its (spoke) origin, rested — but the
# positioning deadhead must depart the evening before, which falls in the PREVIOUS
# window. That window never sees the flight as a coverage flight (it is past its
# t_commit), so it has no incentive to pre-position, and the flight is uncovered.
#
# Fix: also place a SOFT coverage constraint on flights just past t_commit (the seam
# zone). The owning window's predecessor then pays C_UNC_LOOKAHEAD if it leaves the
# seam flight "unplanned", which makes it commit a positioning deadhead (in its own
# commit region) so the next window inherits a rested crew at the spoke and covers it
# for real. The penalty must sit between the positioning cost (~1.5e4: deadhead + wait
# + the leg) and the hard commit penalty (1e8) so the solver pre-positions but NEVER
# trades a committed cover for a look-ahead one. Set T_LOOKAHEAD_COVER = 0 to disable.
C_UNC_LOOKAHEAD = 10**6        # soft penalty per look-ahead (seam) coverage slot
T_LOOKAHEAD_COVER = 12 * 60    # minutes past t_commit to softly cover (half a day)

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
    break_expiry: int = NO_STATE   # absolute minute by which the next >= DELTA_HB
                                   # home break must complete. NO_STATE = base graph
                                   # (un-expanded); SINK_EXPIRY = collapsed horizon.

    def __post_init__(self):
        # Cache the hash: the fields are frozen, so it never changes, and these nodes
        # are hashed hundreds of millions of times as dict/set keys during graph build,
        # reachability and break-clock expansion. The default frozen-dataclass __hash__
        # recomputes hash((airport, time, break_expiry)) on every call.
        object.__setattr__(self, "_hash", hash((self.airport, self.time, self.break_expiry)))

    def __hash__(self):
        return self._hash

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


# Clock state per crew
@dataclass
class ClockState:
    t_reset: int            = 0      # last reset time (min from week start)
    d_work: int             = 0      # consecutive work-days since last rest
    h_home: int             = 0      # home-wait accumulated since last departure (min)
    t_last_home_return: int = -LARGE # absolute minute the last >= DELTA_HB home break
                                     # ENDED (the post-break leave-home departure). The
                                     # D_AWAY budget is measured from here. Only a
                                     # completed 48h break advances it; brief home touches
                                     # do not. -LARGE means no break served yet.
    away_since: int = -LARGE         # absolute minute the current away spell began,
                                     # measured from the last completed 48h break. Brief
                                     # touches at base do NOT reset it; only a 48h break
                                     # does. -LARGE = crew is home with no open spell.
    home_break_until: int = -LARGE   # if the away spell since the last break reached the
                                     # D_AWAY cap, the crew owes a 48h home break until
                                     # this minute; -LARGE = no break owed.
    home_since: int = -LARGE         # start of the current continuous home stay (last
                                     # arrival at base); -LARGE when away. Used to detect
                                     # when a >= DELTA_HB break has been served.
    break_in_progress: bool = False  # True iff crew is currently at home in their initial
                                     # break; away-clock has not started yet. Cleared the
                                     # first time they depart home (or serve a real break).


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
    random.seed(RANDOM_SEED)
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
                load_factor = float(lf_raw) if lf_raw else random.uniform(0.5, 1.0)
            except ValueError:
                load_factor = random.uniform(0.5, 1.0)

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
# CREW BASE SIZING
# n_p = max(n_min, ceil( (Σ_{f: f.orig=p} m_f * d_f / τ_duty) * 1.8 + noise ))
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
        n_demand = ceil(1.8 * (Sum_{f: orig=p} m_f * d_f) / tau_duty)  (duration-weighted)
        n_peak   = ceil(1.8 * peak concurrent crew load over the horizon)
        n        = max(n_demand, n_peak, S_MIN), then a 10% Gaussian jitter
      tau_duty = 8h/day * horizon_days * CREW_UTILISATION
    """
    rng = random.Random(seed)

    origins = set(f.origin for f in flights)
    dests   = set(f.dest   for f in flights)
    # A base is any airport with flights both to AND from it in this airline.
    bases = sorted(origins & dests)

    # Demand:  Sum_{f: orig=p} m_f * d_f   (numerator)
    demand_minutes: dict[str, float] = defaultdict(float)
    for f in flights:
        demand_minutes[f.origin] += f.min_crew * f.duration

    # Peak concurrent crew demand per airport (dep = +crew, arr = -crew sweep).
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

    horizon_days = max(f.arr_min for f in flights) / 1440 if flights else 3
    tau_duty = 8 * 60 * max(1.0, horizon_days) * CREW_UTILISATION

    base_counts: dict[str, int] = {}
    for ap in bases:
        demand   = demand_minutes.get(ap, 0.0)
        peak     = peak_concurrent.get(ap, 0)
        n_demand = math.ceil((demand / tau_duty) * 1.8) if demand > 0 else 0
        n_peak   = math.ceil(peak * 1.8)
        needed   = max(n_demand, n_peak, S_MIN)
        noisy    = int(rng.gauss(needed, max(1, needed * 0.10)))
        base_counts[ap] = max(S_MIN,  noisy)

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
# OPPORTUNITY COST MODEL
# ─────────────────────────────────────────────────────────────────────────────

def _opp_cost_scale(lf: float) -> float:
    if lf <= LF_LOW:
        return 0.0
    if lf >= LF_HIGH:
        return 1.0
    return (lf - LF_LOW) / (LF_HIGH - LF_LOW)


def deadhead_cost(f: Flight, home_base: str = "", c_fl: float = C_FL) -> float:
    """c_dh^a for deadhead arc on flight f.

    Deadhead costs crew salary (same as flying) plus the seat fare (displacing
    a paying passenger), making it strictly more expensive than working the same
    flight.  This ensures the solver never prefers deadhead loops over idle wait.

    If home_base is provided and f.dest == home_base, apply a cost discount so
    that the solver prefers routing regional / spoke crew home over sending
    them to yet another outstation.  The discount is purely a cost signal; the
    hard flow-balance and d_away constraints remain unchanged.

    c_fl: per-minute flight-time cost rate; defaults to the standard C_FL but
    can be overridden (e.g. C_FL_SENIOR) to match the crew tier being scheduled.
    """
    labor = f.duration * c_fl   # crew salary: same cost as working the flight
    fare  = FARE_BASE + FARE_PER_MILE * max(0.0, f.distance)
    opp   = fare * _opp_cost_scale(f.load_factor)
    cost  = labor + opp         # total: labor + seat opportunity cost
    if home_base and f.dest == home_base:
        cost *= (1.0 - C_DH_HOME_RETURN_DISCOUNT)
    return cost


# ─────────────────────────────────────────────────────────────────────────────
# ROLLING HORIZON WINDOWS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Window:
    idx: int
    t_start: int    # minutes
    t_commit: int   # minutes (commit boundary)
    t_hor: int      # minutes (horizon end = t_start + (T_solve+T_tail)*1440)


def build_windows(total_days: int) -> list[Window]:
    """
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


def slice_flights(
    flights: list[Flight], win: Window
) -> tuple[list[Flight], list[Flight], list[Flight]]:
    """
      F_w      = {f : t_start ≤ dep_f < t_hor}
      F_cov_w  = {f : dep_f < t_commit}                         (hard coverage)
      F_look_w = {f : t_commit ≤ dep_f < t_commit + T_LOOKAHEAD_COVER}  (soft, seam)

    F_look is the look-ahead seam zone: flights just past the commit boundary that get
    a SOFT coverage incentive so this window pre-positions crew for them (see the
    C_UNC_LOOKAHEAD comment). They are NOT committed here — the next window covers them
    for real — so they never count toward this window's uncovered total.
    """
    f_w   = [f for f in flights if win.t_start <= f.dep_min < win.t_hor]
    f_cov = [f for f in f_w     if f.dep_min  <  win.t_commit]
    look_end = win.t_commit + T_LOOKAHEAD_COVER
    f_look = [f for f in f_w if win.t_commit <= f.dep_min < look_end] if T_LOOKAHEAD_COVER > 0 else []
    return f_w, f_cov, f_look


# ─────────────────────────────────────────────────────────────────────────────
# REACHABILITY PRUNING
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
    R_b^w = Fwd_b(G_w) ∩ Bwd_b(G_w).
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
        # d_work counts distinct duty DAYS, not flight legs: several legs on the same
        # calendar day are one duty day (matching compute_carry_over).
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

                # d_away: time since the last home touch must not exceed DELTA_AWAY.
                # Home-bound arcs (destination == base) are kept regardless of time_away
                # so a crew over the cap can always route home, arriving at most one leg
                # late; only arcs travelling further away are pruned.
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


# Counter for unique expanded-arc ids (kept distinct from base arc ids).
_EXP_ARC_ID = [10 ** 9]


def _bucket_day(t: int) -> int:
    """Round a break-deadline DOWN to a whole day (conservative: never late)."""
    return (t // 1440) * 1440


def expand_break_clock(
    reachable_arcs: set[Arc],
    base: str,
    depot_node: Node,
    horizon_end: int,
    seed_expiry: int,
    home_carry: int = -LARGE,
) -> tuple[set[Node], list[Arc], dict[Node, list[Arc]], dict[Node, list[Arc]], Node]:
    """
    Bake the single "time since last >= DELTA_HB home break" resource into the
    graph for ONE crew whose home is `base`.

    Reset = a home stay of >= DELTA_HB; it re-anchors the 4-day window and is
    FREE (cost 0).  In real networks the stay is represented by a CHAIN of short
    wait arcs (one per event interval), so we also emit a synthetic zero-cost
    "home-break jump" from every state at `base` to the first base-timeline node
    >= DELTA_HB later.  This subsumes the old single-arc >= DELTA_HB check
    (kept for backward compat with test graphs that use explicit long arcs).
    Every other arc keeps its cost and is legal only while the node it reaches is
    within the current deadline.  Horizon-time states collapse to SINK_EXPIRY.

    Returns (state_nodes, state_arcs, arcs_from, arcs_to, depot_state).
    """
    import bisect as _bisect

    base_from: dict[Node, list[Arc]] = defaultdict(list)
    for arc in reachable_arcs:
        base_from[arc.start].append(arc)

    # Sorted list of all times that appear at `base` in the reachable graph.
    # Used to compute synthetic home-break jumps without enumerating every
    # intermediate wait arc.  horizon_end is always included so the crew can
    # reach the sink via the break path even if the last base event is earlier.
    base_times: list[int] = sorted(set(
        t
        for arc in reachable_arcs
        for ap, t in ((arc.start.airport, arc.start.time),
                      (arc.end.airport,   arc.end.time))
        if ap == base
    ) | {depot_node.time, horizon_end})

    def state_of(end_time: int, expiry: int) -> int:
        return SINK_EXPIRY if end_time == horizon_end else expiry

    depot_state = Node(depot_node.airport, depot_node.time,
                       _bucket_day(seed_expiry))
    state_nodes: set[Node] = {depot_state}
    state_arcs: list[Arc] = []
    frontier = [depot_state]
    seen = {depot_state}

    while frontier:
        sn = frontier.pop()
        base_node = Node(sn.airport, sn.time)   # NO_STATE key into the base graph
        for arc in base_from.get(base_node, []):
            if sn.break_expiry == BREAK_IN_PROGRESS:
                # ── Initial home break: away-clock has not started yet ────────
                # Arcs ending at base keep the crew in their ongoing break.
                # The first departure from base starts the clock.
                if arc.end.airport == base:
                    end  = Node(base, arc.end.time,
                                state_of(arc.end.time, BREAK_IN_PROGRESS))
                    cost = 0.0
                else:
                    new_expiry = _bucket_day(sn.time + DELTA_AWAY + DELTA_HB)
                    end  = Node(arc.end.airport, arc.end.time,
                                state_of(arc.end.time, new_expiry))
                    cost = arc.cost
            else:
                is_reset = (arc.is_wait
                            and arc.start.airport == base
                            and arc.end.airport == base
                            and (arc.end.time - arc.start.time) >= DELTA_HB)
                if is_reset:
                    if sn.time + DELTA_HB > sn.break_expiry:
                        continue               # break would complete after deadline
                    new_expiry = _bucket_day(arc.end.time + DELTA_AWAY + DELTA_HB)
                    end  = Node(arc.end.airport, arc.end.time,
                                state_of(arc.end.time, new_expiry))
                    cost = 0.0                       # the home break is free
                else:
                    if arc.end.time > sn.break_expiry:
                        continue                     # would breach the cap with no break
                    end  = Node(arc.end.airport, arc.end.time,
                                state_of(arc.end.time, sn.break_expiry))
                    cost = arc.cost
            _EXP_ARC_ID[0] += 1
            state_arcs.append(Arc(
                id=_EXP_ARC_ID[0], start=sn, end=end, true_end=arc.true_end,
                cost=cost, arc_type=arc.arc_type, flight_id=arc.flight_id,
            ))
            if end not in seen:
                seen.add(end)
                state_nodes.add(end)
                frontier.append(end)

        # Synthetic home-break jump: when crew is at base, emit a single zero-cost
        # arc that jumps to the first base-timeline node >= DELTA_HB after now.
        # This correctly handles the common case where the home stay is a CHAIN of
        # short wait arcs rather than a single long one.
        # At the DEPOT, a home break that already started in the previous window
        # (home_carry) is credited: the 48h completes at home_carry + DELTA_HB, so the
        # crew only finishes the remaining rest here instead of restarting a full 48h.
        home_start = home_carry if (sn == depot_state and home_carry >= 0) else sn.time
        reset_done = home_start + DELTA_HB
        if sn.airport == base and reset_done <= sn.break_expiry:
            idx = _bisect.bisect_left(base_times, max(sn.time, reset_done))
            if idx < len(base_times):
                tgt_time = base_times[idx]
                new_expiry = _bucket_day(tgt_time + DELTA_AWAY + DELTA_HB)
                end = Node(base, tgt_time, state_of(tgt_time, new_expiry))
                _EXP_ARC_ID[0] += 1
                state_arcs.append(Arc(
                    id=_EXP_ARC_ID[0], start=sn, end=end, true_end=tgt_time,
                    cost=0.0, arc_type='wait', flight_id=None,
                ))
                if end not in seen:
                    seen.add(end)
                    state_nodes.add(end)
                    frontier.append(end)

    # Backward prune: keep only states that can still reach a horizon sink.
    arcs_to: dict[Node, list[Arc]] = defaultdict(list)
    for a in state_arcs:
        arcs_to[a.end].append(a)
    sinks = [n for n in state_nodes
             if n.time == horizon_end and n.break_expiry == SINK_EXPIRY]
    alive: set[Node] = set()
    stack = list(sinks)
    alive.update(sinks)
    while stack:
        n = stack.pop()
        for a in arcs_to.get(n, []):
            if a.start not in alive:
                alive.add(a.start)
                stack.append(a.start)

    state_nodes = {n for n in state_nodes if n in alive}
    state_arcs = [a for a in state_arcs if a.start in alive and a.end in alive]
    # Deterministic ordering. The expansion above visits states in set-iteration order
    # (which varies run-to-run with PYTHONHASHSEED), so state_arcs — and hence the
    # Gurobi variable column order built from it downstream — would otherwise differ
    # every run, swinging the solve time via degenerate-pivot luck. Sort by node/arc
    # VALUE so every run hands Gurobi a byte-identical model. Pure reordering: Arc is
    # keyed by id in the var dicts, so this changes nothing but the column order.
    state_arcs.sort(key=lambda a: (
        a.start.time, a.start.airport, a.start.break_expiry,
        a.end.time, a.end.airport, a.end.break_expiry,
        a.arc_type, a.flight_id if a.flight_id is not None else -1,
        a.true_end, a.cost,
    ))
    arcs_from: dict[Node, list[Arc]] = defaultdict(list)
    arcs_to = defaultdict(list)
    for a in state_arcs:
        arcs_from[a.start].append(a)
        arcs_to[a.end].append(a)
    return state_nodes, state_arcs, arcs_from, arcs_to, depot_state


# ─────────────────────────────────────────────────────────────────────────────
# CORE DDD NETWORK (one airline, one window)
# ─────────────────────────────────────────────────────────────────────────────

class CrewNetwork:
    """
    Time-expanded network for one airline and one rolling-horizon window.

    Variables:
      f_{g,a} ∈ ℤ≥0    integer crew FLOW per clock-group g, arc a (≤ |g|, the group
                       size): how many crew of group g traverse arc a
      s_f ∈ {0,…,r_f}  coverage slack per flight f ∈ F_cov

    Objective:
      min Σ_g Σ_{a∈A_fl} c_fl*δ_a * f_{g,a}
        + Σ_g Σ_{a∈A_dh} c_dh^a * f_{g,a}
        + Σ_g Σ_{a∈A_wt} (c_wt*Δt_a + c_ov*1[Δt≥4h]) * f_{g,a}
        + Σ_{f∈F_cov} c_unc * s_f
      where c_unc is the uncovered-slot penalty (C_UNC_EFFECTIVE for committed flights,
      C_UNC_LOOKAHEAD for soft seam coverage).

    Constraint 1': flow balance per clock-group
    Constraint 2': coverage per flight (equality, Σ_g f_{g,a_f} + s_f = r_f)
    Constraint 3': duty + home-break enforced structurally (no MIP constraint)
    Constraint 4': f_{g,a} ∈ ℤ≥0, s_f ∈ ℤ≥0
    """

    def __init__(
        self,
        flights: list[Flight],         # F_w (full window)
        cov_flights: list[Flight],     # F_cov_w (need coverage)
        crew: list[CrewMember],
        win: Window,
        carry_positions: Optional[dict[str, dict[str, int]]] = None,
        carry_clocks: Optional[dict[int, ClockState]] = None,
        carry_crew_pos: Optional[dict[int, str]] = None,
        lookahead_flights: Optional[list[Flight]] = None,   # F_look_w (soft seam coverage)
        verbose: bool = True,
        cost_fl: float = C_FL,   # per-minute flight cost (override for senior crew)
        cost_wt: float = C_WT,   # per-minute wait cost  (override for senior crew)
    ):
        self.c_fl = cost_fl
        self.c_wt = cost_wt
        self.flights    = flights
        self.cov_set    = {f.id for f in cov_flights}
        self.cov_flights = cov_flights
        # Look-ahead (seam) flights get a SOFT coverage constraint to pre-position crew;
        # they are never reported as uncovered here. Exclude any that are already hard
        # coverage flights (disjoint by dep_min, but guard defensively).
        self.lookahead_flights = [f for f in (lookahead_flights or []) if f.id not in self.cov_set]
        self.crew       = crew
        self.crew_by_id = {c.id: c for c in crew}
        self.win        = win
        self.horizon_end = win.t_hor
        self.depot_start = win.t_start   # window-relative depot (rolls with each window)
        self.verbose    = verbose

        # Carry-over from previous window
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
        self.arc_var: dict[int, dict[Arc, gp.Var]] = {}   # legacy / unused in v4 flow model
        self.slack_var: dict[int, gp.Var] = {}
        self.flow_constrs: dict[int, dict[Node, gp.Constr]] = {}
        self.coverage_constrs: dict[int, gp.Constr] = {}
        self._base_reachable_arcs: dict[str, set[Arc]] = {}  # populated in build_model
        # v4 crew-flow aggregation: one integer flow var per (clock-group, arc) instead
        # of one binary per (crew, arc). Populated in _compute_base_reachability / build_model.
        self.exp_groups: dict[tuple, dict] = {}
        self.group_of: dict[int, tuple] = {}
        self.group_flow: dict[tuple, dict] = {}
        self.active_group_keys: list[tuple] = []
        self._crew_active_arcs: dict[int, list[Arc]] = {}   # per-crew arcs after decomposition

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
        snap arrival end of flight arc to earliest existing node at dest
        satisfying the turnaround gap:
          t' = min{t' ∈ T_ap : t' >= true_arr + Δ_ta}

        This enforces that crew cannot immediately depart on another flight after
        landing — they must wait at least Δ_ta minutes at the destination.
        The arc ends at t', not at true_arr. true_arr is stored separately as
        arc.true_end for violation detection in the DDD loop.

        Depot departures (t=0) are exempt.
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

        # 1. Depot (t=depot_start) and horizon nodes.
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

        # 2. Insert event nodes for every flight.
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

        # 3. Wait arcs: chain consecutive nodes at each airport.
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

        # 5. Flight + deadhead arcs
        #    Arrival end is snapped to satisfy Δ_ta.
        n_arcs = 0
        n_missing = 0
        n_duty_pruned = 0
        for f in sorted(self.flights, key=lambda x: x.dep_min):
            dep_node = self._find_node_at_or_before(f.origin, f.dep_min)
            if dep_node is None or dep_node.time != f.dep_min:
                n_missing += 1
                continue

            # Snap arrival
            arr_node = self._snap_arrival(f.dest, f.arr_min)
            if arr_node is None or arr_node.time > self.horizon_end:
                n_missing += 1
                continue

            # Duty-time check (Constraint 3')
            duty_at_dep = self.min_duty_at.get(dep_node, 0)
            duty_after  = duty_at_dep + f.duration
            if duty_after > DELTA_DUTY:
                n_duty_pruned += 1
                continue

            # Update min_duty_at arr_node
            existing = self.min_duty_at.get(arr_node, DELTA_DUTY + 1)
            if duty_after < existing:
                self.min_duty_at[arr_node] = duty_after

            # Flight arc: cost = c_fl * δ_f
            self._make_arc(dep_node, arr_node, f.arr_min,
                           f.duration * self.c_fl, 'flight', f.id)
            # Deadhead arc: cost = c_dh^a
            self._make_arc(dep_node, arr_node, f.arr_min,
                           deadhead_cost(f, c_fl=self.c_fl), 'deadhead', f.id)
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
            cost = dt * self.c_wt + overnight * C_OV
        arc = self._make_arc(frm, to, to.time, cost, 'wait')
        self.wait_arc_by_start[frm] = arc
        # Rest resets duty (wait ≥ Δ_rest resets duty clock)
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
        Compute reachable arc sets per crew member, from each crew's OWN start airport
        and carry-over clock (init_dwork, init_t_last_home) rather than a worst-case
        clock shared across a base. Crew sharing an identical
        (base, start_airport, init_dwork, init_t_last_home) signature share a single
        Dijkstra, so the call count stays close to |B| in the common case (most crew
        start at home with a zero clock).

        self._base_reachable_arcs is now keyed by CREW ID (not base).
        """
        import time as _t
        t0 = _t.time()
        self._base_reachable_arcs: dict[int, set[Arc]] = {}   # keyed by crew.id

        # Per-crew expanded (break-clock state-space) structures — populated below.
        self._exp_nodes:       dict[int, set[Node]]             = {}
        self._exp_arcs:        dict[int, list[Arc]]             = {}
        self._exp_arcs_from:   dict[int, dict]                  = {}
        self._exp_arcs_to:     dict[int, dict]                  = {}
        self._exp_depot_state: dict[int, Node]                  = {}
        self._exp_sink_node:   dict[int, Optional[Node]]        = {}

        # v4 aggregation: crew sharing an identical (base, start airport, d_work, away
        # anchor, seed_expiry) signature have a byte-identical expanded graph, so we
        # EXPAND ONCE per such group and share the graph. Reachability is cached one
        # level coarser (it does not depend on seed_expiry).
        self.exp_groups = {}
        self.group_of = {}

        node_list   = list(self.nodes)
        arcs_from_d = dict(self.arcs_from)
        arcs_to_d   = dict(self.arcs_to)

        reach_cache: dict[tuple, set[Arc]] = {}   # reachability keyed by rkey
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
            rkey = (c.base, start_ap, init_dwork, init_tlh)

            # seed_expiry: absolute minute by which the crew's NEXT >=48 h home break
            # must complete. Together with rkey it fully determines the expansion.
            if clock.break_in_progress:
                seed_expiry = BREAK_IN_PROGRESS
            elif clock.t_last_home_return >= 0:
                seed_expiry = clock.t_last_home_return + DELTA_AWAY + DELTA_HB
            else:
                seed_expiry = self.depot_start + DELTA_AWAY + DELTA_HB
            # In-progress home break carried across the seam (only meaningful when the
            # crew starts this window at base): the crew may FINISH the 48h break it
            # already started, credited from home_carry rather than restarting at depot.
            home_carry = (clock.home_since
                          if (start_ap == c.base and clock.home_since >= 0) else -LARGE)
            gkey = (c.base, start_ap, init_dwork, init_tlh, seed_expiry, home_carry)

            if _DEBUG_CREW_ID is not None and c.id == _DEBUG_CREW_ID:
                fresh = (not clock.break_in_progress and clock.t_last_home_return < 0)
                print(f"    [SEED w{self.win.idx}] crew {c.id} base={c.base} "
                      f"start_ap={start_ap} {'AWAY' if start_ap != c.base else 'home'} "
                      f"bip={clock.break_in_progress} tlhr={clock.t_last_home_return} "
                      f"home_carry={home_carry} "
                      f"seed_expiry={seed_expiry} depot_start={self.depot_start} "
                      f"{'<<< FRESH RESEED (away-anchor lost)' if (start_ap != c.base and fresh) else ''}")

            # ── Reachability (shared by rkey) ─────────────────────────────────
            reachable = reach_cache.get(rkey)
            if reachable is None:
                depot_node   = Node(start_ap, self.depot_start)
                horizon_node = Node(c.base, self.horizon_end)
                if depot_node not in self.nodes or horizon_node not in self.nodes:
                    reachable = set()
                else:
                    reachable_nodes = compute_reachable(
                        nodes=node_list, arcs_from=arcs_from_d, arcs_to=arcs_to_d,
                        depot_nodes=[depot_node], horizon_nodes=[horizon_node],
                        t_hor=self.horizon_end, base_airport=c.base,
                        init_dwork=init_dwork, init_t_last_home=init_tlh,
                    )
                    n_dijkstra += 1
                    reachable = {
                        arc for arc in self.arcs
                        if arc.start in reachable_nodes and arc.end in reachable_nodes
                    }
                reach_cache[rkey] = reachable
            self._base_reachable_arcs[c.id] = reachable

            # ── Break-clock expansion (shared by gkey — expand ONCE per group) ─
            grp = self.exp_groups.get(gkey)
            if grp is None:
                depot_node_exp = Node(start_ap, self.depot_start)
                if reachable:
                    e_nodes, e_arcs, e_from, e_to, e_depot = expand_break_clock(
                        reachable, c.base, depot_node_exp, self.horizon_end, seed_expiry,
                        home_carry=home_carry,
                    )
                    e_sink_c = [n for n in e_nodes if n.break_expiry == SINK_EXPIRY]
                    e_sink = e_sink_c[0] if e_sink_c else None
                else:
                    e_nodes, e_arcs, e_from, e_to = set(), [], {}, {}
                    e_depot = Node(start_ap, self.depot_start, _bucket_day(seed_expiry))
                    e_sink = None
                grp = {
                    "members": [], "base": c.base,
                    "e_nodes": e_nodes, "e_arcs": e_arcs,
                    "e_from": e_from, "e_to": e_to,
                    "e_depot": e_depot, "e_sink": e_sink,
                }
                self.exp_groups[gkey] = grp
            grp["members"].append(c.id)
            self.group_of[c.id] = gkey

            # Per-crew pointers into the SHARED group graph (for the active-crew
            # filter and _extract_routes — same objects across the group's members).
            self._exp_nodes[c.id]       = grp["e_nodes"]
            self._exp_arcs[c.id]        = grp["e_arcs"]
            self._exp_arcs_from[c.id]   = grp["e_from"]
            self._exp_arcs_to[c.id]     = grp["e_to"]
            self._exp_depot_state[c.id] = grp["e_depot"]
            self._exp_sink_node[c.id]   = grp["e_sink"]

        if self.verbose:
            n_groups = len(self.exp_groups)
            total_exp_arcs = sum(len(g["e_arcs"]) for g in self.exp_groups.values())
            print(f"    Reachability: {len(self.crew)} crew in {n_groups} clock-groups, "
                  f"{n_dijkstra} Dijkstra calls  ({_t.time()-t0:.1f}s)")
            print(f"    Break-clock expansion (v4 shared): {total_exp_arcs:,} expanded arcs "
                  f"across {n_groups} groups  ({_t.time()-t0:.1f}s)")

    # ── Gurobi model (Constraints 1'–4') ───────────────────────

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

        # ── Variables: per-group integer crew FLOW (v4) ──────────────────────
        # Instead of one binary x_{c,a} per (crew, arc), use one integer flow variable
        # per (clock-GROUP, arc): flow = how many crew of that group traverse the arc.
        # Crew in a group are interchangeable (identical expanded graph), and every
        # flight is unit-demand, so this is exact — we recover individual routes by
        # decomposing each group's integer flow into one path per crew after solving.
        # The home-return deadhead discount depends only on the crew's base, which is
        # constant within a group, so it is applied once per group.
        active_set = {c.id for c in active_crew}
        self.group_flow = {}
        self.active_group_keys = []
        total_vars = 0
        _flight_by_id: dict[int, Flight] = {f.id: f for f in self.flights}
        for gkey, grp in self.exp_groups.items():
            members = [cid for cid in grp["members"] if cid in active_set]
            if not members or not grp["e_arcs"]:
                continue
            K = len(members)
            grp["K"] = K
            grp["active_members"] = members
            gidx = len(self.active_group_keys)
            self.active_group_keys.append(gkey)
            base = grp["base"]
            fv: dict[Arc, gp.Var] = {}
            for arc in grp["e_arcs"]:
                cost = arc.cost  # reset arcs already have cost=0
                if (arc.arc_type == 'deadhead'
                        and arc.flight_id is not None
                        and C_DH_HOME_RETURN_DISCOUNT > 0):
                    fl = _flight_by_id.get(arc.flight_id)
                    if fl is not None and fl.dest == base:
                        cost *= (1.0 - C_DH_HOME_RETURN_DISCOUNT)
                var = self.model.addVar(
                    lb=0.0, ub=float(K), obj=cost,
                    vtype=GRB.CONTINUOUS,
                    name=f"f_{gidx}_{arc.id}",
                )
                fv[arc] = var
                total_vars += 1
            self.group_flow[gkey] = fv

        # ── Slack variables s_f ───────────────────────────────────
        # Hard coverage (commit region): full uncovered penalty.
        for f in self.cov_flights:
            sv = self.model.addVar(
                lb=0.0, ub=float(f.min_crew),
                obj=C_UNC_EFFECTIVE,
                vtype=GRB.CONTINUOUS,
                name=f"slack_{f.id}",
            )
            self.slack_var[f.id] = sv
        # Soft look-ahead (seam) coverage: smaller penalty so the window pre-positions
        # crew for the next window's first bank without ever preferring it over a
        # committed cover. These slacks are excluded from the uncovered report.
        for f in self.lookahead_flights:
            sv = self.model.addVar(
                lb=0.0, ub=float(f.min_crew),
                obj=C_UNC_LOOKAHEAD,
                vtype=GRB.CONTINUOUS,
                name=f"slack_look_{f.id}",
            )
            self.slack_var[f.id] = sv

        self.model.update()
        if self.verbose:
            print(f"    Variables: {total_vars:,} group-flow + {len(self.slack_var)} slack  "
                  f"({len(self.active_group_keys)} groups, {len(self.lookahead_flights)} "
                  f"look-ahead)  ({_t.time()-t0:.1f}s)")

        # ── Constraint 1': Flow balance, per clock-group  ────────
        # Each group g with K_g crew: K_g units leave its depot state and K_g are
        # absorbed at its SINK_EXPIRY horizon state; flow is conserved everywhere else.
        for gkey in self.active_group_keys:
            grp = self.exp_groups[gkey]
            fv  = self.group_flow[gkey]
            K   = grp["K"]
            depot   = grp["e_depot"]
            sink    = grp["e_sink"]
            exp_from = grp["e_from"]
            exp_to   = grp["e_to"]
            fc: dict[Node, gp.Constr] = {}
            for node in sorted(grp["e_nodes"],
                               key=lambda n: (n.time, n.airport, n.break_expiry)):
                out_arcs = exp_from.get(node, [])
                in_arcs  = exp_to.get(node, [])
                if not out_arcs and not in_arcs:
                    continue
                out_expr = gp.quicksum(fv[a] for a in out_arcs if a in fv) if out_arcs else 0.0
                in_expr  = gp.quicksum(fv[a] for a in in_arcs  if a in fv) if in_arcs  else 0.0
                if node == depot:
                    constr = self.model.addConstr(out_expr - in_expr == K, name=f"fb_depot_g{gkey[0]}_{depot.time}")
                elif sink is not None and node == sink:
                    constr = self.model.addConstr(in_expr - out_expr == K, name=f"fb_sink_g{gkey[0]}_{node.time}")
                else:
                    constr = self.model.addConstr(out_expr == in_expr,
                        name=f"fb_g{len(fc)}_{node.airport}_{node.time}_{node.break_expiry}")
                fc[node] = constr
            grp["flow_constrs"] = fc

        if self.verbose:
            print(f"    Flow balance constraints added (per group)  ({_t.time()-t0:.1f}s)")

        # ── Constraint 2': Coverage ───────────────────────────────
        # Coverage is an EQUALITY: Σ flight-arc flow + slack = r_f, with slack in
        # [0, r_f]. The slack absorbs any shortfall (penalised), and the equality also
        # caps the operator count at r_f — so the flow can no longer OVER-cover a flight
        # by routing surplus crew through it to reposition (which used to happen because
        # deadheading, priced c_fl + fare, is dearer than operating). Excess positioning
        # crew now reroute onto the deadhead arc for the same flight (which exists for
        # every flight and does NOT count toward coverage), so the model stays feasible
        # and each flight is operated by exactly its requirement (or fewer, with slack).
        # Always add the constraint even when fl_arcs is empty (then slack = r_f).
        # Build expanded flight-arc index: flight_id -> [(group_key, arc), ...].
        # Coverage = total crew FLOW on the flight's arcs across all groups.
        _exp_fl: dict[int, list[tuple[tuple, Arc]]] = defaultdict(list)
        for gkey in self.active_group_keys:
            for arc in self.exp_groups[gkey]["e_arcs"]:
                if arc.arc_type == 'flight' and arc.flight_id is not None:
                    _exp_fl[arc.flight_id].append((gkey, arc))

        n_no_arcs = 0
        for f in self.cov_flights:
            pairs = _exp_fl.get(f.id, [])
            if pairs:
                cov_expr = gp.quicksum(self.group_flow[gk][arc] for gk, arc in pairs)
            else:
                cov_expr = 0.0
                n_no_arcs += 1
            constr = self.model.addConstr(
                cov_expr + self.slack_var[f.id] >= f.min_crew,
                name=f"cov_{f.id}",
            )
            self.coverage_constrs[f.id] = constr

        # Soft look-ahead (seam) coverage constraints. Same shape as the hard ones but
        # backed by the smaller-penalty slack; a seam flight with no reachable arc in
        # THIS window simply falls back to slack (no diagnostic — it is not committed).
        for f in self.lookahead_flights:
            pairs = _exp_fl.get(f.id, [])
            cov_expr = gp.quicksum(self.group_flow[gk][arc] for gk, arc in pairs) if pairs else 0.0
            constr = self.model.addConstr(
                cov_expr + self.slack_var[f.id] >= f.min_crew,
                name=f"cov_look_{f.id}",
            )
            self.coverage_constrs[f.id] = constr

        if self.verbose and n_no_arcs:
            print(f"    WARNING: {n_no_arcs} coverage flights have no reachable "
                  f"flight arcs — will be uncovered (slack-only constraint added)")
            # Diagnose each uncoverable flight across three layers:
            #   1. arc exists in base graph at all
            #   2. arc in any crew's base-Dijkstra reachable set
            #   3. arc in any crew's expanded break-clock graph
            base_fl_arcs: dict[int, list[Arc]] = {}
            for fid, arcs in self._arcs_by_flight.items():
                fl_arcs = [a for a in arcs if a.arc_type == 'flight']
                if fl_arcs:
                    base_fl_arcs[fid] = fl_arcs
            for f in self.cov_flights:
                if _exp_fl.get(f.id):
                    continue
                has_base_arc = f.id in base_fl_arcs
                n_base_reach = sum(
                    1 for c in active_crew
                    if any(a.flight_id == f.id and a.arc_type == 'flight'
                           for a in self._base_reachable_arcs.get(c.id, set()))
                )
                print(f"      UNCOVERABLE {f.flight_num:>8s}  "
                      f"{f.origin}→{f.dest}  "
                      f"dep=d{f.dep_min//1440}@{f.dep_min%1440//60:02d}h  "
                      f"base_arc={'yes' if has_base_arc else 'NO'}  "
                      f"dijkstra_reach={n_base_reach}/{len(active_crew)} crew  "
                      f"exp_reach=0")

        self.model.update()
        if self.verbose:
            print(f"    Coverage constraints: {len(self.cov_flights)} hard + "
                  f"{len(self.lookahead_flights)} soft look-ahead  ({_t.time()-t0:.1f}s)")
            print(f"    Model: {self.model.NumVars:,} vars, "
                  f"{self.model.NumConstrs:,} constrs  ({_t.time()-t0:.1f}s)")

        # NOTE: The 4-day away cap and the >=48 h home-break requirement are
        # enforced STRUCTURALLY by the expanded state-space graph built in
        # _compute_base_reachability / expand_break_clock.  Each crew's arc_var
        # is over expanded arcs whose nodes carry a break_expiry dimension; the
        # flow conservation constraints (Constraint 1') guarantee every feasible
        # crew path reaches the SINK_EXPIRY horizon node, which is only reachable
        # via at least one synthetic home-break jump.  No additional LP rows are
        # needed for hb_ or xwhb_ — those blocks are deleted (Step 5).

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

        # Home-return deadhead discount (same logic as in build_model variable creation)
        cost = arc.cost
        if (arc.arc_type == 'deadhead'
                and arc.flight_id is not None
                and C_DH_HOME_RETURN_DISCOUNT > 0):
            fl = next((f for f in self.flights if f.id == arc.flight_id), None)
            if fl is not None and fl.dest == c.base:
                cost *= (1.0 - C_DH_HOME_RETURN_DISCOUNT)

        var = self.model.addVar(lb=0.0, ub=1.0, obj=cost,
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
        home_horizon = self._exp_sink_node.get(crew_id)
        if arc.start in cf:
            self.model.chgCoeff(cf[arc.start], var, +1)

        # Arc is incoming at its end node.
        # Horizon constraint is written as (in - out == 1) so incoming arc is +1 there too.
        # All other constraints are (out - in == RHS) so incoming arc is -1.
        if arc.end in cf:
            coeff = +1 if (home_horizon is not None and arc.end == home_horizon) else -1
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
        crew_depot   = Node(self.crew_start_airport[crew_id], self.depot_start)
        home_horizon = Node(c.base, self.horizon_end)

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

    # ── Solve loop ─────────────────────────────────────────

    def make_integer(self):
        """Constraint 4': make crew-flow integral.

        Group-flow variables are general INTEGER. A flight arc may carry up to
        min_crew(f) crew of one group (the flight needs r_f working crew, not 1), and a
        deadhead arc may carry several. Decomposition splits each unit into its own
        path, so a flight worked by 4 crew yields 4 paths through that flight arc."""
        for fv in self.group_flow.values():
            for var in fv.values():
                var.VType = GRB.INTEGER
        for var in self.slack_var.values():
            var.VType = GRB.INTEGER
        self.model.setParam("OutputFlag", int(self.verbose))
        self.model.update()

    def solve(self) -> dict:
        """
        Solve the window.

        The network is built event-exact (a node at every flight dep/arr plus the
        turnaround-snap node), so the discretisation has no coarsening to refine:
        the old DDD refinement loop converged at iteration 0 in every window and
        never inserted a node.  That loop, its LP-relaxation pre-solve, and the
        violation/refinement machinery (inspect_violations / add_node /
        _expose_flight_arcs_*) have been removed.  We build the integer model and
        hand it straight to Gurobi, which solves its own root relaxation anyway.
        """
        print(f"\n  === Solve (window {self.win.idx}) ===")

        # MIP phase: flip flow vars to integer and solve once.
        self.make_integer()
        # Each slack carries its own penalty in .Obj (C_UNC_EFFECTIVE for committed
        # coverage, C_UNC_LOOKAHEAD for soft seam coverage), so read it per-variable
        # rather than hardcoding the commit penalty for every slack.
        self.model.setObjective(
            gp.quicksum(
                var.Obj * var
                for fv in self.group_flow.values()
                for arc, var in fv.items()
            ) + gp.quicksum(v.Obj * v for v in self.slack_var.values()),
            GRB.MINIMIZE,
        )
        self.model.setParam("MIPGap", 0.01)
        self.model.setParam("TimeLimit", 1800)
        # Barrier (interior-point) on the relaxation: far faster than simplex on these
        # large sparse network LPs (simplex was burning the whole limit at the root).
        # Crossover off — we round/decompose, so we don't need a basic solution.
        # CREW_METHOD env overrides (-1 auto/simplex-concurrent, 2 barrier).
        self.model.setParam("Method", 2)
        self.model.setParam("Crossover", 0)
        # Adaptive crossover (opt-in via CREW_PROBE=<seconds>; default OFF = original
        # single Crossover=0 solve). Unit-demand models (senior layer, single-crew
        # airlines) and most multi-crew windows solve at the ROOT with no basis, so
        # Crossover=0 is fastest and forcing crossover just taxes them. But a few hard
        # multi-crew windows get stuck while BRANCHING — with no warm-start basis each
        # node LP is re-solved almost from scratch (~33k pivots/node). So: probe with
        # Crossover=0 up to PROBE seconds; if it times out still far from optimal (i.e.
        # it IS branching but crawling), reset, switch crossover on for a warm-startable
        # basis + focus on incumbents, and re-solve with the full limit. (A window that
        # is stuck in ROOT cut-gen rather than branching — the 3M-var case — won't be
        # rescued by this; that needs a smaller model.)
        probe = int(os.environ.get("CREW_PROBE", "0"))
        if probe > 0:
            self.model.setParam("TimeLimit", probe)
            self.model.optimize()
            stuck = (self.model.Status == GRB.TIME_LIMIT
                     and (self.model.SolCount == 0 or self.model.MIPGap > 0.05))
            if stuck:
                gap = self.model.MIPGap if self.model.SolCount > 0 else float('inf')
                print(f"    [CREW_PROBE] still stuck after {probe}s (gap={gap:.1%}, "
                      f"nodes={int(self.model.NodeCount)}); switching crossover ON, re-solving")
                self.model.reset()
                self.model.setParam("Crossover", -1)
                self.model.setParam("MIPFocus", 1)
                self.model.setParam("TimeLimit", 1800)
                self.model.optimize()
        else:
            self.model.optimize()

        return self.extract_solution()

    # ── Solution extraction ────────────────────────────────────

    def _decompose_flows(self) -> dict[int, list[Arc]]:
        """Decompose each group's integer crew-flow into one depot→sink path per crew.

        The expanded graph is a time-acyclic DAG, so the integer flow contains no
        cycles and splits cleanly into exactly K_g simple paths — each is one crew's
        chronological leg sequence. Paths are assigned to the group's member crew-ids
        (any order; members are interchangeable)."""
        out: dict[int, list[Arc]] = {}
        for gkey in self.active_group_keys:
            grp = self.exp_groups[gkey]
            fv  = self.group_flow[gkey]
            depot = grp["e_depot"]
            sink  = grp["e_sink"]
            e_from = grp["e_from"]
            flow = {a: int(round(v.X)) for a, v in fv.items()}
            members = grp["active_members"]
            for cid in members:
                path: list[Arc] = []
                node = depot
                for _ in range(100000):
                    if sink is not None and node == sink:
                        break
                    nxt = None
                    for a in e_from.get(node, []):
                        if flow.get(a, 0) > 0:
                            nxt = a
                            break
                    if nxt is None:
                        break          # reached sink (or a dead end if sink is None)
                    flow[nxt] -= 1
                    path.append(nxt)
                    node = nxt.end
                out[cid] = path
        return out

    def extract_solution(self) -> dict:
        eps = 1e-4
        self._crew_active_arcs = {}
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

        # Recover per-crew routes by decomposing the group flows into individual paths.
        self._crew_active_arcs = self._decompose_flows()
        crew_active: dict[int, list[Arc]] = {
            cid: arcs for cid, arcs in self._crew_active_arcs.items() if arcs
        }

        uncovered = []
        for f in self.cov_flights:
            if f.id in self.slack_var:
                sv = self.slack_var[f.id].X
                if sv > eps:
                    uncovered.append((f, sv))

        # Cost accounting straight off the group flows (flow = #crew on the arc).
        flight_cost = dh_cost = wait_cost = 0.0
        for fv in self.group_flow.values():
            for arc, var in fv.items():
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
        Route(c) = {ℓ ∈ A*_c | ℓ.type ≠ wait}
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

            # Start from the crew's expanded depot state (has break_expiry dimension).
            # The base-graph Node(start_ap, depot_start) with NO_STATE won't match
            # any key in `out`, which is keyed by expanded Nodes.
            start_ap = self.crew_start_airport.get(crew_id, c.base)
            curr = self._exp_depot_state.get(
                crew_id, Node(start_ap, self.depot_start)
            )
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

    # ── Carry-over ──────────────────────────────────────────────

    def compute_carry_over(self) -> tuple[dict[str, dict[str, int]], dict[int, ClockState], dict[int, str]]:
        """
        position n_{b,k}^{w+1} = |{c ∈ C_b : loc_w(c) = k}|
        worst-case clock state Γ^{w+1}_c = argmax_{c ∈ C_b} d_work_c
        Falls back to home-base positions if no solution is available.

        Returns a third element: crew_pos — the per-crew-id airport map used to
        build `positions`.  The caller passes this directly into the next window's
        CrewNetwork.__init__ as `carry_crew_pos` so crew_start_airport is
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
            # v4: per-crew arcs come from the flow decomposition, not per-crew vars.
            crew_arcs = self._crew_active_arcs.get(c.id, [])
            in_clk = self.carry_clocks.get(c.id, ClockState())
            start_ap0 = self.crew_start_airport.get(c.id, c.base)
            last_airport = start_ap0
            last_d_work = 0
            last_h_home = 0
            break_end = -LARGE   # away-anchor carried forward (minute crew last left home
                                 # after a >= DELTA_HB break that completed within commit).
            # Seed the away-anchor / home-stay continuously from the previous window so the
            # 4-day budget keeps counting across the seam (depot_start == prev t_commit).
            away_anchor = in_clk.t_last_home_return if in_clk.t_last_home_return >= 0 else -LARGE
            home_since  = self.depot_start if start_ap0 == c.base else -LARGE
            if in_clk.break_in_progress:
                away_anchor = -LARGE          # clock not started yet
                home_since  = self.depot_start

            if not no_solution:
                try:
                    active_arcs = sorted(crew_arcs, key=lambda a: a.true_end)

                    # Walk the COMMITTED arcs (dep < t_commit), tracking the away-anchor
                    # exactly as validate_availability.py reconstructs it from the merged
                    # route.  The anchor advances only when a >= DELTA_HB home stay
                    # completes WITHIN the committed region; a break parked in the tail
                    # (>= t_commit) is NOT credited, so the next window inherits the open
                    # budget and must serve the break itself.  This removes the old
                    # cross-window drift, where the expanded arc's break_expiry credited a
                    # tail break and silently reset the clock every window.
                    last_flight_true_end: int = -1
                    duty_days_set: set[int] = set()

                    for arc in active_arcs:
                        # Commit a leg by its DEPARTURE (matching save_combined_result,
                        # which saves legs with dep < t_commit).
                        if arc.start.time >= t_commit:
                            break
                        last_airport = arc.end.airport

                        if arc.arc_type in ('flight', 'deadhead'):
                            # Arrival at base starts (or continues) a home stay.
                            if arc.end.airport == c.base and home_since < 0:
                                home_since = arc.true_end
                            # Leaving home (re)anchors the away clock — reset only if the
                            # home stay just left was itself a completed >= DELTA_HB break.
                            if arc.start.airport == c.base and arc.end.airport != c.base:
                                served = (home_since >= 0
                                          and (arc.start.time - home_since) >= DELTA_HB)
                                if away_anchor < 0 or served:
                                    away_anchor = arc.start.time
                                home_since = -LARGE
                            if arc.arc_type == 'flight':
                                dep_day = arc.start.time // 1440
                                duty_days_set.add(dep_day)
                                last_flight_true_end = max(last_flight_true_end, arc.true_end)
                        elif arc.arc_type == 'wait' and arc.end.airport == c.base:
                            if home_since < 0:
                                home_since = arc.start.time
                            last_h_home += (arc.end.time - arc.start.time)
                            if arc.end.time - arc.start.time >= DELTA_REST:
                                duty_days_set.clear()
                                last_flight_true_end = -1

                    if (last_flight_true_end >= 0
                            and (t_commit - last_flight_true_end) >= DELTA_REST):
                        last_d_work = 0
                    else:
                        last_d_work = len(duty_days_set)

                    # Carry the away-anchor forward.  Reset (-> break_in_progress, fresh
                    # clock) only when the crew is home at t_commit AND has served a full
                    # >= DELTA_HB break that completed by t_commit.  Otherwise carry the
                    # real anchor so the budget keeps counting; a crew that still owes a
                    # break is forced to serve it early in the next window.
                    if (last_airport == c.base and home_since >= 0
                            and (t_commit - home_since) >= DELTA_HB):
                        break_end = -LARGE          # fully rested -> fresh (BIP below)
                    else:
                        break_end = away_anchor      # away, or owes a break

                except AttributeError:
                    pass  # no solution available; keep defaults

            # Fresh / initial-break state: crew is home at t_commit with the away clock
            # not running (never left, or a full >= DELTA_HB break completed by t_commit).
            bip = (not no_solution
                   and last_airport == c.base
                   and break_end == -LARGE)
            if _DEBUG_CREW_ID is not None and c.id == _DEBUG_CREW_ID:
                print(f"    [CARRY w{self.win.idx}] crew {c.id} -> last_airport={last_airport} "
                      f"{'AWAY' if last_airport != c.base else 'home'} "
                      f"away_anchor={away_anchor} home_since={home_since} "
                      f"break_end(tlhr)={break_end} bip={bip} d_work={last_d_work}")

            # Carry the in-progress home stay across the seam: a crew that is home at
            # t_commit but still owes part of its 48h break (started the break in the
            # committed window) only needs to FINISH it in the next window — credited
            # from home_since, not restarted from depot_start.
            home_carry = home_since if (not bip and last_airport == c.base
                                        and home_since >= 0) else -LARGE
            crew_pos[c.id] = last_airport
            crew_clock[c.id] = ClockState(
                t_reset=0,
                d_work=last_d_work,
                h_home=last_h_home,
                t_last_home_return=break_end,
                away_since=-LARGE,       # structural: enforced by expanded graph
                home_break_until=-LARGE, # structural: enforced by expanded graph
                home_since=home_carry,   # in-progress home break carried across the seam
                break_in_progress=bip,
            )

        # n_{b,k}^{w+1}
        positions: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for c in self.crew:
            positions[c.base][crew_pos[c.id]] += 1

        # Worst-case clock per crew: argmax d_work, tiebreak by t_reset then h_home
        worst_clocks: dict[int, ClockState] = {}
        for c in self.crew:
            # In individual model each crew member has their own clock; just carry it forward
            worst_clocks[c.id] = crew_clock[c.id]

        return dict(positions), worst_clocks, crew_pos


def derive_home_breaks(base: str, legs: list[dict]) -> list[dict]:
    """Mandatory 48h home-break windows, reconstructed from a crew's committed legs.

    The MILP enforces home breaks structurally (expanded-graph reset) but never
    writes the window out, so the viz was left guessing and getting it wrong.
    This mirrors the solver's clocks so the export carries the truth:

      * d_work = consecutive DUTY DAYS in the current away-from-base spell.
        Counts distinct flight calendar-days (several legs in one day = one day,
        matching the Dijkstra prune); any touch of `base` resets it.
      * The streak is capped at D_WORK.  A crew that works an away-spell up to the
        cap and then sits at base for >= DELTA_HB is on its mandatory home break
        for the first DELTA_HB of that stay.

    Spells that hit the cap but turn around in < DELTA_HB get NO window — the crew
    flew again, so it was never actually benched.  Returns a list of
    {"start", "end", "type": "home_48h"} in minutes, same clock as the legs.

    NOTE: gated on the D_WORK duty-day cap, matching "worked the max duty days ->
    mandatory break".  To instead surface EVERY >= DELTA_HB home reset (the broader
    is_reset condition in the expanded graph), drop the `reached` gate below.
    """
    if not base or not legs:
        return []
    seq = sorted(legs, key=lambda l: l.get("dep", 0))
    out: list[dict] = []
    dwork = 0
    last_fly_day = -1
    reached = False
    for i, l in enumerate(seq):
        if l.get("from") == base:
            dwork, last_fly_day, reached = 0, -1, False
        if l.get("type") == "flight":
            dep_day = l.get("dep", 0) // 1440
            if dep_day != last_fly_day:
                dwork += 1
                last_fly_day = dep_day
            if dwork >= D_WORK:
                reached = True
        if l.get("to") == base:
            nxt = seq[i + 1].get("dep") if i + 1 < len(seq) else None
            stay = (nxt - l.get("arr", 0)) if nxt is not None else None
            if reached and (stay is None or stay >= DELTA_HB):
                start = l.get("arr", 0)
                out.append({"start": start, "end": start + DELTA_HB, "type": "home_48h"})
            dwork, last_fly_day, reached = 0, -1, False
    return out


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
            crew_home = crew_base.get(cid, "")
            routes.append({
                "crew_id": cid,
                "base": crew_home,
                "crew_count": 1,
                "legs": deduped,
                "breaks": derive_home_breaks(crew_home, deduped),
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
    crew_override: Optional[list] = None,
    cost_fl: float = C_FL,   # per-minute flight cost (use C_FL_SENIOR for seniors)
    cost_wt: float = C_WT,   # per-minute wait cost  (use C_WT_SENIOR for seniors)
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

    # Build windows across the coverage period.
    # If coverage_days < T_DAYS_COMMIT we still run at least one window.
    effective_days = max(coverage_days, T_DAYS_COMMIT)
    windows = build_windows(effective_days)
    _max_win = int(os.environ.get("CREW_MAX_WINDOWS", "0")) or len(windows)
    print(f"  Rolling horizon: {len(windows)} windows "
          f"(T_solve={T_DAYS_SOLVE}d, T_commit={T_DAYS_COMMIT}d, T_tail={T_DAYS_TAIL}d)"
          + (f"  [CAPPED to first {_max_win}]" if _max_win < len(windows) else ""))

    # Crew pool sized on all flights in the airline dataset — or an explicit pool
    # passed by a caller (e.g. the two-layer driver, which solves seniors and normals
    # as separate crew sets over the same flights).
    crew = crew_override if crew_override is not None else size_crew_bases(flights)

    carry_positions: dict[str, dict[str, int]] = {}
    # Window 0: every crew is currently at home in their ongoing break.
    # The away-clock starts only when they first leave home; until then the
    # break_expiry stays at BREAK_IN_PROGRESS (= 0) and the expanded graph
    # sets expiry = _bucket_day(departure_time + DELTA_AWAY) on first departure.
    carry_clocks: dict[int, ClockState] = {
        c.id: ClockState(break_in_progress=True) for c in crew
    }
    carry_crew_pos: dict[int, str] = {}   # Fix 2: per-crew-id airport from previous window
    all_results = []
    window_entries: list[dict] = []   # accumulate for combined save

    for win in windows:
        if win.idx >= _max_win:
            print(f"\n  [CREW_MAX_WINDOWS] stopping after window {_max_win-1}")
            break
        t0 = _t.time()
        f_win, f_cov, f_look = slice_flights(flights, win)

        if not f_cov:
            print(f"\n  Window {win.idx}: no coverage flights — skipping")
            continue

        print(f"\n  Window {win.idx}: {len(f_win)} total, {len(f_cov)} need coverage, "
              f"{len(f_look)} seam look-ahead  "
              f"[t={win.t_start//1440}d – commit={win.t_commit//1440}d – hor={win.t_hor//1440}d]")

        net = CrewNetwork(
            flights=f_win,
            cov_flights=f_cov,
            crew=crew,
            win=win,
            carry_positions=carry_positions,
            carry_clocks=carry_clocks,
            carry_crew_pos=carry_crew_pos or None,   # Fix 2: deterministic per-crew positions
            lookahead_flights=f_look,
            verbose=verbose,
            cost_fl=cost_fl,
            cost_wt=cost_wt,
        )
        net.build_initial_network()
        net.build_model()
        result = net.solve()

        # Carry-over for next window
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
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 30

    # Tally flights per operating carrier.
    with open(path, encoding="utf-8") as _f:
        _counts = _Counter(row["OP_CARRIER"].strip()
                           for row in _csv.DictReader(_f)
                           if row.get("OP_CARRIER", "").strip())

    ranked = _counts.most_common()              # [(code, n), ...] high -> low
    print(f"\n{len(ranked)} airlines found in {path}:\n")
    for i, (code, n) in enumerate(ranked, 1):
        print(f"  [{i:2d}]  {code:4s}  {n:>9,} flights")

    # Airline may be given as the 3rd CLI arg (code or list number); else prompt.
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

    print(f"\nSelected airline: {airline} ({_counts[airline]:,} flights)")
    main(path, days=days, out_dir="results", verbose=True, airlines_filter=[airline])