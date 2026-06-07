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
  s_min=2, s_max=120
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
S_MIN = 2             # minimum crew at any base — 2 not 5: ensures a backup crew
                      # is available when the primary crew member is on home break
                      # (48h), while avoiding the original S_MIN=5 problem of
                      # flooding regional bases (peak demand=1) with 5 crew who
                      # have no local work and get drafted as ORD labour for 30 days
S_MAX = 120           # maximum crew at any single base (raised to allow ORD to be
                      # correctly staffed: ORD carries ~49% of all flights)
RANDOM_SEED = 42

# Crew base assignment (slide 12)
MIN_CREW_PER_BASE     = S_MIN          # alias used by assign_crew_bases
MAX_BASES: int | None = None           # None = no cap on number of bases
BASE_SELECTION        = "demand"       # strategy: "demand" | "all"
SATELLITE_RADIUS_MI   = 150.0          # max radius (mi) for satellite pre-positioning
SATELLITE_MIN_FLIGHTS = 3              # min daily departures to qualify as satellite
AIRPORT_COORDS_CSV: str | None = None  # optional CSV: IATA,lat,lon

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
D_AWAY      = 4       # return window in days
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
    t_reset: int            = 0      # last reset time (min from week start)
    d_work: int             = 0      # consecutive work-days since last rest
    h_home: int             = 0      # home-wait accumulated since last departure (min)
    t_last_home_return: int = -LARGE # absolute minute of last arrival at home base;
                                     # -LARGE means never returned (no prior break needed)


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
# n_p = max(n_min, ceil( (Σ_{f: f.orig=p} m_f * d_f / τ_duty) * 1.5 + noise ))
# ─────────────────────────────────────────────────────────────────────────────

def _haversine_mi(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance in miles between two (lon, lat) points."""
    R = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def assign_crew_bases(
    flights: list[Flight],
    seed: int = RANDOM_SEED,
    max_bases: int | None = MAX_BASES,
    strategy: str = BASE_SELECTION,
    satellite_radius_mi: float = SATELLITE_RADIUS_MI,
    satellite_min_flights: int = SATELLITE_MIN_FLIGHTS,
    airport_coords_csv: str | None = AIRPORT_COORDS_CSV,
) -> list[CrewMember]:
    """
    Select crew bases and assign crew counts.

    Sizing uses the larger of two estimates per base:

    (A) Duration-weighted demand (slide 12 formula):
          n_demand = ceil( (Σ_{f: orig=p} m_f·d_f / τ_duty) · buffer )
        where τ_duty = 8h/day · horizon_days · CREW_UTILISATION
        The utilisation discount (~0.55) accounts for home-break (48h),
        consecutive-day limits (D_WORK=3), and rest requirements eating
        into theoretical crew availability.

    (B) Peak concurrent crew demand:
          n_peak = max over all t of Σ_{f departing at t from p} m_f
        This catches high-frequency hubs (e.g. ORD) where individual
        flights are short but many depart simultaneously.

    Final count = max(S_MIN, min(S_MAX, max(n_demand, n_peak) · buffer + noise))

    Key changes vs old formula:
    - CREW_UTILISATION discount in tau_duty prevents chronic ORD understaffing
    - S_MIN=1 (not 5): prevents regional bases with peak demand=1 from getting
      5 crew who have no local work and get drafted as ORD labour for 30 days
    - S_MAX=120: allows ORD to be correctly staffed (~49% of all flights)
    - Peak concurrent demand as floor prevents the duration formula from giving
      zero crew to a base that has several simultaneous flights of short duration
    """
    rng = random.Random(seed)
    all_airports = sorted(set(f.origin for f in flights) | set(f.dest for f in flights))

    # Demand: Σ_{f: orig=p} m_f · d_f   (slide 12 numerator)
    demand_minutes: dict[str, float] = defaultdict(float)
    dep_count: dict[str, int] = defaultdict(int)
    for f in flights:
        demand_minutes[f.origin] += f.min_crew * f.duration
        dep_count[f.origin] += 1

    # Peak concurrent crew demand per origin airport.
    # For each base, sweep all flight events (dep=+crew, arr=-crew) and record
    # the maximum simultaneous crew load.  This is the hard floor: you need at
    # least this many crew available at this airport at the same time.
    peak_concurrent: dict[str, int] = defaultdict(int)
    from collections import defaultdict as _dd
    events_by_ap: dict[str, list[tuple[int, int]]] = _dd(list)
    for f in flights:
        events_by_ap[f.origin].append((f.dep_min,  +f.min_crew))
        events_by_ap[f.origin].append((f.arr_min,  -f.min_crew))
    for ap, evts in events_by_ap.items():
        cur = pk = 0
        for _, delta in sorted(evts):
            cur += delta
            pk = max(pk, cur)
        peak_concurrent[ap] = pk

    # Only origin airports can host bases
    hub_airports = sorted(ap for ap in all_airports if demand_minutes[ap] > 0)
    if max_bases is not None and len(hub_airports) > max_bases:
        hub_airports = sorted(
            hub_airports,
            key=lambda ap: demand_minutes[ap],
            reverse=True,
        )[:max_bases]

    # Optional coordinate data for satellite detection
    coords: dict[str, tuple[float, float]] | None = None
    if airport_coords_csv and os.path.exists(airport_coords_csv):
        coords = {}
        with open(airport_coords_csv, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                iata = row.get("IATA", row.get("iata", "")).strip().upper()
                try:
                    coords[iata] = (float(row["lon"]), float(row["lat"]))
                except (KeyError, ValueError):
                    pass

    # ── Satellite pre-positioning ─────────────────────────────────────────────
    hub_set = set(hub_airports)
    satellite_airports: dict[str, str] = {}   # satellite_ap -> nearest_hub

    horizon_days = max(f.arr_min for f in flights) / 1440 if flights else 3

    if coords:
        for ap in all_airports:
            if ap in hub_set or ap not in coords:
                continue
            if demand_minutes[ap] == 0:
                continue
            daily_deps = dep_count[ap] / max(1.0, horizon_days)
            if daily_deps < satellite_min_flights:
                continue
            nearest = min(
                (h for h in hub_airports if h in coords),
                key=lambda h: _haversine_mi(*coords[ap], *coords[h]),
                default=None,
            )
            if nearest is not None:
                dist = _haversine_mi(*coords[ap], *coords[nearest])
                if dist <= satellite_radius_mi:
                    satellite_airports[ap] = nearest

    # ── τ_duty: utilisation-discounted available duty-minutes per crew member ──
    # 8h/day * horizon_days is the theoretical maximum.  CREW_UTILISATION (~0.55)
    # discounts for home-break obligations, consecutive-day limits, and rest gaps.
    tau_duty = 8 * 60 * max(1.0, horizon_days) * CREW_UTILISATION

    # ── Size each base ────────────────────────────────────────────────────────
    all_bases = sorted(hub_set | set(satellite_airports.keys()))
    base_counts: dict[str, int] = {}

    for ap in all_bases:
        demand = demand_minutes.get(ap, 0.0)
        peak   = peak_concurrent.get(ap, 0)

        if ap in satellite_airports:
            # Satellites: take max of duration formula and peak, 1.2x buffer, no noise
            n_demand = math.ceil((demand / tau_duty) * 1.2) if demand > 0 else 0
            n_peak   = math.ceil(peak * 1.2)
            needed   = max(n_demand, n_peak, MIN_CREW_PER_BASE)
            base_counts[ap] = max(MIN_CREW_PER_BASE, min(S_MAX, needed))
        else:
            # Hubs: take max of duration formula and peak, 1.5x buffer + noise.
            # 1.5x accounts for the fact that home-break obligations,
            # consecutive-day limits, and rest gaps mean crew are only scheduleable
            # ~55% of the horizon — the CREW_UTILISATION discount in tau_duty handles
            # the denominator, but the numerator buffer must also be generous enough
            # that the solver has slack to satisfy all rest constraints simultaneously.
            n_demand = math.ceil((demand / tau_duty) * 1.5) if demand > 0 else 0
            n_peak   = math.ceil(peak * 1.5)
            needed   = max(n_demand, n_peak, MIN_CREW_PER_BASE)
            noisy    = int(rng.gauss(needed, max(1, needed * 0.10)))
            base_counts[ap] = max(MIN_CREW_PER_BASE, min(S_MAX, noisy))

    # ── Build CrewMember list ─────────────────────────────────────────────────
    crew_list: list[CrewMember] = []
    cid = 0
    for ap in sorted(all_bases):
        for _ in range(base_counts[ap]):
            crew_list.append(CrewMember(id=cid, base=ap))
            cid += 1

    # ── Diagnostics ───────────────────────────────────────────────────────────
    total = len(crew_list)
    n_hub_crew = sum(base_counts[ap] for ap in hub_airports if ap in base_counts)
    n_sat_crew = sum(base_counts[ap] for ap in satellite_airports)
    total_demand    = sum(demand_minutes.values())
    total_available = sum(base_counts[ap] * tau_duty for ap in all_bases)

    print(f"  Created {total:,} crew  "
          f"({n_hub_crew} at {len(hub_airports)} hubs  +  "
          f"{n_sat_crew} at {len(satellite_airports)} satellites)")
    print(f"  Utilisation discount      :  {CREW_UTILISATION:.0%}  "
          f"(tau_duty = {tau_duty/60:.0f}h per crew over {horizon_days:.0f}d horizon)")
    print(f"  Total crew-minutes needed :  {total_demand:,.0f}")
    print(f"  Available crew-minutes    :  {total_available:,.0f}")
    print(f"  Coverage ratio            :  {total_available / max(1, total_demand):.2f}x")

    # Per-base sizing breakdown for the top 10 bases by crew count
    top_bases = sorted(base_counts, key=lambda x: -base_counts[x])[:10]
    print(f"  Top bases by crew count:")
    for ap in top_bases:
        peak = peak_concurrent.get(ap, 0)
        print(f"    {ap}: {base_counts[ap]} crew  (peak concurrent={peak}, "
              f"demand={demand_minutes.get(ap,0):,.0f} min)")

    # Warn about airports far from every base (likely uncoverable)
    if coords and hub_airports:
        unreachable = [
            ap for ap in all_airports
            if ap not in set(all_bases)
            and ap in coords
            and min(
                _haversine_mi(*coords[ap], *coords[h])
                for h in hub_airports if h in coords
            ) > satellite_radius_mi * 1.5
        ]
        if unreachable:
            print(
                f"  WARNING: {len(unreachable)} airports are >{satellite_radius_mi * 1.5:.0f} mi "
                f"from any base (likely uncoverable): "
                f"{sorted(unreachable, key=lambda a: dep_count.get(a, 0), reverse=True)[:8]}"
            )

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
        # State: (cost, d_work_days, t_last_home, node)
        # t_last_home = absolute minute when crew last touched home base
        pq = []
        for s in sources:
            dist[s] = 0.0
            # Honour carry-over away clock.
            # If the crew starts at home (or no base tracking), reset t_last_home
            # to the depot time so the away budget starts fresh.
            # If they start away from home, use init_t_last_home so the D_AWAY
            # budget is correctly consumed from how long they've already been out.
            if not base_airport or s.airport == base_airport or init_t_last_home < 0:
                t_last_home = s.time
            else:
                t_last_home = init_t_last_home
            heapq.heappush(pq, (0.0, init_dwork, t_last_home, s))
        visited: dict[Node, tuple] = {}
        while pq:
            d, dw, tlh, u = heapq.heappop(pq)
            if u in visited:
                continue
            visited[u] = (dw, tlh)
            for arc in arcs_from.get(u, []):
                v = arc.end
                if v not in node_set:
                    continue
                # Reset away clock when crew returns to home base
                if base_airport and arc.end.airport == base_airport:
                    new_tlh = arc.true_end
                else:
                    new_tlh = tlh

                # d_work: max consecutive flight-days (reset on home wait)
                if arc.arc_type == 'flight':
                    new_dw = dw + 1
                elif arc.arc_type == 'wait' and base_airport and arc.end.airport == base_airport:
                    new_dw = 0   # rest at home resets consecutive work counter
                else:
                    new_dw = dw
                if new_dw > D_WORK:
                    continue

                # d_away: time since last home touch must not exceed DELTA_AWAY
                time_away = arc.true_end - new_tlh
                if arc.arc_type in ('flight', 'deadhead') and time_away > DELTA_AWAY:
                    continue

                nd = d + arc.cost
                if nd < dist.get(v, INF):
                    dist[v] = nd
                    heapq.heappush(pq, (nd, new_dw, new_tlh, v))
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
        for ap in self.nodes_by_airport:
            ap_nodes = self.nodes_by_airport[ap]
            for i in range(len(ap_nodes) - 1):
                self._make_wait_arc(ap_nodes[i], ap_nodes[i+1])

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

        Home-spine wait arcs (both endpoints at the crew's home airport, or at their
        start airport for this window) bypass the reachability check entirely.
        They form the depot->...->horizon chain every crew member must traverse.
        """
        if arc.arc_type == 'wait':
            ap = arc.start.airport
            if ap == crew.base or ap == self.crew_start_airport.get(crew.id, crew.base):
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
            horizon_node = Node(base, self.horizon_end)

            # Depot sources: home base + any off-base start positions for this base's crew
            depot_sources: list[Node] = []
            home_depot = Node(base, self.depot_start)
            if home_depot in self.nodes:
                depot_sources.append(home_depot)
            for c in self.crew:
                if c.base != base:
                    continue
                sa = self.crew_start_airport.get(c.id, base)
                if sa != base:
                    n = Node(sa, self.depot_start)
                    if n in self.nodes and n not in depot_sources:
                        depot_sources.append(n)

            if not depot_sources or horizon_node not in self.nodes:
                self._base_reachable_arcs[base] = set()
                continue

            # Worst-case carry-over clock for this base.
            # max_dwork: use the highest d_work so reachability is pruned for
            # the most constrained crew member (conservative / correct).
            # min_t_last_home: use the earliest last-home-touch so the away
            # clock is most consumed — again the conservative choice.
            # Both default to values that impose no extra constraint when there
            # is no carry-over (window 0 or no prior solution).
            base_clocks = [self.carry_clocks.get(c.id, ClockState())
                           for c in self.crew if c.base == base]
            max_dwork = max((cs.d_work for cs in base_clocks), default=0)
            # Only consider crews whose start airport for this window is NOT home
            # (i.e. they are actually away); for crews starting at home the away
            # clock is irrelevant and we don't want to penalise reachability.
            away_last_homes = [
                cs.t_last_home_return
                for c in self.crew if c.base == base
                for cs in [self.carry_clocks.get(c.id, ClockState())]
                if self.crew_start_airport.get(c.id, base) != base
                and cs.t_last_home_return >= 0
            ]
            # init_t_last_home: earliest (most constrained) last-home time among
            # away crew, or -1 if all crew start at home this window.
            init_t_last_home = min(away_last_homes) if away_last_homes else -1

            reachable_nodes = compute_reachable(
                nodes=node_list,
                arcs_from=arcs_from_d,
                arcs_to=arcs_to_d,
                depot_nodes=depot_sources,
                horizon_nodes=[horizon_node],
                t_hor=self.horizon_end,
                base_airport=base,
                init_dwork=max_dwork,
                init_t_last_home=init_t_last_home,
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
                      f"({n_iso_crew} crew — tier-1 drop: no flight/dh arcs reachable)")

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
        if prev_node:
            self._make_wait_arc(prev_node, new_node)
        if next_node:
            self._make_wait_arc(new_node, next_node)

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

        # ── Per-base reachability (prune variables before creation) ───────────
        self._base_reachable_arcs = {}
        self._compute_base_reachability()

        # ── Stranded-crew recovery ─────────────────────────────────────────────
        # A crew carried forward to an off-base airport (from the previous window's
        # committed position) can become stranded if that airport has no outgoing
        # flight, deadhead, OR wait arcs in THIS window's graph — making the flow
        # balance constraint demand 1 unit departs a completely dead node.
        #
        # IMPORTANT: "no reachable flight/dh arcs" is NOT the right test.
        # A crew at an off-base airport after hitting D_WORK consecutive duty days
        # will have ALL flight/dh arcs pruned by the clock-constrained Dijkstra
        # (init_dwork >= D_WORK -> new_dw > D_WORK on first flight -> prune).
        # That is correct model behaviour: they need a rest wait before flying again.
        # Resetting them to home base in this case is a TELEPORT, not a recovery.
        #
        # Correct detection: the crew is truly stranded only if Node(start_ap, depot_start)
        # has NO arcs at all in self.arcs_from (not even a wait arc).  A wait arc is
        # always usable (bypasses reachability in _crew_can_use_arc) and will let the
        # crew rest until a flight/dh arc becomes feasible.
        #
        # If Node(start_ap, depot_start) doesn't even exist in the graph (the airport
        # had no activity at all this window), we also reset — that is a genuine dead-end.
        n_stranded_reset = 0
        for c in self.crew:
            start_ap = self.crew_start_airport.get(c.id, c.base)
            if start_ap == c.base:
                continue  # already home, nothing to check
            depot_node = Node(start_ap, self.depot_start)
            # Any arc from depot_node (including wait) is enough to avoid infeasibility.
            # Wait arcs bypass the reachability check and allow the crew to rest in place.
            has_any_arc = bool(self.arcs_from.get(depot_node))
            if not has_any_arc:
                self.crew_start_airport[c.id] = c.base
                n_stranded_reset += 1
        if self.verbose and n_stranded_reset:
            print(f"    Stranded-crew reset: {n_stranded_reset} crew returned to home base "
                  f"(carry-over airport has no arcs at all in this window's graph)")

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
            reachable = self._base_reachable_arcs.get(c.base, set())
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
            for t_dep, dep_arc in dep_list:
                t_lo   = t_dep - DELTA_HB
                lo_idx = _bisect.bisect_left(inb_list, (t_lo,))
                hi_idx = _bisect.bisect_left(inb_list, (t_dep,))
                recent_inbound = [arc for _, arc in inb_list[lo_idx:hi_idx]]
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
            t_ret = clock.t_last_home_return
            if t_ret < 0:
                # Never returned home before this window; no embargo needed
                continue
            embargo_end = t_ret + DELTA_HB
            if embargo_end <= self.win.t_start:
                # Break already completed before this window started; nothing to block
                continue
            # Some or all of the 48-hour break must be served during this window.
            # Block every departure arc from home base with dep_time < embargo_end.
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
            t_ret = clock.t_last_home_return
            if t_ret < 0:
                continue
            embargo_end = t_ret + DELTA_HB
            if embargo_end > self.win.t_start:
                self._xw_hb_embargo[c.id] = embargo_end

        self.model.update()
        if self.verbose and n_xw_hb:
            print(f"    Cross-window home-break constraints: {n_xw_hb}  "
                  f"({len(self._xw_hb_embargo)} crew under embargo)  "
                  f"({_t.time()-t0:.1f}s)")

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

        var = self.model.addVar(lb=0.0, ub=ub, obj=arc.cost,
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
                    for arc in active_arcs:
                        if arc.end.time > t_commit:
                            break
                        last_airport = arc.end.airport
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
        result = net.solve()

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

    # Auto-detect the airline with the fewest flights in the CSV (fastest to solve)
    with open(path, encoding="utf-8") as _f:
        _counts = _Counter(row["OP_CARRIER"].strip()
                           for row in _csv.DictReader(_f)
                           if row.get("OP_CARRIER", "").strip())
    airline = _counts.most_common()[-1][0]
    print(f"Airline of interests: {airline} ({_counts[airline]:,} rows)")

    main(path, days=days, out_dir="results", verbose=True, airlines_filter=[airline])