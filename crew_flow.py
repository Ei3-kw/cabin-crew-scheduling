"""
Cabin Crew Pairing via Integer Flow
=====================================
Solves the minimum-cost crew pairing problem for US domestic flights.

Problem:
  - Every flight must be staffed with at least MIN_CABIN_CREW crew members
  - Crew start and end at their home base airport
  - Crew can deadhead (ride as passenger) to reposition
  - Crew wait at airports between flights (layover cost if overnight)
  - Minimise: flight-hour costs + deadhead costs + layover costs

Method:
  - Build a time-expanded network: nodes = (airport, exact_flight_time)
  - Arcs: flight | deadhead | wait
  - Variables: integer FLOW per arc (not per crew member)
      f_work[arc] = # crew working this arc
      f_dh[arc]   = # crew deadheading this arc
      f_wait[arc] = # crew waiting on this arc
  - Flow balance per base: supply = crew_count[base] at depot,
                           demand = crew_count[base] at horizon
  - Coverage: f_work[flight_arc] >= flight.min_crew
  - Deadhead = surplus flow above min_crew (priced separately)
  - Solve directly as MIP (no DDD needed — network already uses exact flight times)

Stage 2 (roster assignment):
  - After solving, decompose flows into individual crew routes
  - Assign named crew IDs to routes by base via flow decomposition
  - Each crew member gets one route starting and ending at their home base

Rolling Horizon:
  - ROLLING_HORIZON = True enables a sliding-window solve instead of one
    monolithic 30-day MIP.
  - The horizon is divided into windows of WINDOW_DAYS days, advancing by
    STEP_DAYS each iteration (overlap = WINDOW_DAYS - STEP_DAYS).
  - At each step:
      1. Solve only the current window (+ RETURN_WINDOW tail for return arcs).
      2. Fix the crew positions at the end of STEP_DAYS as the warm-start
         supply vector for the next window ("state hand-off").
      3. Append the window's routes to the global result.
  - Why rolling horizon for a month?
      * A 30-day monolithic MIP has O(30×daily_arcs) variables — typically
        10–50× more than a 3-day window — and B&B scales super-linearly.
      * Rolling horizon gives near-optimal solutions in a fraction of the time,
        at the cost of a small boundary effect (crew positions are fixed, not
        re-optimised across the cut point).
      * The overlap (WINDOW_DAYS - STEP_DAYS) lets the solver "look ahead"
        past the commit horizon so boundary flights aren't systematically
        under-staffed.

Key advantages over individual-variable formulation:
  - Variables: O(|arcs|) instead of O(|crew| × |arcs|)
  - Constraints: O(|nodes|) instead of O(|crew| × |nodes|)
  - Crew identity restored in Stage 2 (flow decomposition) — trivial post-solve
  - Same optimality guarantees; same result file format

Data:
  - flights_enriched.csv (BTS On-Time + FAA registry join)
  - Uses first DAYS_TO_SOLVE days; bases chosen by weighted k-median +
    satellite pre-positioning
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
from sortedcontainers import SortedList

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
DAYS_TO_SOLVE      = 30
RETURN_WINDOW      = 7
RANDOM_SEED        = 42069
MIN_CREW_PER_BASE  = 5
MAX_BASES          = 20       # number of full crew bases (k-median hubs)
TIME_BUCKET        = 15
MIN_TURNAROUND     = 45
MIN_REST           = 8 * 60
OVERNIGHT_THRESHOLD = 4 * 60

# ── Rolling horizon ───────────────────────────
# Set True to solve day-by-day windows instead of one monolithic MIP.
# Recommended for DAYS_TO_SOLVE >= 14.
ROLLING_HORIZON    = True
WINDOW_DAYS        = 7        # planning window per solve (days)
STEP_DAYS          = 3        # days committed per step (< WINDOW_DAYS for overlap)

# ── Base selection strategy ───────────────────
# 'volume'   : legacy — top-N airports by departure count (poor geographic coverage)
# 'kmedian'  : Option 2 — minimise max deadhead distance from any flight origin to
#              its nearest base; requires airport lat/lon in the CSV or FAA data
# 'kmedian+satellite' : Option 2 + Option 3 — k-median hubs with satellite
#              pre-positioning for airports within SATELLITE_RADIUS_MI of a hub
BASE_SELECTION     = 'kmedian+satellite'
SATELLITE_RADIUS_MI = 250        # airports within this radius get pre-positioned crew
SATELLITE_MIN_FLIGHTS = 3        # ignore satellite airports with fewer daily flights
AIRPORT_COORDS_CSV  = None       # path to CSV with columns: airport,lat,lon
                                 # if None, uses built-in US airport coordinate table

COST_FLIGHT_HOUR   = 100.0
COST_DEADHEAD_BASE = 20.0
COST_LAYOVER_MIN   = 0.5
COST_OVERNIGHT     = 500.0
COST_UNCOVERED     = 1e7

MAX_DUTY_MINUTES   = 20 * 60    # max flying minutes before mandatory MIN_REST (reset anywhere)
MAX_FLYING         = 14 * 60   # alias used in comments; duty is flying-only, matches MAX_DUTY_MINUTES intent

MAX_DUTY_DAYS      = 5          # wall-clock span of a duty period (days); used in network pruning

# Home-reset clocks — enforced per-crew in Stage 2 (flow decomposition) AND
# as penalty terms in the MIP objective so the solver avoids violations.
# Both MAX_WORK_DAY and MAX_AWAY_DAYS can ONLY be reset after HOME_BREAK_DAYS
# consecutive days at the home base.
MAX_WORK_DAY       = 5          # max calendar days with any flight before mandatory home break
MAX_AWAY_DAYS      = 7          # max calendar days away from home base before mandatory home break
HOME_BREAK_DAYS    = 2          # full calendar days at home base required to reset both clocks
HOME_BREAK         = HOME_BREAK_DAYS * 1440  # same in minutes (2880)

FARE_BASE          = 50.0
FARE_PER_MILE      = 0.15
LF_LOW_THRESHOLD   = 0.75
LF_HIGH_THRESHOLD  = 0.90

DEPOT_TIME_START   = 0
LARGE              = int(1e9)


# ─────────────────────────────────────────────
# DATA STRUCTURES
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
# CSV PARSING
# ─────────────────────────────────────────────

def parse_hhmm(s: str) -> int:
    s = s.strip().zfill(4)
    return int(s[:2]) * 60 + int(s[2:])


def _find_week_start(filepath: str) -> datetime:
    """
    Two-pass helper: scan the entire CSV to find the true minimum FL_DATE.

    FIX: the original code set week_start from the first row encountered,
    which may not be chronologically first (CSV is sorted by carrier, not
    date). Any flight before that row got day_offset < 0 and was silently
    dropped, causing the 'nothing after day N' symptom.
    """
    date_fmt_options = ["%m/%d/%Y %I:%M:%S %p", "%Y-%m-%d", "%m/%d/%Y"]
    min_date = None

    with open(filepath, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            fl_date_str = row.get('FL_DATE', '').strip()
            fl_date = None
            for fmt in date_fmt_options:
                try:
                    fl_date = datetime.strptime(fl_date_str, fmt).date()
                    break
                except ValueError:
                    continue
            if fl_date is None:
                continue
            if min_date is None or fl_date < min_date:
                min_date = fl_date

    if min_date is None:
        raise ValueError("No valid FL_DATE values found in CSV.")

    return datetime(min_date.year, min_date.month, min_date.day)


def parse_flights(
    filepath: str,
    days: int,
    horizon_days: int | None = None,
    week_start: datetime | None = None,
) -> tuple[list[Flight], datetime]:
    """
    Parse flights from CSV.

    Parameters
    ----------
    filepath     : path to flights_enriched.csv
    days         : number of days that need crew coverage (needs_coverage=True)
    horizon_days : total days to load (>= days; extra tail used for return arcs)
    week_start   : anchor date; if None, determined by scanning the full CSV
                   for the minimum FL_DATE (two-pass read — fixes the
                   first-row-wins bug in the original code).
    """
    if horizon_days is None:
        horizon_days = days

    # ── Determine anchor date ─────────────────────────────────────────────────
    if week_start is None:
        week_start = _find_week_start(filepath)

    date_fmt_options = ["%m/%d/%Y %I:%M:%S %p", "%Y-%m-%d", "%m/%d/%Y"]
    flights = []
    fid = 0

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

    if not flights:
        raise ValueError("No valid flights found in CSV.")

    n_cov  = sum(1 for f in flights if getattr(f, 'needs_coverage', True))
    n_repo = len(flights) - n_cov
    print(f"Loaded {len(flights)} flights from {week_start.date()}  "
          f"({n_cov} need coverage, {n_repo} return-window repositioning arcs)")
    return flights, week_start


# ─────────────────────────────────────────────
# AIRPORT COORDINATES
# ─────────────────────────────────────────────

# Condensed table of ~600 US commercial airports (IATA -> (lat, lon)).
# Covers every airport that appears in BTS domestic on-time data.
# Generated from the FAA NASR 56-day subscription (public domain).
_US_AIRPORT_COORDS: dict[str, tuple[float, float]] = {
    "ABE": (40.6521, -75.4408), "ABI": (32.4113, -99.6819), "ABQ": (35.0402, -106.6090),
    "ABR": (45.4491, -98.4218), "ABY": (31.5355, -84.1945), "ACK": (41.2531, -70.0600),
    "ACT": (31.6113, -97.2305), "ACV": (40.9781, -124.1086), "ACY": (39.4576, -74.5772),
    "ADK": (51.8780, -176.6460), "ADQ": (57.7500, -152.4938), "AEX": (31.3274, -92.5488),
    "AGS": (33.3699, -81.9645), "AKN": (58.6768, -156.6493), "ALB": (42.7483, -73.8017),
    "ALO": (42.5571, -92.4003), "ALS": (37.4348, -105.8662), "ALW": (46.0949, -118.2880),
    "AMA": (35.2194, -101.7059), "ANC": (61.1744, -149.9961), "ANI": (61.5816, -159.5427),
    "ANK": (39.9503, -32.8836), "APF": (26.1526, -81.7753), "APN": (45.0781, -83.5603),
    "ART": (43.9919, -76.0216), "ARV": (45.9279, -89.7309), "ASE": (39.2232, -106.8688),
    "ATL": (33.6407, -84.4277), "ATW": (44.2581, -88.5191), "AUS": (30.1945, -97.6699),
    "AVL": (35.4362, -82.5418), "AVP": (41.3385, -75.7234), "AZA": (33.3078, -111.6553),
    "BDL": (41.9389, -72.6832), "BET": (60.7798, -161.8380), "BFL": (35.4336, -119.0568),
    "BGM": (42.2087, -75.9798), "BGR": (44.8074, -68.8281), "BHM": (33.5629, -86.7535),
    "BIL": (45.8077, -108.5430), "BIS": (46.7727, -100.7463), "BJI": (47.5095, -94.9338),
    "BKG": (36.5321, -93.2001), "BLI": (48.7928, -122.5375), "BLV": (38.5452, -89.8352),
    "BMI": (40.4771, -88.9159), "BNA": (36.1245, -86.6782), "BOI": (43.5644, -116.2228),
    "BOS": (42.3643, -71.0052), "BPT": (30.0671, -94.0207), "BQK": (31.2588, -81.4665),
    "BQN": (18.4949, -67.1294), "BRD": (46.3983, -94.1381), "BRL": (40.7832, -91.1255),
    "BRO": (25.9068, -97.4259), "BRW": (71.2854, -156.7661), "BTM": (45.9548, -112.4972),
    "BTR": (30.5332, -91.1496), "BTV": (44.4720, -73.1533), "BUF": (42.9405, -78.7322),
    "BUR": (34.2007, -118.3590), "BWI": (39.1754, -76.6683), "BZN": (45.7775, -111.1531),
    "CAE": (33.9389, -81.1195), "CAK": (40.9161, -81.4422), "CDC": (37.7010, -113.0988),
    "CDV": (60.4917, -145.4778), "CHA": (35.0353, -85.2038), "CHO": (38.1386, -78.4529),
    "CHS": (32.8986, -80.0405), "CID": (41.8847, -91.7108), "CIU": (46.2508, -84.4724),
    "CKB": (39.2966, -80.2282), "CLD": (33.1283, -117.2790), "CLE": (41.4117, -81.8498),
    "CLT": (35.2140, -80.9431), "CMH": (39.9980, -82.8919), "CMI": (40.0399, -88.2781),
    "CMX": (47.1684, -88.4889), "CNY": (38.7559, -109.7549), "COD": (44.5202, -109.0238),
    "COU": (38.8181, -92.2196), "CPR": (42.9080, -106.4644), "CRP": (27.7704, -97.5012),
    "CRW": (38.3731, -81.5932), "CSG": (32.5163, -84.9389), "CVG": (39.0488, -84.6678),
    "CWA": (44.7776, -89.6667), "DAB": (29.1799, -81.0581), "DAL": (32.8471, -96.8517),
    "DAY": (39.9024, -84.2194), "DBQ": (42.4020, -90.7095), "DCA": (38.8521, -77.0377),
    "DEN": (39.8561, -104.6737), "DFW": (32.8998, -97.0403), "DHN": (31.3213, -85.4496),
    "DIK": (46.7975, -102.8020), "DLG": (59.0445, -158.5069), "DLH": (46.8421, -92.1936),
    "DRO": (37.1515, -107.7538), "DSM": (41.5340, -93.6631), "DTW": (42.2124, -83.3534),
    "DVL": (48.1142, -98.9088), "EAU": (44.8658, -91.4843), "EGE": (39.6426, -106.9177),
    "EKO": (40.8249, -115.7919), "ELM": (42.1599, -76.8916), "ELP": (31.8072, -106.3779),
    "ERI": (42.0831, -80.1739), "ESC": (45.7227, -87.0936), "EUG": (44.1246, -123.2119),
    "EVV": (37.9329, -87.5324), "EWN": (35.0730, -77.0430), "EWR": (40.6925, -74.1687),
    "EYW": (24.5561, -81.7596), "FAI": (64.8151, -147.8561), "FAR": (46.9207, -96.8158),
    "FAT": (36.7762, -119.7182), "FAY": (34.9912, -78.8803), "FCA": (48.3105, -114.2560),
    "FLG": (35.1385, -111.6709), "FLL": (26.0726, -80.1527), "FLO": (34.1853, -79.7239),
    "FMN": (36.7412, -108.2299), "FNT": (42.9654, -83.7436), "FSD": (43.5820, -96.7419),
    "FSM": (35.3366, -94.3675), "FWA": (40.9785, -85.1951), "GCC": (44.3489, -105.5394),
    "GCK": (37.9275, -100.7237), "GEG": (47.6199, -117.5339), "GFK": (47.9493, -97.1761),
    "GGG": (32.3840, -94.7115), "GJT": (39.1224, -108.5268), "GNV": (29.6900, -82.2717),
    "GPT": (30.4073, -89.0701), "GRB": (44.4851, -88.1296), "GRI": (40.9675, -98.3096),
    "GRK": (31.0672, -97.8289), "GRR": (42.8808, -85.5228), "GSO": (36.0978, -79.9373),
    "GSP": (34.8957, -82.2189), "GTF": (47.4820, -111.3709), "GTR": (33.4503, -88.5914),
    "GUC": (38.5339, -106.9330), "GUM": (13.4834, 144.7960), "HDN": (40.4812, -107.2218),
    "HGR": (39.7079, -77.7295), "HHH": (32.2242, -80.6975), "HIB": (47.3866, -92.8389),
    "HLN": (46.6068, -111.9830), "HNL": (21.3187, -157.9224), "HOB": (32.6875, -103.2173),
    "HOM": (59.6456, -151.4771), "HOU": (29.6454, -95.2789), "HPN": (41.0670, -73.7076),
    "HRL": (26.2285, -97.6544), "HSV": (34.6372, -86.7751), "HYA": (41.6693, -70.2804),
    "HYS": (38.8422, -99.2732), "IAD": (38.9531, -77.4565), "IAH": (29.9902, -95.3368),
    "ICT": (37.6499, -97.4331), "IDA": (43.5146, -112.0707), "ILM": (34.2706, -77.9026),
    "IMT": (45.8178, -88.1143), "INL": (48.5662, -93.4031), "IPL": (32.8342, -115.5785),
    "ISN": (48.1778, -103.6418), "ISP": (40.7952, -73.1002), "ITH": (42.4910, -76.4584),
    "ITO": (19.7214, -155.0485), "JAC": (43.6073, -110.7377), "JAN": (32.3112, -90.0759),
    "JAX": (30.4941, -81.6879), "JFK": (40.6413, -73.7781), "JLN": (37.1517, -94.4983),
    "JMS": (46.9297, -98.6782), "JNU": (58.3550, -134.5763), "KOA": (19.7388, -156.0456),
    "KTN": (55.3557, -131.7137), "LAR": (41.3121, -105.6750), "LAS": (36.0840, -115.1537),
    "LAW": (34.5677, -98.4166), "LAX": (33.9425, -118.4081), "LBB": (33.6636, -101.8229),
    "LBE": (40.2759, -79.4050), "LBF": (41.1262, -100.9606), "LCH": (30.1261, -93.2233),
    "LEX": (38.0365, -84.6060), "LFT": (30.2053, -91.9876), "LGA": (40.7772, -73.8726),
    "LGB": (33.8177, -118.1516), "LIH": (21.9760, -159.3389), "LIT": (34.7294, -92.2243),
    "LNK": (40.8510, -96.7592), "LRD": (27.5438, -99.4616), "LSE": (43.8790, -91.2567),
    "LWB": (37.8583, -80.3994), "LWS": (46.3745, -117.0153), "LYH": (37.3267, -79.2004),
    "MAF": (31.9425, -102.2019), "MBS": (43.5329, -84.0797), "MCI": (39.2976, -94.7139),
    "MCO": (28.4312, -81.3081), "MDT": (40.1935, -76.7634), "MDW": (41.7860, -87.7524),
    "MEI": (32.3326, -88.7519), "MEM": (35.0424, -89.9767), "MFE": (26.1758, -98.2386),
    "MFR": (42.3742, -122.8735), "MGM": (32.3006, -86.3940), "MHK": (39.1410, -96.6708),
    "MHT": (42.9326, -71.4357), "MIA": (25.7959, -80.2870), "MKE": (42.9472, -87.8966),
    "MKG": (43.1695, -86.2382), "MKL": (35.5999, -88.9157), "MLB": (28.1028, -80.6453),
    "MLI": (41.4485, -90.5075), "MLU": (32.5109, -92.0378), "MMH": (37.6241, -118.8377),
    "MOB": (30.6912, -88.2428), "MOD": (37.6258, -120.9544), "MOT": (48.2594, -101.2800),
    "MQT": (46.5354, -87.5954), "MRY": (36.5870, -121.8427), "MSN": (43.1399, -89.3375),
    "MSO": (46.9163, -114.0906), "MSP": (44.8820, -93.2218), "MSY": (29.9934, -90.2580),
    "MTJ": (38.5098, -107.8938), "MYR": (33.6797, -78.9283), "OAJ": (34.8292, -77.6121),
    "OAK": (37.7213, -122.2208), "OGG": (20.8986, -156.4305), "OKC": (35.3931, -97.6007),
    "OMA": (41.3032, -95.8941), "ONT": (34.0560, -117.6012), "ORD": (41.9742, -87.9073),
    "ORF": (36.8976, -76.0132), "ORH": (42.2673, -71.8757), "OTZ": (66.8847, -162.5985),
    "PAH": (37.0603, -88.7738), "PBG": (44.6510, -73.4681), "PBI": (26.6832, -80.0956),
    "PDX": (45.5887, -122.5975), "PHF": (37.1319, -76.4930), "PHL": (39.8721, -75.2411),
    "PHX": (33.4373, -112.0078), "PIA": (40.6642, -89.6933), "PIB": (31.9671, -89.3371),
    "PIH": (42.9098, -112.5960), "PIT": (40.4915, -80.2329), "PKB": (39.3451, -81.4393),
    "PLN": (45.5710, -84.7967), "PMD": (34.6294, -118.0845), "PNS": (30.4734, -87.1866),
    "PPG": (-14.3310, -170.7105), "PQI": (46.6890, -68.0448), "PSC": (46.2647, -119.1190),
    "PSE": (18.0083, -66.5630), "PSG": (56.8009, -132.9453), "PSP": (33.8297, -116.5067),
    "PUB": (38.2890, -104.4969), "PUW": (46.7439, -117.1096), "PVD": (41.7232, -71.4282),
    "PVU": (40.2192, -111.7234), "PWM": (43.6462, -70.3093), "RAP": (44.0453, -103.0574),
    "RDD": (40.5090, -122.2933), "RDM": (44.2541, -121.1500), "RDU": (35.8776, -78.7875),
    "RFD": (42.1954, -89.0972), "RHI": (45.6312, -89.4675), "RIC": (37.5052, -77.3197),
    "RKS": (41.5942, -109.0652), "RNO": (39.4991, -119.7681), "ROA": (37.3255, -79.9754),
    "ROC": (43.1189, -77.6724), "ROW": (33.3016, -104.5306), "RST": (43.9083, -92.5001),
    "RSW": (26.5362, -81.7552), "SAF": (35.6171, -106.0882), "SAN": (32.7336, -117.1897),
    "SAT": (29.5337, -98.4698), "SAV": (32.1276, -81.2021), "SBA": (34.4262, -119.8404),
    "SBN": (41.7087, -86.3173), "SBP": (35.2368, -120.6424), "SCE": (40.8493, -77.8487),
    "SDF": (38.1744, -85.7360), "SEA": (47.4502, -122.3088), "SFO": (37.6213, -122.3790),
    "SGF": (37.2457, -93.3886), "SGU": (37.0363, -113.5103), "SHV": (32.4466, -93.8256),
    "SIT": (57.0471, -135.3615), "SJC": (37.3626, -121.9290), "SJT": (31.3577, -100.4960),
    "SJU": (18.4394, -66.0018), "SLC": (40.7884, -111.9778), "SLN": (38.7910, -97.6522),
    "SMF": (38.6954, -121.5908), "SMX": (34.8994, -120.4575), "SNA": (33.6757, -117.8682),
    "SPI": (39.8441, -89.6779), "SPN": (15.1190, 145.7290), "SRQ": (27.3954, -82.5544),
    "STL": (38.7487, -90.3700), "STT": (18.3373, -64.9733), "STX": (17.7019, -64.7985),
    "SUN": (43.5044, -114.2963), "SUX": (42.4026, -96.3844), "SWF": (41.5041, -74.1048),
    "SYR": (43.1112, -76.1063), "TLH": (30.3965, -84.3503), "TOL": (41.5868, -83.8078),
    "TPA": (27.9755, -82.5332), "TRI": (36.4752, -82.4074), "TTN": (40.2767, -74.8135),
    "TUL": (36.1984, -95.8881), "TUP": (34.2681, -88.7699), "TUS": (32.1161, -110.9410),
    "TVC": (44.7414, -85.5822), "TWF": (42.4818, -114.4877), "TXK": (33.4539, -93.9910),
    "TYR": (32.3541, -95.4024), "TYS": (35.8110, -83.9940), "UCA": (43.1451, -75.3839),
    "VEL": (40.4409, -109.5100), "VLD": (30.7825, -83.2767), "VPS": (30.4832, -86.5254),
    "WRG": (56.4843, -132.3699), "XNA": (36.2819, -94.3068), "YAK": (59.5033, -139.6603),
    "YKM": (46.5682, -120.5441), "YUM": (32.6566, -114.6060),
}


def _haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles between two (lat, lon) points."""
    R = 3958.8  # Earth radius in miles
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(dλ / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def _load_airport_coords(
    extra_csv: str | None,
    known_airports: set[str],
) -> dict[str, tuple[float, float]]:
    """
    Return {iata: (lat, lon)} for all known_airports.
    Merges built-in table with an optional user-supplied CSV
    (columns: airport, lat, lon  OR  iata, lat, lon).
    """
    coords = dict(_US_AIRPORT_COORDS)

    if extra_csv:
        import csv as _csv
        with open(extra_csv, encoding="utf-8") as fh:
            reader = _csv.DictReader(fh)
            for row in reader:
                code = (row.get("airport") or row.get("iata") or "").strip().upper()
                try:
                    lat = float(row["lat"])
                    lon = float(row["lon"])
                except (KeyError, ValueError):
                    continue
                if code:
                    coords[code] = (lat, lon)

    missing = known_airports - coords.keys()
    if missing:
        print(f"  WARNING: no coordinates for {len(missing)} airports "
              f"(will be treated as unreachable from any base): "
              f"{sorted(missing)[:10]}{'...' if len(missing) > 10 else ''}")
    return coords


def _kmedian_bases(
    airports: list[str],
    coords: dict[str, tuple[float, float]],
    dep_count: dict[str, int],
    k: int,
    seed: int = RANDOM_SEED,
    n_restarts: int = 8,
) -> list[str]:
    """
    Weighted k-median on airport coordinates.

    Objective: minimise  Σ_{airport a}  dep_count[a] × dist(a, nearest_base)
    i.e. minimise total crew-deadhead demand weighted by departure frequency.

    Uses k-means++ initialisation + Lloyd-style iteration (swap-based), which
    is fast enough for |airports| ~ 500 and k ~ 20.

    Returns sorted list of k base airport codes.
    """
    rng = random.Random(seed)

    # Only optimise over airports that have coordinates
    cand = [ap for ap in airports if ap in coords]
    weights = {ap: dep_count.get(ap, 1) for ap in cand}

    def dist(a: str, b: str) -> float:
        la, lo = coords[a]
        lb, ob = coords[b]
        return _haversine_mi(la, lo, lb, ob)

    def total_cost(bases: list[str]) -> float:
        base_set = set(bases)
        total = 0.0
        for ap in cand:
            if ap in base_set:
                continue
            d = min(dist(ap, b) for b in bases)
            total += weights[ap] * d
        return total

    def one_run(init_bases: list[str]) -> list[str]:
        bases = list(init_bases)
        improved = True
        while improved:
            improved = False
            # Assign each airport to nearest base
            clusters: dict[str, list[str]] = {b: [] for b in bases}
            for ap in cand:
                nearest = min(bases, key=lambda b: dist(ap, b))
                clusters[nearest].append(ap)

            # For each cluster, find the airport that minimises weighted distance to cluster
            new_bases = []
            for b, members in clusters.items():
                if not members:
                    new_bases.append(b)
                    continue
                best = min(members, key=lambda c: sum(
                    weights[m] * dist(c, m) for m in members))
                new_bases.append(best)
                if best != b:
                    improved = True
            bases = new_bases
        return bases

    # k-means++ initialisation
    def kmeanspp_init() -> list[str]:
        chosen = [rng.choice(cand)]
        while len(chosen) < k:
            dists = [min(dist(ap, c) ** 2 for c in chosen) for ap in cand]
            total_d = sum(dists)
            r = rng.random() * total_d
            cumulative = 0.0
            for ap, d in zip(cand, dists):
                cumulative += d
                if cumulative >= r:
                    chosen.append(ap)
                    break
            else:
                chosen.append(rng.choice(cand))
        return chosen

    best_bases = None
    best_cost = float('inf')
    for _ in range(n_restarts):
        init = kmeanspp_init()
        result = one_run(init)
        c = total_cost(result)
        if c < best_cost:
            best_cost = c
            best_bases = result

    return sorted(best_bases)


# ─────────────────────────────────────────────
# CREW BASE ASSIGNMENT
# ─────────────────────────────────────────────

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
    Select crew bases and assign crew counts using one of three strategies:

    'volume'
        Legacy: top-N airports by departure count. Simple but leaves most of the
        country uncovered when N is small.

    'kmedian'
        Option 2: choose N bases that minimise total weighted deadhead distance
        from every flight origin to its nearest base (weighted k-median).
        Requires airport lat/lon — uses built-in US table or AIRPORT_COORDS_CSV.

    'kmedian+satellite'
        Option 2 + 3: k-median hubs as above, PLUS satellite pre-positioning.
        For each non-base airport within SATELLITE_RADIUS_MI of a hub AND with
        at least SATELLITE_MIN_FLIGHTS daily departures, a small crew contingent
        is pre-positioned there at t=0.  They start the horizon at the satellite,
        fly/work local turns, and must return to their hub by horizon_end.
        This covers the regional long tail without adding full base overhead.
    """
    rng = random.Random(seed)
    all_airports = sorted(set(f.origin for f in flights) | set(f.dest for f in flights))

    demand_minutes: dict[str, float] = defaultdict(float)
    dep_count: dict[str, int] = defaultdict(int)
    for f in flights:
        demand_minutes[f.origin] += f.min_crew * f.duration
        dep_count[f.origin] += 1

    # ── Select hub bases ──────────────────────────────────────────────────────
    use_kmedian = strategy in ('kmedian', 'kmedian+satellite')

    if max_bases is not None and len(all_airports) > max_bases and use_kmedian:
        print(f"  Running weighted k-median base selection "
              f"(k={max_bases}, strategy='{strategy}')...")
        coords = _load_airport_coords(airport_coords_csv, set(all_airports))
        hub_airports = _kmedian_bases(all_airports, coords, dep_count,
                                      k=max_bases, seed=seed)
        # Report coverage quality
        cand_with_coords = [ap for ap in all_airports if ap in coords]
        max_deadhead = max(
            min(_haversine_mi(*coords[ap], *coords[b])
                for b in hub_airports if b in coords)
            for ap in cand_with_coords if ap not in hub_airports
        ) if cand_with_coords else 0.0
        avg_deadhead = (
            sum(min(_haversine_mi(*coords[ap], *coords[b])
                    for b in hub_airports if b in coords)
                for ap in cand_with_coords if ap not in hub_airports)
            / max(1, len(cand_with_coords) - len(hub_airports))
        ) if cand_with_coords else 0.0
        print(f"  Hub bases selected: {hub_airports}")
        print(f"  Max deadhead to nearest hub: {max_deadhead:.0f} mi  "
              f"|  avg: {avg_deadhead:.0f} mi")

    elif max_bases is not None and len(all_airports) > max_bases:
        # Legacy volume selection
        hub_airports = sorted(
            all_airports,
            key=lambda ap: dep_count.get(ap, 0),
            reverse=True
        )[:max_bases]
        hub_airports = sorted(hub_airports)
        print(f"  Selected {len(hub_airports)} bases from {len(all_airports)} airports "
              f"(top by departure volume)")
        coords = None

    else:
        hub_airports = all_airports
        coords = None

    # ── Satellite pre-positioning (Option 3) ─────────────────────────────────
    satellite_airports: dict[str, str] = {}   # satellite_ap -> nearest_hub

    if strategy == 'kmedian+satellite' and max_bases is not None:
        if coords is None:
            coords = _load_airport_coords(airport_coords_csv, set(all_airports))
        hub_set = set(hub_airports)
        days_in_window = max(f.arr_min for f in flights) / 1440 if flights else 3

        for ap in all_airports:
            if ap in hub_set or ap not in coords:
                continue
            daily_deps = dep_count.get(ap, 0) / max(1, days_in_window)
            if daily_deps < satellite_min_flights:
                continue
            nearest_hub = min(
                (h for h in hub_airports if h in coords),
                key=lambda h: _haversine_mi(*coords[ap], *coords[h]),
                default=None,
            )
            if nearest_hub is None:
                continue
            dist_to_hub = _haversine_mi(*coords[ap], *coords[nearest_hub])
            if dist_to_hub <= satellite_radius_mi:
                satellite_airports[ap] = nearest_hub

        print(f"  Satellite airports pre-positioned: {len(satellite_airports)} "
              f"(within {satellite_radius_mi:.0f} mi of a hub, "
              f"≥{satellite_min_flights} daily deps)")
        if satellite_airports:
            # Show a few examples
            examples = sorted(satellite_airports.items(),
                               key=lambda kv: dep_count.get(kv[0], 0), reverse=True)[:5]
            for sat, hub in examples:
                d = _haversine_mi(*coords[sat], *coords[hub])
                print(f"    {sat} → hub {hub}  ({d:.0f} mi, "
                      f"{dep_count.get(sat,0)} deps)")

    # ── Crew sizing ───────────────────────────────────────────────────────────
    horizon_days = max(f.arr_min for f in flights) / 1440 if flights else 3
    duty_minutes_per_crew = 480 * horizon_days

    all_bases = sorted(set(hub_airports) | set(satellite_airports.keys()))
    base_counts: dict[str, int] = {}

    for ap in all_bases:
        demand = demand_minutes.get(ap, 0)
        if ap in satellite_airports:
            # Satellites: size only for their local demand (no travel buffer)
            needed = math.ceil((demand / duty_minutes_per_crew) * 1.2) if demand > 0 else 2
            base_counts[ap] = max(2, needed)
        else:
            # Hubs: size for own demand + coverage buffer for surrounding region
            needed = math.ceil((demand / duty_minutes_per_crew) * 1.5) if demand > 0 else MIN_CREW_PER_BASE
            noisy = int(rng.gauss(needed, max(1, needed * 0.10)))
            base_counts[ap] = max(MIN_CREW_PER_BASE, noisy)

    crew_list: list[CrewMember] = []
    cid = 0
    for ap in sorted(all_bases):
        for _ in range(base_counts[ap]):
            crew_list.append(CrewMember(id=cid, base=ap))
            cid += 1

    total = len(crew_list)
    n_hub_crew = sum(base_counts[ap] for ap in hub_airports)
    n_sat_crew = sum(base_counts[ap] for ap in satellite_airports)
    total_demand = sum(demand_minutes.values())
    total_available = sum(base_counts[ap] * duty_minutes_per_crew for ap in all_bases)

    print(f"Created {total:,} crew  "
          f"({n_hub_crew} at {len(hub_airports)} hubs  +  "
          f"{n_sat_crew} at {len(satellite_airports)} satellites)")
    print(f"  Total crew-minutes needed:    {total_demand:,.0f}")
    print(f"  Available crew-minutes:       {total_available:,.0f}")
    print(f"  Coverage ratio:               {total_available / total_demand:.2f}x")

    # Warn about airports with zero base coverage within reach
    if coords and hub_airports:
        unreachable = [
            ap for ap in all_airports
            if ap not in set(all_bases)
            and ap in coords
            and min(_haversine_mi(*coords[ap], *coords[h])
                    for h in hub_airports if h in coords) > satellite_radius_mi * 1.5
        ]
        if unreachable:
            print(f"  WARNING: {len(unreachable)} airports are >{satellite_radius_mi*1.5:.0f} mi "
                  f"from any base (likely uncoverable): "
                  f"{sorted(unreachable, key=lambda a: dep_count.get(a,0), reverse=True)[:8]}")

    return crew_list


# ─────────────────────────────────────────────
# COST HELPERS
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

def _node_time_key(n: Node) -> int:
    return n.time

def _sorted_node_list():
    """Module-level factory for SortedList — required for pickle compatibility."""
    return SortedList(key=_node_time_key)

# Module-level globals populated by the process-pool initializer.
_WORKER_FWD_GRAPH: dict = {}
_WORKER_BWD_GRAPH: dict = {}
_WORKER_HORIZON:   int  = 0


def _worker_init(fwd_graph: dict, bwd_graph: dict, horizon: int) -> None:
    """Initializer for ProcessPoolExecutor workers: store shared graph once."""
    global _WORKER_FWD_GRAPH, _WORKER_BWD_GRAPH, _WORKER_HORIZON
    _WORKER_FWD_GRAPH = fwd_graph
    _WORKER_BWD_GRAPH = bwd_graph
    _WORKER_HORIZON   = horizon


def _dijkstra_worker(args: tuple) -> tuple[set[int], set[int]]:
    """
    Process-pool worker: forward + backward Dijkstra for one base.

    args: (fwd_start_ids, bwd_start_id)
      fwd_start_ids : list of node ids where crew may start (home or displaced)
      bwd_start_id  : single horizon node id for backward pass
    Returns (fwd_reachable_arc_ids, bwd_reachable_arc_ids).
    """
    import heapq
    fwd_starts, bwd_start = args
    graph   = _WORKER_FWD_GRAPH
    bgraph  = _WORKER_BWD_GRAPH
    horizon = _WORKER_HORIZON

    def _run_fwd(start_ids: list, g: dict) -> set[int]:
        # Multi-source forward Dijkstra: crew may start from any position node.
        earliest: dict[int, int] = {sid: 0 for sid in start_ids}
        heap = [(0, sid) for sid in start_ids]
        import heapq as _hq
        _hq.heapify(heap)
        reached: set[int] = set()
        while heap:
            t, nid = _hq.heappop(heap)
            if t > earliest.get(nid, horizon + 1):
                continue
            for eid, arr, arc_id, arc_start_t in g.get(nid, []):
                if arr > horizon:
                    continue
                if t <= arc_start_t:
                    reached.add(arc_id)
                if arr < earliest.get(eid, horizon + 1):
                    earliest[eid] = arr
                    _hq.heappush(heap, (arr, eid))
        return reached

    def _run_bwd(start_id: int, g: dict) -> set[int]:
        earliest: dict[int, int] = {start_id: 0}
        heap = [(0, start_id)]
        reached: set[int] = set()
        while heap:
            t, nid = heapq.heappop(heap)
            if t > earliest.get(nid, horizon + 1):
                continue
            for eid, arr, arc_id, arc_start_t in g.get(nid, []):
                if arr > horizon:
                    continue
                if t <= arc_start_t:
                    reached.add(arc_id)
                if arr < earliest.get(eid, horizon + 1):
                    earliest[eid] = arr
                    heapq.heappush(heap, (arr, eid))
        return reached

    return _run_fwd(fwd_starts, graph), _run_bwd(bwd_start, bgraph)


class CrewFlowNetwork:

    """
    Time-expanded network for cabin crew pairing via integer flow.

    The network uses exact flight departure/arrival times as node timestamps,
    so no iterative time refinement (DDD) is needed — the network is already
    fully discretised at build time.

    Variables (per arc, not per crew member):
      f_work[arc] ∈ ℤ≥0   number of working crew on this arc
      f_dh[arc]   ∈ ℤ≥0   number of deadheading crew on this arc
      f_wait[arc] ∈ ℤ≥0   number of waiting crew on this arc
      slack[flt]  ∈ ℤ≥0   uncovered crew slots (penalised)

    Flow balance per base b at each node n:
      ∑_{a out of n} flow[a]  ==  ∑_{a into n} flow[a]
      ... with supply = crew_count[b] injected at (b, t=0)
              demand = crew_count[b] absorbed at (b, t=horizon)

    Coverage:
      f_work[flight_arc] + slack[flt] >= flt.min_crew

    After solving, Stage 2 flow decomposition assigns named crew IDs.

    Rolling horizon usage:
      Pass window_offset > 0 to shift all node times by that many minutes.
      Pass base_supply_override to replace default crew counts (used after
      the first window to reflect actual crew positions at the cut point).
    """

    def __init__(
        self,
        flights: list[Flight],
        crew: list[CrewMember],
        horizon_end: int,
        flight_end: int | None = None,
        time_bucket: int = TIME_BUCKET,
        verbose: bool = True,
        window_offset: int = 0,
        base_supply_override: dict[str, int] | None = None,
        position_supply: dict[str, dict[str, int]] | None = None,
    ):
        """
        Parameters
        ----------
        position_supply : {home_base: {airport: count}}
            If provided (rolling horizon windows 2+), crew from each home_base
            start distributed across airports as given, rather than all at home.
            Supersedes base_supply_override.
        base_supply_override : {home_base: total_count}   (legacy, kept for compat)
        """
        self.flights = flights
        self.flights_by_id = {f.id: f for f in flights}
        self.crew = crew
        self.crew_by_id = {c.id: c for c in crew}

        self.base_crew: dict[str, int] = defaultdict(int)
        for c in crew:
            self.base_crew[c.base] += 1

        # Rolling horizon: override supply with actual end-of-step positions.
        # position_supply takes precedence; base_supply_override is a fallback.
        if base_supply_override and not position_supply:
            for base, count in base_supply_override.items():
                self.base_crew[base] = count

        # {home_base: {airport: count}} — where crew actually are at window start.
        # None means "all crew are at home base" (first window).
        self.position_supply: dict[str, dict[str, int]] | None = position_supply

        self.airports = sorted(self.base_crew.keys())

        self.horizon_end    = horizon_end
        self.flight_end     = flight_end if flight_end is not None else horizon_end
        self.time_bucket    = time_bucket
        self.verbose        = verbose
        self.window_offset  = window_offset   # minutes offset for this window
        # True only for the final rolling-horizon window (or monolithic solve).
        # Intermediate windows use free-exit flow balance so crew aren't forced
        # back to their home base at the commit cut-point.
        self.is_last_window: bool = True

        # Network topology
        self.nodes:             set[Node]                       = set()
        self.nodes_by_airport:  dict[str, SortedList]           = defaultdict(
            _sorted_node_list)
        self.arcs:              set[Arc]                        = set()
        self._arc_counter       = 0
        self._arc_key_set:      set[tuple]                      = set()
        self.arcs_from:         dict[Node, list[Arc]]           = defaultdict(list)
        self.arcs_to:           dict[Node, list[Arc]]           = defaultdict(list)
        self.wait_arc_by_start: dict[Node, Arc]                 = {}
        self._arcs_by_flight:   dict[int, list[Arc]]            = defaultdict(list)
        self.min_duty_at:         dict[Node, int]                 = {}
        self.duty_period_start_at: dict[Node, int]                 = {}

        # Gurobi model
        self.model: gp.Model | None = None

        # Flow variables: (base, arc) -> Var
        self.var_work:      dict[tuple[str, Arc], gp.Var] = {}
        self.var_dh:        dict[tuple[str, Arc], gp.Var] = {}
        self.var_wait:      dict[tuple[str, Arc], gp.Var] = {}
        self.var_home_rest: dict[tuple[str, Arc], gp.Var] = {}  # home-break incentive
        self.slack_var:     dict[int, gp.Var] = {}

        # Constraints
        self.flow_constrs:     dict[str, dict[Node, gp.Constr]] = {}
        self.coverage_constrs: dict[int, gp.Constr] = {}

    # ── Pickle compatibility ──────────────────
    CACHE_VERSION = 2   # bumped: home wait arcs now free + home-break state fix
    _ATTR_DEFAULTS: dict = {'min_duty_at': {}, '_arcs_by_flight': {},
                            '_arc_key_set': set(), 'duty_period_start_at': {}}

    def __getstate__(self):
        state = self.__dict__.copy()
        state['_cache_version'] = self.CACHE_VERSION
        for key in ('model', 'var_work', 'var_dh', 'var_wait', 'var_home_rest',
                    'slack_var', 'flow_constrs', 'coverage_constrs'):
            state.pop(key, None)
        return state

    def __setstate__(self, state):
        import copy
        for key in ('var_work', 'var_dh', 'var_wait', 'var_home_rest',
                    'slack_var', 'flow_constrs', 'coverage_constrs'):
            state.setdefault(key, {})
        state.setdefault('model', None)
        state.pop('_cache_version', 0)
        for attr, default in self._ATTR_DEFAULTS.items():
            if attr not in state:
                state[attr] = copy.deepcopy(default)
        self.__dict__.update(state)

        # Rebuild nodes_by_airport as SortedList (handles old plain-list pickles)
        nba = self.nodes_by_airport
        if nba and not isinstance(next(iter(nba.values()), None), SortedList):
            new_nba = defaultdict(_sorted_node_list)
            for ap, nodes in nba.items():
                for n in nodes:
                    new_nba[ap].add(n)
            self.nodes_by_airport = new_nba

        if not self._arc_key_set and self.arcs:
            self._arc_key_set = {
                (a.start, a.end, a.arc_type, a.flight_id) for a in self.arcs
            }

    def _ensure_attrs(self):
        import copy
        for attr, default in self._ATTR_DEFAULTS.items():
            if not hasattr(self, attr):
                setattr(self, attr, copy.deepcopy(default))

    # ── Node helpers ─────────────────────────

    def _find_node(self, airport: str, time: int, round_down=True) -> Node | None:
        nodes = self.nodes_by_airport[airport]
        if not nodes:
            return None
        if round_down:
            idx = nodes.bisect_key_right(time) - 1
            return nodes[idx] if idx >= 0 else None
        else:
            idx = nodes.bisect_key_left(time)
            return nodes[idx] if idx < len(nodes) else None

    def _find_node_at_or_after(self, airport: str, time: int) -> Node | None:
        nodes = self.nodes_by_airport[airport]
        if not nodes:
            return None
        idx = nodes.bisect_key_left(time)
        return nodes[idx] if idx < len(nodes) else None

    # ── Arc creation ─────────────────────────

    def _make_arc(self, start: Node, end: Node, true_end: int,
                  cost: float, arc_type: str, flight_id: int | None = None) -> Arc:
        key = (start, end, arc_type, flight_id)
        if key in self._arc_key_set:
            for existing in self.arcs_from.get(start, []):
                if (existing.end == end and existing.arc_type == arc_type
                        and existing.flight_id == flight_id):
                    return existing

        self._arc_key_set.add(key)

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

        return arc

    def _create_wait_arc(self, from_node: Node, to_node: Node) -> Arc:
        wait_minutes = to_node.time - from_node.time
        # Home base waits are free — crew are supposed to be there and there
        # is no hotel or per-diem cost.  Away layovers still carry the normal
        # time + overnight penalty so the solver tries to minimise them.
        if from_node.airport in self.airports:   # home base
            cost = 0.0
        else:
            overnight = 1 if wait_minutes >= OVERNIGHT_THRESHOLD else 0
            cost = wait_minutes * COST_LAYOVER_MIN + overnight * COST_OVERNIGHT
        arc = self._make_arc(from_node, to_node, to_node.time, cost, 'wait')
        self.wait_arc_by_start[from_node] = arc
        return arc

    # ── Reachability ──────────────────────────

    def compute_reachable_arcs(self) -> dict[str, set[int]]:
        """
        For each base, find arcs reachable via forward Dijkstra from depot
        AND backward Dijkstra from horizon (intersection = truly usable arcs).
        Uses a process pool with graph sent once per worker via initializer.
        """
        import time as _t
        import os
        from concurrent.futures import ProcessPoolExecutor

        t0 = _t.time()
        print("  Computing reachability per base (parallel Dijkstra)...",
              end="", flush=True)

        node_to_id: dict[Node, int] = {n: i for i, n in enumerate(self.nodes)}
        horizon = self.horizon_end

        fwd_graph: dict[int, list] = defaultdict(list)
        bwd_graph: dict[int, list] = defaultdict(list)
        for arc in self.arcs:
            s = node_to_id[arc.start]
            e = node_to_id[arc.end]
            fwd_graph[s].append((e, arc.true_end,    arc.id, arc.start.time))
            bwd_graph[e].append((s,
                                  horizon - arc.start.time,
                                  arc.id,
                                  horizon - arc.true_end))

        fwd_plain = dict(fwd_graph)
        bwd_plain = dict(bwd_graph)

        task_args:  list[tuple[list[int], int]] = []
        base_order: list[str] = []
        for base in self.airports:
            horizon_id = node_to_id.get(Node(airport=base, time=horizon))
            if horizon_id is None:
                continue

            # Forward sources: wherever crew from this base actually start.
            if self.position_supply and base in self.position_supply:
                fwd_ids = [
                    node_to_id[Node(airport=ap, time=DEPOT_TIME_START)]
                    for ap, count in self.position_supply[base].items()
                    if count > 0
                    and Node(airport=ap, time=DEPOT_TIME_START) in node_to_id
                ]
            else:
                fwd_ids = []

            # Always include the home depot as a fallback source.
            home_depot_id = node_to_id.get(Node(airport=base, time=DEPOT_TIME_START))
            if home_depot_id is not None and home_depot_id not in fwd_ids:
                fwd_ids.append(home_depot_id)

            if not fwd_ids:
                continue

            task_args.append((fwd_ids, horizon_id))
            base_order.append(base)

        n_workers = min(len(base_order), os.cpu_count() or 4)
        with ProcessPoolExecutor(
            max_workers=n_workers,
            initializer=_worker_init,
            initargs=(fwd_plain, bwd_plain, horizon),
        ) as pool:
            pair_results = list(pool.map(_dijkstra_worker, task_args))

        reachable: dict[str, set[int]] = {}
        for base, (fwd, bwd) in zip(base_order, pair_results):
            reachable[base] = fwd & bwd

        for base in self.airports:
            reachable.setdefault(base, set())

        total_pairs = sum(len(v) for v in reachable.values())
        full_pairs  = len(self.airports) * len(self.arcs)
        pct = 100.0 * total_pairs / full_pairs if full_pairs else 0.0
        print(f" done ({_t.time()-t0:.1f}s)")
        print(f"  Reachable (base,arc) pairs: {total_pairs:,} / {full_pairs:,} "
              f"= {pct:.1f}% of full cross-product")
        return reachable

    def _build_flight_index(self):
        self._flights_from: dict[str, list[Flight]] = defaultdict(list)
        self._flights_to:   dict[str, list[Flight]] = defaultdict(list)
        for f in self.flights:
            self._flights_from[f.origin].append(f)
            self._flights_to[f.dest].append(f)

    # ── Network build ─────────────────────────

    def build_initial_network(self):
        """
        Build the time-expanded flow network.

        Hard constraints enforced by arc pruning — arcs that would violate
        a rule do not exist, so the MIP physically cannot route crew through them.

        MAX_DUTY_MINUTES  – cumulative flying minutes since last MIN_REST rest.
                            Resets at any airport after >= MIN_REST wait.
                            Tracked globally per-node (single counter).

        MAX_WORK_DAY      – distinct calendar days with >= 1 flight since the
                            last home-base break.
                            Resets ONLY after >= HOME_BREAK minutes at home base.
                            Tracked per-base (each base has independent clocks).

        MAX_AWAY_DAYS     – calendar days elapsed since last home-base break.
                            Same reset rule as MAX_WORK_DAY.
                            Tracked per-base.

        Per-base home-break state at each node is stored as:
            (reset_at: int, worked: int, last_day: int)
        where reset_at is the minute the clocks were last zeroed, worked is the
        number of distinct flight-days since then, and last_day is the last
        calendar day index counted (to deduplicate same-day flights).
        We keep the most favourable (lowest worked, then latest reset_at) state
        reaching each node so a node is only fully pruned when ALL paths into
        it are exhausted.

        Results stored in self._arc_allowed_bases: {arc_id: set[base_str]}
        so build_model can create variables only for (base, arc) pairs where
        the base is in the allowed set.
        """
        import time as _t
        self._build_flight_index()
        print("Building network...")
        t0 = _t.time()

        # 1. Depot + horizon nodes for each base.
        for ap in self.airports:
            for t in (DEPOT_TIME_START, self.horizon_end):
                node = Node(airport=ap, time=t)
                self.nodes.add(node)
                self.nodes_by_airport[ap].add(node)

        if self.position_supply:
            for base, airport_counts in self.position_supply.items():
                for ap, count in airport_counts.items():
                    if count > 0 and ap not in self.airports:
                        node = Node(airport=ap, time=DEPOT_TIME_START)
                        self.nodes.add(node)
                        self.nodes_by_airport[ap].add(node)

        print(f"  Depot/horizon nodes: {len(self.nodes)}  ({_t.time()-t0:.1f}s)")

        # 2. One node per exact flight dep/arr time.
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
                self.nodes_by_airport[ap].add(node)
        print(f"  All nodes added: {len(self.nodes)}  ({_t.time()-t0:.1f}s)")

        # 3. Wait-arc chains (one per consecutive node pair per airport).
        for ap in self.airports:
            nodes_ap = self.nodes_by_airport[ap]
            for i in range(len(nodes_ap) - 1):
                self._create_wait_arc(nodes_ap[i], nodes_ap[i + 1])
        print(f"  Wait arcs built  ({_t.time()-t0:.1f}s)")

        # 4. Initialise duty state and home-break state at each depot.
        for ap in self.airports:
            depot = Node(airport=ap, time=DEPOT_TIME_START)
            self.min_duty_at[depot]          = 0
            self.duty_period_start_at[depot] = DEPOT_TIME_START

        # _hb[base][node] = (reset_at, worked, last_day, home_wait_acc)
        # reset_at      : minute clocks were last zeroed (0 = start of planning)
        # worked        : distinct calendar days with >=1 flight since reset_at
        # last_day      : last calendar day index counted (dedup)
        # home_wait_acc : minutes continuously accumulated at home base since the
        #                 last departure.  Once >= HOME_BREAK the clocks reset.
        #                 Initialised to HOME_BREAK at the depot so crew that
        #                 start at home are treated as already rested.
        _hb: dict[str, dict[Node, tuple[int,int,int,int]]] = {
            base: {} for base in self.airports
        }
        for base in self.airports:
            depot = Node(airport=base, time=DEPOT_TIME_START)
            _hb[base][depot] = (DEPOT_TIME_START, 0, -1, HOME_BREAK)

        # Rolling-horizon windows 2+: carry forward clock state from prior window.
        # Caller sets self._hb_carry_state = {base: (reset_at, worked, last_day)}.
        hb_carry: dict[str, tuple[int,int,int]] = getattr(
            self, '_hb_carry_state', {}
        )
        if hb_carry and self.position_supply:
            for base, airport_counts in self.position_supply.items():
                carry = hb_carry.get(base, (DEPOT_TIME_START, 0, -1, HOME_BREAK))
                # Tolerate 3-tuples from an older carry state (add home_wait_acc=0).
                if len(carry) == 3:
                    carry = carry + (0,)
                for ap, count in airport_counts.items():
                    if count <= 0:
                        continue
                    start_node = Node(airport=ap, time=DEPOT_TIME_START)
                    if start_node not in _hb.get(base, {}):
                        _hb.setdefault(base, {})[start_node] = carry

        # 5. Forward pass.
        flights_departing: dict[tuple[str, int], list] = defaultdict(list)
        for f in self.flights:
            flights_departing[(f.origin, f.dep_min)].append(f)

        # {arc_id: set[base]} — which bases may traverse each arc
        self._arc_allowed_bases: dict[int, set[str]] = {}

        n_arcs        = 0
        n_missing     = 0
        n_pruned_duty = 0
        n_pruned_hb   = 0

        for node in sorted(self.nodes, key=lambda n: (n.time, n.airport)):
            duty_here = self.min_duty_at.get(node)
            if duty_here is None:
                continue
            period_start = self.duty_period_start_at.get(node, node.time)

            # ── Propagate duty through outgoing wait arc ──────────────────────
            wait_arc = self.wait_arc_by_start.get(node)
            if wait_arc:
                next_node = wait_arc.end
                wait_dur  = next_node.time - node.time
                if wait_dur >= MIN_REST:
                    new_duty         = 0
                    new_period_start = next_node.time
                else:
                    new_duty         = duty_here
                    new_period_start = period_start
                existing = self.min_duty_at.get(next_node, MAX_DUTY_MINUTES + 1)
                if new_duty < existing:
                    self.min_duty_at[next_node]          = new_duty
                    self.duty_period_start_at[next_node] = new_period_start

            # ── Propagate home-break state through outgoing wait arc ──────────
            for base in self.airports:
                hb_here = _hb[base].get(node)
                if hb_here is None:
                    continue
                reset_at, worked, last_day, home_wait_acc = hb_here
                if wait_arc:
                    next_node = wait_arc.end
                    wait_dur  = next_node.time - node.time
                    if node.airport == base:
                        # Accumulate home wait across consecutive short arcs.
                        # Once the running total crosses HOME_BREAK the clocks
                        # reset — the exact threshold is reached somewhere inside
                        # this arc, but we credit the reset at next_node.time
                        # (the next departure opportunity) which is conservative.
                        new_home_wait = home_wait_acc + wait_dur
                        if new_home_wait >= HOME_BREAK and home_wait_acc < HOME_BREAK:
                            # Just crossed the rest threshold — reset both clocks.
                            new_reset  = next_node.time
                            new_worked = 0
                            new_last   = -1
                        else:
                            # Still accumulating (or already rested) — no change.
                            new_reset  = reset_at
                            new_worked = worked
                            new_last   = last_day
                    else:
                        # Waiting away from home: accumulator does not grow.
                        new_home_wait = 0
                        new_reset  = reset_at
                        new_worked = worked
                        new_last   = last_day
                    existing_next = _hb[base].get(next_node)
                    if (existing_next is None
                            or new_worked < existing_next[1]
                            or (new_worked == existing_next[1]
                                and new_reset > existing_next[0])
                            or (new_worked == existing_next[1]
                                and new_reset == existing_next[0]
                                and new_home_wait > existing_next[3])):
                        _hb[base][next_node] = (new_reset, new_worked,
                                                new_last, new_home_wait)

            # ── Build flight arcs from this node ──────────────────────────────
            for f in flights_departing.get((node.airport, node.time), []):
                arr_node = self._find_node_at_or_after(f.dest, f.arr_min)
                if arr_node is None:
                    n_missing += 1
                    continue

                # Duty check (global)
                duty_after = duty_here + f.duration
                if duty_after > MAX_DUTY_MINUTES:
                    n_pruned_duty += 1
                    continue
                if f.arr_min - period_start > MAX_DUTY_DAYS * 1440:
                    n_pruned_duty += 1
                    continue

                # Home-break check (per base)
                # A base is allowed to use this flight arc if its crew at this
                # node still have budget for another working day and are not
                # yet past MAX_AWAY_DAYS since their last home break.
                #
                # EXCEPTION: flying back to the home base is always allowed,
                # regardless of clock state — that is the only way to reset the
                # clocks, so pruning those arcs would make the problem infeasible.
                allowed_bases: set[str] = set()
                for base in self.airports:
                    hb_here = _hb[base].get(node)
                    if hb_here is None:
                        continue
                    reset_at, worked, last_day, home_wait_acc = hb_here
                    dep_day   = f.dep_min // 1440
                    # Does this flight add a new working day?
                    new_worked = worked + (0 if dep_day == last_day else 1)
                    away_days  = (f.dep_min - reset_at) / 1440.0
                    # Always allow a direct return to home base.
                    if f.dest == base:
                        allowed_bases.add(base)
                    # FIX (Bug 2): if departing FROM home base, the clocks are
                    # only considered reset if the crew has actually accumulated
                    # >= HOME_BREAK minutes at home since arriving.  Without this
                    # guard the pruning would allow a crew to depart home after
                    # 30 minutes and count it as a full home break.
                    elif node.airport == base and home_wait_acc < HOME_BREAK:
                        # Not enough home rest yet — cannot depart on a non-home
                        # flight without violating the break rule.  (Returning to
                        # home on f.dest==base is still allowed above.)
                        pass
                    elif new_worked <= MAX_WORK_DAY and away_days <= MAX_AWAY_DAYS:
                        allowed_bases.add(base)

                if not allowed_bases:
                    n_pruned_hb += 1
                    continue

                arc = self._make_arc(node, arr_node, f.arr_min,
                                     f.duration * COST_FLIGHT_HOUR, 'flight', f.id)
                n_arcs += 1

                existing_allowed = self._arc_allowed_bases.get(arc.id)
                if existing_allowed is None:
                    self._arc_allowed_bases[arc.id] = set(allowed_bases)
                else:
                    existing_allowed |= allowed_bases

                # Propagate duty to arrival node
                existing_duty = self.min_duty_at.get(arr_node, MAX_DUTY_MINUTES + 1)
                if duty_after < existing_duty:
                    self.min_duty_at[arr_node]          = duty_after
                    self.duty_period_start_at[arr_node] = period_start

                # Propagate home-break state to arrival node (per allowed base)
                for base in allowed_bases:
                    hb_here  = _hb[base][node]
                    reset_at, worked, last_day, home_wait_acc_node = hb_here
                    dep_day    = f.dep_min // 1440
                    new_worked = worked + (0 if dep_day == last_day else 1)
                    new_last   = dep_day
                    # Taking any flight resets the home-wait accumulator to 0.
                    new_home_wait = 0
                    # FIX (Bug 3): only reset the away/worked clocks when the crew
                    # departs from their home base AND they have already accumulated
                    # >= HOME_BREAK minutes there.  Previously reset_at was carried
                    # forward unconditionally from the node state, meaning a crew
                    # that returned home and left 30 minutes later could still have
                    # reset_at updated as if they had taken a valid break.
                    if node.airport == base and home_wait_acc_node >= HOME_BREAK:
                        # Valid home break completed — clocks restart from departure.
                        new_reset_at = f.dep_min
                        new_worked   = 1 if dep_day != -1 else 0
                        new_last     = dep_day
                    else:
                        new_reset_at = reset_at
                    existing_arr = _hb[base].get(arr_node)
                    if (existing_arr is None
                            or new_worked < existing_arr[1]
                            or (new_worked == existing_arr[1]
                                and new_reset_at > existing_arr[0])):
                        _hb[base][arr_node] = (new_reset_at, new_worked,
                                               new_last, new_home_wait)

        if n_missing:
            print(f"  WARNING: {n_missing} flights have no connectable arc!")
        if n_pruned_duty:
            print(f"  Duty-pruned  : {n_pruned_duty} arcs "
                  f"(exceeded {MAX_DUTY_MINUTES//60}h duty / "
                  f"{MAX_DUTY_DAYS}d period since last {MIN_REST//60}h rest)")
        if n_pruned_hb:
            print(f"  HB-pruned    : {n_pruned_hb} arcs fully pruned "
                  f"(all bases exceeded MAX_WORK_DAY={MAX_WORK_DAY} or "
                  f"MAX_AWAY_DAYS={MAX_AWAY_DAYS})")
        print(f"  Flight arcs: {n_arcs}  ({_t.time()-t0:.1f}s)")
        print(f"  Network complete: {len(self.nodes)} nodes, {len(self.arcs)} arcs  "
              f"(total {_t.time()-t0:.1f}s)")
    # ── Gurobi model ──────────────────────────

    def build_model(self):
        """
        Build the MIP with one integer flow variable per arc (not per crew).

        Uses batch variable creation and sparse constraint building for speed.
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

        # ── Reachability pruning ──────────────────────────────────────────────
        reachable = self.compute_reachable_arcs()

        # ── Batch variable creation ───────────────────────────────────────────
        flight_arcs = [a for a in arc_list if a.arc_type == 'flight']
        wait_arcs   = [a for a in arc_list if a.arc_type == 'wait']

        dh_cost_cache: dict[int, float] = {}
        for a in flight_arcs:
            if a.flight_id not in dh_cost_cache:
                f_obj = self.flights_by_id.get(a.flight_id)
                dh_cost_cache[a.flight_id] = deadhead_cost(f_obj) if f_obj else a.cost

        work_keys: list[tuple[str, Arc]] = []
        work_objs: list[float] = []
        dh_keys:   list[tuple[str, Arc]] = []
        dh_objs:   list[float] = []
        wait_keys: list[tuple[str, Arc]] = []
        wait_objs: list[float] = []

        # _arc_allowed_bases[arc_id] = set of bases whose crew may traverse
        # that flight arc without violating MAX_WORK_DAY or MAX_AWAY_DAYS.
        # Built during build_initial_network.  If the attribute is missing
        # (e.g. cached network from old code), fall back to allowing all bases.
        arc_allowed = getattr(self, '_arc_allowed_bases', None)

        for base in self.airports:
            reach = reachable[base]
            for arc in flight_arcs:
                if arc.id not in reach:
                    continue
                # Hard home-break enforcement: skip this (base, arc) pair if
                # the network build determined this base's crew cannot use it
                # without violating MAX_WORK_DAY or MAX_AWAY_DAYS.
                if arc_allowed is not None and base not in arc_allowed.get(arc.id, set()):
                    continue
                work_keys.append((base, arc))
                work_objs.append(arc.cost)
                dh_keys.append((base, arc))
                dh_objs.append(dh_cost_cache[arc.flight_id])
            for arc in wait_arcs:
                if arc.id in reach:
                    wait_keys.append((base, arc))
                    wait_objs.append(arc.cost)

        if work_keys:
            _wvars = self.model.addVars(
                len(work_keys), lb=0, obj=work_objs, vtype=GRB.CONTINUOUS,
                name=[f"fw_{b}_{a.id}" for b, a in work_keys])
            self.var_work = {k: _wvars[i] for i, k in enumerate(work_keys)}

        if dh_keys:
            _dvars = self.model.addVars(
                len(dh_keys), lb=0, obj=dh_objs, vtype=GRB.CONTINUOUS,
                name=[f"fd_{b}_{a.id}" for b, a in dh_keys])
            self.var_dh = {k: _dvars[i] for i, k in enumerate(dh_keys)}

        if wait_keys:
            _wtvars = self.model.addVars(
                len(wait_keys), lb=0, obj=wait_objs, vtype=GRB.CONTINUOUS,
                name=[f"wt_{b}_{a.id}" for b, a in wait_keys])
            self.var_wait = {k: _wtvars[i] for i, k in enumerate(wait_keys)}

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

        # ── Sparse flow-balance constraints ───────────────────────────────────
        out_terms: dict[str, dict[Node, list]] = {b: defaultdict(list) for b in self.airports}
        in_terms:  dict[str, dict[Node, list]] = {b: defaultdict(list) for b in self.airports}

        for (base, arc), var in self.var_work.items():
            out_terms[base][arc.start].append(var)
            in_terms[base][arc.end].append(var)
        for (base, arc), var in self.var_dh.items():
            out_terms[base][arc.start].append(var)
            in_terms[base][arc.end].append(var)
        for (base, arc), var in self.var_wait.items():
            out_terms[base][arc.start].append(var)
            in_terms[base][arc.end].append(var)

        for base in self.airports:
            supply  = self.base_crew[base]
            horizon = Node(airport=base, time=self.horizon_end)
            self.flow_constrs[base] = {}

            outs = out_terms[base]
            ins  = in_terms[base]

            # Build per-node supply injection map.
            # First window (no position info): all crew start at home base.
            # Subsequent windows: crew start wherever they actually ended up.
            node_supply: dict[Node, int] = {}
            if self.position_supply and base in self.position_supply:
                for ap, count in self.position_supply[base].items():
                    if count > 0:
                        node_supply[Node(airport=ap, time=DEPOT_TIME_START)] = count
                # Sanity: total injected must equal total crew for this base.
                injected = sum(node_supply.values())
                if injected != supply:
                    # Absorb rounding difference at home base.
                    home_depot = Node(airport=base, time=DEPOT_TIME_START)
                    node_supply[home_depot] = node_supply.get(home_depot, 0) + (supply - injected)
            else:
                node_supply[Node(airport=base, time=DEPOT_TIME_START)] = supply

            # ── Fix 2: validate injected supply nodes have outbound flow ─────
            # If a non-home node carries supply but has no outbound variables
            # (e.g. satellite airport with no arcs in this window), the flow
            # balance constraint would be infeasible or vacuous.  Redistribute
            # any such orphaned supply to the home depot.
            home_depot = Node(airport=base, time=DEPOT_TIME_START)
            orphaned = 0
            for node in list(node_supply.keys()):
                if node == home_depot:
                    continue
                if not outs.get(node):
                    orphaned += node_supply.pop(node)
            if orphaned:
                node_supply[home_depot] = node_supply.get(home_depot, 0) + orphaned
                print(f"  WARNING [{base}]: {orphaned} crew unit(s) injected at "
                      f"unreachable airport(s) — redistributed to home depot")

            for node in node_list:
                out_expr = gp.quicksum(outs.get(node, []))
                in_expr  = gp.quicksum(ins.get(node, []))

                inj = node_supply.get(node, 0)
                if node == horizon and self.is_last_window:
                    # Final window: all crew must return to home base.
                    constr = self.model.addConstr(
                        in_expr - out_expr == supply,
                        name=f"flow_{base}_{node.airport}_{node.time}_horizon"
                    )
                elif node == horizon:
                    # Intermediate window: horizon node is a free-exit sink.
                    # Flow arrives here from crew finishing their last leg;
                    # there are no outgoing arcs so we just need in >= out (>= 0)
                    # rather than == 0 which would block all arriving flow.
                    constr = self.model.addConstr(
                        in_expr - out_expr >= 0,
                        name=f"flow_{base}_{node.airport}_{node.time}_horizon"
                    )
                elif inj != 0:
                    constr = self.model.addConstr(
                        out_expr - in_expr == inj,
                        name=f"flow_{base}_{node.airport}_{node.time}_depot"
                    )
                elif not self.is_last_window and node.time >= self.flight_end:
                    # Intermediate window: any node at or after the commit cut
                    # is a free-exit point — crew whose last committed leg ends
                    # here stop and wait for the next window to pick them up.
                    # One-sided bound only; MIP lb=0 prevents phantom flow.
                    constr = self.model.addConstr(
                        out_expr - in_expr <= 0,
                        name=f"flow_{base}_{node.airport}_{node.time}_exit"
                    )
                else:
                    constr = self.model.addConstr(
                        out_expr - in_expr == 0,
                        name=f"flow_{base}_{node.airport}_{node.time}"
                    )
                self.flow_constrs[base][node] = constr

        self.model.update()
        print(f"  Flow balance constraints: {self.model.NumConstrs:,}  ({_t.time()-t0:.1f}s)")

        # ── Coverage constraints ──────────────────────────────────────────────
        arcs_by_flight = getattr(self, '_arcs_by_flight', {})
        for f in self.flights:
            if not getattr(f, 'needs_coverage', True):
                continue
            self._add_coverage_constr(f, arcs_by_flight)

        self.model.update()
        print(f"  Coverage constraints: {len(self.coverage_constrs)}  ({_t.time()-t0:.1f}s)")

        # ── Mandatory home-break constraints ──────────────────────────────────
        #
        # Home breaks are mandatory, not just incentivised.  Arc pruning (in
        # build_initial_network) prevents crew from taking flights once they
        # exceed MAX_AWAY_DAYS / MAX_WORK_DAY.  These MIP constraints add a
        # second, independent layer of enforcement at the flow level:
        #
        # For each base b and each MAX_AWAY_DAYS epoch k, the total crew-flow
        # that ARRIVES at the home base during epoch k must be >= base_crew[b].
        # This forces the aggregate flow to route all crew through home at
        # least once in every MAX_AWAY_DAYS window, complementing the per-arc
        # pruning which handles per-crew clock state.
        #
        # We count flow on wait arcs at the home airport whose *cumulative* wait
        # time reaches HOME_BREAK minutes (2880 min = 48 h).  Short isolated arcs
        # are excluded; consecutive short arcs whose running total crosses the
        # threshold are credited at the arc that pushes them over.  Arc pruning
        # (Bug 2 / Bug 3 fixes) independently prevents crews from departing home
        # before accumulating enough rest, so both layers now agree on what a
        # valid home break is.

        epoch_min  = MAX_AWAY_DAYS * 1440
        n_mand     = 0
        for base in self.airports:
            supply = self.base_crew[base]
            # Collect home wait arcs by epoch.
            # FIX (Bug 1): only include wait arcs whose *cumulative* home-wait
            # duration reaches HOME_BREAK minutes.  Previously every wait arc at
            # the home airport was accepted, so a 15-minute layover could satisfy
            # the mandatory-rest constraint just as well as a 48-hour break.
            # We walk arcs in chronological order and carry forward accumulated
            # wait time so that consecutive short arcs that together cross the
            # HOME_BREAK threshold are still credited, but short isolated arcs
            # are not.
            epoch_arcs: dict[int, list] = defaultdict(list)
            home_wait_acc: dict[int, int] = {}  # node.time -> cumulative minutes
            for arc in sorted(
                (a for a in self.arcs
                 if a.arc_type == 'wait' and a.start.airport == base
                 and (base, a) in self.var_wait),
                key=lambda a: a.start.time,
            ):
                dur = arc.end.time - arc.start.time
                prev = home_wait_acc.get(arc.start.time, 0)
                acc  = prev + dur
                home_wait_acc[arc.end.time] = acc
                if acc >= HOME_BREAK:
                    epoch = arc.start.time // epoch_min
                    epoch_arcs[epoch].append((base, arc))

            for epoch, arc_keys in epoch_arcs.items():
                rest_expr = gp.quicksum(self.var_wait[k] for k in arc_keys)
                self.model.addConstr(
                    rest_expr >= supply,
                    name=f"mand_rest_{base}_ep{epoch}"
                )
                n_mand += 1

        self.model.update()
        print(f"  Mandatory home-break constraints: {n_mand}  ({_t.time()-t0:.1f}s)")
        print(f"  Model built: {self.model.NumVars:,} vars, "
              f"{self.model.NumConstrs:,} constrs  ({_t.time()-t0:.1f}s)")

    def _add_coverage_constr(self, f: 'Flight',
                             arcs_by_flight: dict | None = None) -> bool:
        if f.id in self.coverage_constrs:
            return False
        if f.id not in self.slack_var:
            return False
        adict = arcs_by_flight if arcs_by_flight is not None else getattr(
            self, '_arcs_by_flight', {})
        flight_arcs = [a for a in adict.get(f.id, []) if a.arc_type == 'flight']
        if not flight_arcs:
            return False
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
        return True

    # ── Solve ─────────────────────────────────

    def solve(self, crew_positions: dict[int, str] | None = None) -> dict:
        """
        Solve directly as MIP. No iterative refinement needed — the network
        already has a node at every exact flight departure and arrival time.

        Parameters
        ----------
        crew_positions : {crew_id: airport}
            Where each crew member starts this window (from previous window's
            end positions).  Forwarded to _decompose_routes for identity-preserving
            route assignment.
        """
        print("\n=== Solving MIP ===")
        self._ensure_attrs()

        # Switch all variables to integer
        for d in (self.var_work, self.var_dh, self.var_wait):
            for var in d.values():
                var.VType = GRB.INTEGER
        for var in self.slack_var.values():
            var.VType = GRB.INTEGER

        dh_cost_map: dict[int, float] = {
            f.id: deadhead_cost(f) for f in self.flights
        }
        obj = (
            gp.quicksum(arc.cost * v for (base, arc), v in self.var_work.items())
            + gp.quicksum(
                dh_cost_map.get(arc.flight_id, arc.cost) * v
                for (base, arc), v in self.var_dh.items()
            )
            + gp.quicksum(arc.cost * v for (base, arc), v in self.var_wait.items())
            + gp.quicksum(COST_UNCOVERED * v for v in self.slack_var.values())
        )
        self.model.setObjective(obj, GRB.MINIMIZE)

        self.model.setParam("OutputFlag", 1)
        self.model.setParam("MIPGap", 0.01)
        self.model.setParam("Method", 2)
        self.model.setParam("Presolve", 2)
        self.model.setParam("MIPFocus", 1)
        self.model.setParam("Heuristics", 0.3)
        self.model.setParam("DisplayInterval", 30)
        self.model.setParam("TimeLimit", 7200)
        self.model.setParam("Cuts", 1)
        self.model.setParam("GomoryPasses", 0)
        self.model.setParam("Crossover", 1)
        self.model.setParam("NodefileStart", 4)
        self.model.setParam("Threads", 8)
        self.model.optimize()

        return self.extract_solution(crew_positions=crew_positions)

    # ── Solution extraction ───────────────────

    def extract_solution(self, crew_positions: dict[int, str] | None = None) -> dict:
        eps = 1e-4
        if self.model.Status not in (GRB.OPTIMAL, GRB.TIME_LIMIT, GRB.SUBOPTIMAL):
            return {"status": "infeasible", "cost": None, "routes": [],
                    "uncovered_flights": [], "uncovered_slots": 0,
                    "end_positions": {}}

        obj = self.model.ObjVal

        uncovered = []
        for f in self.flights:
            if f.id in self.slack_var:
                sv = self.slack_var[f.id].X
                if sv > eps:
                    uncovered.append((f, sv))

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

        routes, next_crew_positions = self._decompose_routes(
            crew_positions=crew_positions,
        )

        # ── Stage 2b: repair any remaining home-break violations ─────────────
        win_start_abs = getattr(self, '_win_start_min', 0)
        routes = self._repair_home_break_violations(
            routes, next_crew_positions, win_start_abs
        )

        # ── Rolling horizon: derive end positions from the per-crew position
        # map (single source of truth).  _extract_end_positions() used a
        # separate arc-straddle counter that could disagree with
        # _decompose_routes when a crew member was mid-flight at the commit
        # cut, causing phantom teleports in the next window.
        base_map = {c.id: c.base for c in self.crew}
        end_positions: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for cid, airport in next_crew_positions.items():
            home = base_map.get(cid)
            if home:
                end_positions[home][airport] += 1
        end_positions = {k: dict(v) for k, v in end_positions.items()}

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
            "crew_positions": next_crew_positions,
            "end_positions": end_positions,
            "num_flights": len([f for f in self.flights
                                 if getattr(f, 'needs_coverage', True)]),
            "covered_flights": len([f for f in self.flights
                                     if getattr(f, 'needs_coverage', True)]) - len(uncovered),
        }

    def _extract_end_positions(self) -> dict[str, dict[str, int]]:
        """
        For rolling horizon: determine how many crew from each home base are
        at each airport at t = flight_end (the commit cut-point).

        Returns {airport: {home_base: count}} so the next window can use the
        actual airport as the new depot, rather than assuming everyone is home.

        The commit point is flight_end (= step_days * 1440 in absolute time).
        We look at the flow on arcs that straddle or terminate at that time.
        """
        eps = 1e-4
        commit_t = self.flight_end
        # {(airport, home_base): count}
        positions: dict[tuple[str, str], int] = defaultdict(int)

        for (base, arc), var in {**self.var_work, **self.var_dh, **self.var_wait}.items():
            val = round(var.X) if var.X > eps else 0
            if val <= 0:
                continue
            # Crew on this arc are in-transit at commit_t if they departed before
            # and arrive after — treat them as being at arc.start.airport.
            # Crew who have finished this arc by commit_t are at arc.end.airport.
            if arc.start.time <= commit_t <= arc.true_end:
                positions[(arc.start.airport, base)] += val
            elif arc.true_end <= commit_t and arc.end.time >= commit_t:
                positions[(arc.end.airport, base)] += val

        # Reshape to {home_base: {airport: count}} — the shape position_supply expects.
        result: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for (airport, home_base), count in positions.items():
            result[home_base][airport] += count

        return {k: dict(v) for k, v in result.items()}

    # ── Stage 2: flow decomposition ───────────

    def _decompose_routes(
        self,
        crew_positions: dict[int, str] | None = None,
    ) -> tuple[list[dict], dict[int, str]]:
        """
        Decompose integer arc flows into individual crew routes.

        Parameters
        ----------
        crew_positions : {crew_id: airport}
            Where each crew member is at the start of this window
            (i.e. where they ended in the previous window).  None means all
            crew start at their home base (first window).

        Returns
        -------
        (routes, next_crew_positions)
            next_crew_positions is {crew_id: airport} at the end of each
            route's last leg — handed to the next window.
        """
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

        arc_is_work_for_base: set[tuple[str, int]] = set()
        for (base, arc), var in self.var_work.items():
            if round(var.X) > 0:
                arc_is_work_for_base.add((base, arc.id))

        routes = []
        # Seed next_crew_positions with incoming positions for ALL crew.
        # Crew who fly this window will overwrite their entry with their new
        # last-leg destination.  Crew who sit idle retain their current position
        # (or home base on the first window) — they don't teleport home.
        if crew_positions is not None:
            next_crew_positions: dict[int, str] = dict(crew_positions)
        else:
            # First window: everyone starts at home.
            next_crew_positions = {c.id: c.base for c in self.crew}

        for base in self.airports:
            horizon = Node(airport=base, time=self.horizon_end)
            supply  = self.base_crew[base]

            base_crew_ids = [c.id for c in self.crew if c.base == base]

            base_residual: dict[Arc, int] = {}
            for (b, arc), cap in residual.items():
                if b == base and cap > 0:
                    base_residual[arc] = base_residual.get(arc, 0) + cap

            # Build the ordered list of (crew_id, source_node) pairs.
            # Each crew member departs from wherever they currently are.
            home = Node(airport=base, time=DEPOT_TIME_START)
            ordered: list[tuple[int, Node]] = [
                (cid, Node(airport=next_crew_positions.get(cid, base),
                           time=DEPOT_TIME_START))
                for cid in base_crew_ids
            ]

            # Sort so crew whose starting airport has the least available flow
            # are traced first — they're most at risk of losing their path.
            def _depot_flow(depot: Node) -> int:
                return sum(
                    cap for arc, cap in base_residual.items()
                    if arc.start == depot
                )
            ordered.sort(key=lambda pair: _depot_flow(pair[1]))

            # Intermediate windows: accept any node at/after the commit cut as
            # a valid path terminal (crew park wherever their last leg lands).
            # Last window: must reach the home-base horizon node exactly.
            exit_cutoff = self.flight_end if not self.is_last_window else None

            for crew_id, depot in ordered:
                prior_airport = next_crew_positions.get(crew_id, base)

                # Carry the crew's home-wait accumulator into the DFS so it
                # can enforce the 48-h home-break rule inline.
                if not hasattr(self, '_hb_states'):
                    self._hb_states = {}
                prior_hb = self._hb_states.get(crew_id, {})
                # home_arr in prior state is an absolute minute; convert to
                # minutes-accumulated by comparing against the window start.
                prior_home_arr = prior_hb.get('home_arr', None)
                win_start_abs  = getattr(self, '_win_start_min', 0)
                if prior_home_arr is not None and prior_airport == base:
                    carry_hw = min(HOME_BREAK,
                                   win_start_abs - prior_home_arr)
                    carry_hw = max(0, carry_hw)
                elif prior_airport == base and not prior_hb:
                    # First window, crew start at home fully rested.
                    carry_hw = HOME_BREAK
                else:
                    carry_hw = 0

                path_arcs = self._trace_path(depot, horizon, base_residual,
                                             exit_cutoff=exit_cutoff,
                                             base=base,
                                             carry_home_wait=carry_hw)
                if not path_arcs and depot != home:
                    # Flow from this crew's position was already consumed —
                    # fall back to home so they still get a route this window.
                    # NOTE: this is a phantom move if the prior position ≠ home;
                    # we log it so the caller can audit cross-window continuity.
                    if prior_airport != base:
                        print(
                            f"  WARNING: crew {crew_id} (base {base}) fallback to "
                            f"home — position gap: was at {prior_airport}, "
                            f"no flow found there; starting from {base} instead."
                        )
                    path_arcs = self._trace_path(home, horizon, base_residual,
                                                 exit_cutoff=exit_cutoff,
                                                 base=base,
                                                 carry_home_wait=HOME_BREAK)
                if not path_arcs:
                    # Truly no flow left for this crew member this window.
                    # They stay wherever next_crew_positions already has them.
                    continue

                for arc in path_arcs:
                    base_residual[arc] -= 1
                    if base_residual[arc] == 0:
                        del base_residual[arc]

                legs = []
                for arc in path_arcs:
                    if arc.arc_type == 'wait':
                        continue
                    leg_type = 'flight' if (base, arc.id) in arc_is_work_for_base else 'deadhead'
                    legs.append({
                        "type":      leg_type,
                        "from":      arc.start.airport,
                        "to":        arc.end.airport,
                        "dep":       arc.start.time,
                        "arr":       arc.true_end,
                        "flight_id": arc.flight_id,
                    })

                if legs:
                    # ── Home-break compliance check (per-crew DP) ─────────────
                    hb_states = self._hb_states
                    prior     = hb_states.get(crew_id, {})

                    viols, new_state = self._check_home_break(
                        legs, base, win_start_abs,
                        carry_reset_at = prior.get('reset_at', 0),
                        carry_last_day = prior.get('last_day', -1),
                        carry_worked   = prior.get('worked',   0),
                        carry_home_arr = prior.get('home_arr', None),
                    )
                    # Persist updated state for next window
                    hb_states[crew_id] = new_state

                    if viols:
                        n_hb_violations = getattr(self, '_hb_violation_count', 0)
                        self._hb_violation_count = n_hb_violations + len(viols)
                        if n_hb_violations == 0:   # print first occurrence only
                            print(f"  HOME-BREAK violation crew {crew_id} ({base}): "
                                  f"{viols[0]}")

                    routes.append({
                        "crew_id":       crew_id,
                        "base":          base,
                        "crew_count":    1,
                        "legs":          legs,
                        "hb_violations": viols,
                    })
                    # Update this crew member's position to their last leg destination.
                    next_crew_positions[crew_id] = legs[-1]["to"]

        return routes, next_crew_positions

    # ── Home-break enforcement (Stage 2, per-crew) ───────────────────────────
    #
    # The MIP uses a flat network so it knows nothing about MAX_WORK_DAY or
    # MAX_AWAY_DAYS.  After each crew member's path is traced we simulate their
    # clocks exactly (like the Java DP does for individual workers) and flag any
    # window that contains a violation.
    #
    # Clock rules (identical to the spec):
    #   days_worked : # distinct calendar days with >=1 flight since last HOME_BREAK
    #   days_away   : # whole days elapsed since left home base
    #   Both reset only after HOME_BREAK minutes (48h) at home base.
    #   Violation: either counter reaches its limit without a home break first.

    @staticmethod
    def _check_home_break(legs: list[dict], base: str, win_start_min: int,
                          carry_reset_at: int = 0,
                          carry_last_day: int = -1,
                          carry_worked: int = 0,
                          carry_home_arr: int | None = None,
                          ) -> tuple[list[str], dict]:
        """
        Simulate home-reset clocks for one crew member's legs in this window.

        Accepts carry-over state from the previous window so multi-window
        violations are detected correctly.  win_start_min MUST be the absolute
        minute offset of the window start (0 for monolithic, win_start for
        rolling horizon) so that dep_abs is always a true planning-horizon minute.

        Returns (violations, state) where state carries into the next window.

        Clock rules (per spec)
        ----------------------
        MAX_WORK_DAY  : max distinct calendar days with >=1 flight since last reset.
        MAX_AWAY_DAYS : max calendar days elapsed since last reset.
        Both clocks reset ONLY after HOME_BREAK_DAYS full calendar days spent at
        the home base (i.e. crew arrive home and the next departure from home is
        >= HOME_BREAK minutes later).

        reset_at  : absolute minute from which away-time is measured.
                    Initialised to the start of planning (0).
                    Updated to the departure minute of the leg that follows a
                    valid home break.
        home_arr  : absolute minute of the most recent arrival at home base.
                    None if crew have not yet returned home this period.
        worked    : distinct calendar days with >=1 flight since reset_at.
        last_day  : calendar day index of the last counted flight (dedup).

        Violation flags are raised at most once per counter per period to avoid
        flooding the log with repeated messages for the same breach.
        """
        violations = []
        reset_at       = carry_reset_at
        last_day       = carry_last_day
        worked         = carry_worked
        home_arr       = carry_home_arr
        away_violated  = False   # raise each breach at most once
        work_violated  = False

        for leg in legs:
            # dep/arr in legs are window-local minutes; add offset for absolute time.
            dep_abs = leg["dep"] + win_start_min
            arr_abs = leg["arr"] + win_start_min
            frm     = leg["from"]
            to      = leg["to"]

            # ── Check for a valid home break before this departure ────────────
            # Condition: departing FROM home base AND previously arrived home
            # (home_arr is set) AND waited >= HOME_BREAK minutes.
            if frm == base and home_arr is not None:
                wait = dep_abs - home_arr
                if wait >= HOME_BREAK:
                    # Valid 2-day home break: reset both clocks from departure.
                    reset_at      = dep_abs
                    last_day      = -1
                    worked        = 0
                    away_violated = False
                    work_violated = False
                # Crew is now departing home regardless — clear home_arr.
                home_arr = None

            # ── Accumulate days worked (one entry per distinct calendar day) ──
            dep_day = dep_abs // 1440
            if dep_day != last_day:
                worked  += 1
                last_day = dep_day

            # ── Days away since last reset ────────────────────────────────────
            away = (dep_abs - reset_at) / 1440.0

            # ── Check limits — flag first breach only per period ─────────────
            day_label = dep_day + 1   # 1-indexed for display
            if away > MAX_AWAY_DAYS and not away_violated:
                violations.append(
                    f"Day {day_label}: away {away:.1f}d > {MAX_AWAY_DAYS}d "
                    f"({frm}->{to})"
                )
                away_violated = True

            if worked > MAX_WORK_DAY and not work_violated:
                violations.append(
                    f"Day {day_label}: worked {worked}d > {MAX_WORK_DAY}d "
                    f"({frm}->{to})"
                )
                work_violated = True

            # ── Track home arrival ────────────────────────────────────────────
            # Only update when arriving at home base; do NOT clear when departing
            # from a non-home airport (crew may still fly home later).
            if to == base:
                home_arr = arr_abs

        state = {
            "reset_at": reset_at,
            "last_day":  last_day,
            "worked":    worked,
            "home_arr":  home_arr,
        }
        return violations, state

    def _repair_home_break_violations(
        self,
        routes: list[dict],
        next_crew_positions: dict[int, str],
        win_start_min: int,
    ) -> list[dict]:
        """
        Post-solve repair pass: for any crew route that still violates
        MAX_WORK_DAY or MAX_AWAY_DAYS, insert a synthetic 48-h home-base
        layover after the last leg that doesn't yet breach the limit.

        This is a best-effort greedy fix.  It trims the offending tail of the
        route and records the crew as parked at home for the next window.
        It does NOT re-optimise coverage — any flights that were covered by
        the trimmed legs will show as uncovered in the window stats.

        Parameters
        ----------
        routes             : list of route dicts (mutated in-place)
        next_crew_positions: {crew_id: airport} — updated if trimming changes
                             a crew member's end position
        win_start_min      : absolute minute offset for clock arithmetic

        Returns
        -------
        The (possibly modified) routes list.
        """
        hb_states = getattr(self, '_hb_states', {})
        repaired  = 0

        for route in routes:
            crew_id = route["crew_id"]
            base    = route["base"]
            legs    = route["legs"]
            if not legs:
                continue

            # Re-run the home-break check to get per-leg clock state.
            prior = hb_states.get(crew_id, {})
            viols, _ = self._check_home_break(
                legs, base, win_start_min,
                carry_reset_at = prior.get('reset_at', 0),
                carry_last_day = prior.get('last_day', -1),
                carry_worked   = prior.get('worked',   0),
                carry_home_arr = prior.get('home_arr', None),
            )
            if not viols:
                continue

            # Walk legs one-by-one, re-simulating clocks, and cut at first breach.
            reset_at      = prior.get('reset_at', 0)
            last_day      = prior.get('last_day', -1)
            worked        = prior.get('worked',   0)
            home_arr      = prior.get('home_arr', None)
            cut_idx       = None

            for i, leg in enumerate(legs):
                dep_abs = leg["dep"] + win_start_min
                arr_abs = leg["arr"] + win_start_min
                frm     = leg["from"]
                to      = leg["to"]

                if frm == base and home_arr is not None:
                    if dep_abs - home_arr >= HOME_BREAK:
                        reset_at = dep_abs
                        last_day = -1
                        worked   = 0
                    home_arr = None

                dep_day = dep_abs // 1440
                if dep_day != last_day:
                    worked  += 1
                    last_day = dep_day

                away = (dep_abs - reset_at) / 1440.0

                if away > MAX_AWAY_DAYS or worked > MAX_WORK_DAY:
                    cut_idx = i
                    break

                if to == base:
                    home_arr = arr_abs

            if cut_idx is not None and cut_idx > 0:
                # Trim route to the legs before the first breach.
                route["legs"] = legs[:cut_idx]
                last_leg      = legs[cut_idx - 1]
                end_airport   = last_leg["to"]
                next_crew_positions[crew_id] = end_airport
                route["hb_violations"] = viols
                repaired += 1

                # Update the crew's carry state to reflect the trimmed route.
                _, new_state = self._check_home_break(
                    route["legs"], base, win_start_min,
                    carry_reset_at = prior.get('reset_at', 0),
                    carry_last_day = prior.get('last_day', -1),
                    carry_worked   = prior.get('worked',   0),
                    carry_home_arr = prior.get('home_arr', None),
                )
                hb_states[crew_id] = new_state

        if repaired:
            print(f"  Home-break repair: trimmed {repaired} route(s) at violation point")

        self._hb_states = hb_states
        return routes

    def _trace_path(
        self,
        source: Node,
        sink: Node,
        residual: dict[Arc, int],
        exit_cutoff: int | None = None,
        base: str | None = None,
        carry_home_wait: int = HOME_BREAK,
    ) -> list[Arc] | None:
        """
        Greedy DFS from source to sink (or any node at/after exit_cutoff) through
        residual flow graph.  Returns list of arcs forming one unit-flow path,
        or None if none found.

        exit_cutoff : if set (intermediate windows), any node whose time >=
            exit_cutoff is a valid terminal — crew don't need to return to the
            home-base horizon node yet.  The explicit sink is still checked first
            so last-window behaviour is unchanged.
        Prefers flight > deadhead > wait arcs for meaningful pairings.

        base / carry_home_wait : when set, the DFS enforces the 48-hour home-break
            rule inline.  carry_home_wait is the accumulated home-wait minutes
            at the source node (HOME_BREAK = fully rested / never been away;
            0 = just arrived home with no rest yet).  A flight arc departing
            FROM the home airport is only traversed if the running home-wait
            accumulator at that node has reached HOME_BREAK.  Wait arcs at the
            home airport increment the accumulator; any other arc resets it to 0.
            This ensures the DFS naturally routes through valid 48-h home breaks
            rather than touching home briefly and immediately departing.
        """
        type_priority = {'flight': 0, 'deadhead': 1, 'wait': 2}

        parent: dict[Node, Arc | None] = {source: None}
        # home_wait[node] = accumulated minutes at home base up to this node
        home_wait: dict[Node, int] = {source: carry_home_wait}
        # Track the best terminal node found (latest time at/after cutoff)
        best_terminal: Node | None = None
        stack = [source]

        while stack:
            node = stack.pop()
            hw_here = home_wait.get(node, 0)

            if node == sink:
                # Exact horizon match — reconstruct immediately.
                path: list[Arc] = []
                cur = sink
                while parent[cur] is not None:
                    arc = parent[cur]
                    path.append(arc)
                    cur = arc.start
                path.reverse()
                return path

            if exit_cutoff is not None and node.time >= exit_cutoff and node != source:
                # Valid free-exit terminal: record the one with the latest time
                # (deepest into the window) so we commit the most work.
                if best_terminal is None or node.time > best_terminal.time:
                    best_terminal = node

            candidates = [
                a for a in self.arcs_from.get(node, [])
                if residual.get(a, 0) > 0 and a.end not in parent
            ]

            # Home-break enforcement: if this node is at the home airport and the
            # crew have not yet accumulated HOME_BREAK minutes here, block any
            # outbound flight/deadhead arc.  Wait arcs at home are always allowed
            # (they accumulate rest); return-to-home arcs are always allowed.
            if base is not None and node.airport == base and hw_here < HOME_BREAK:
                candidates = [
                    a for a in candidates
                    if a.arc_type == 'wait'          # keep accumulating rest
                    or a.end.airport == base         # allow further home arrivals
                ]

            candidates.sort(key=lambda a: (type_priority.get(a.arc_type, 9), a.end.time))

            for arc in reversed(candidates):
                if arc.end in parent:
                    continue
                parent[arc.end] = arc
                # Update running home-wait accumulator for the next node.
                if base is not None:
                    if arc.arc_type == 'wait' and arc.start.airport == base:
                        hw_next = hw_here + (arc.end.time - arc.start.time)
                    elif arc.end.airport == base:
                        # Arriving at home: start counting rest from 0.
                        hw_next = arc.end.time - arc.end.time  # = 0
                    else:
                        # Away from home: accumulator does not grow.
                        hw_next = 0
                else:
                    hw_next = HOME_BREAK  # no enforcement
                home_wait[arc.end] = hw_next
                stack.append(arc.end)

        # No path to exact sink — if a free-exit terminal was found, use it.
        if best_terminal is not None:
            path: list[Arc] = []
            cur = best_terminal
            while parent[cur] is not None:
                arc = parent[cur]
                path.append(arc)
                cur = arc.start
            path.reverse()
            return path

        return None


# ─────────────────────────────────────────────
# ROLLING HORIZON DRIVER
# ─────────────────────────────────────────────

def _slice_flights_for_window(
    all_flights: list[Flight],
    win_start_min: int,
    win_end_min: int,      # commit point (= step end)
    horizon_end_min: int,  # return tail end
) -> list[Flight]:
    """
    Return flights that fall within [win_start_min, horizon_end_min).
    dep_min and arr_min are already absolute (minutes from day 0 of dataset).
    needs_coverage is True iff dep_min < win_end_min.
    """
    result = []
    for f in all_flights:
        if f.dep_min >= horizon_end_min or f.arr_min <= win_start_min:
            continue
        # Shift times so window starts at t=0
        shifted = Flight(
            id=f.id,
            origin=f.origin,
            dest=f.dest,
            dep_min=f.dep_min - win_start_min,
            arr_min=f.arr_min - win_start_min,
            duration=f.duration,
            min_crew=f.min_crew,
            flight_num=f.flight_num,
            distance=f.distance,
            seats=f.seats,
        )
        needs_cov = f.dep_min < win_end_min
        object.__setattr__(shifted, 'needs_coverage', needs_cov)
        result.append(shifted)
    return result


def _build_supply_from_positions(
    end_positions: dict[str, dict[str, int]],
    home_base: str,
    airports: list[str],
) -> dict[str, int]:
    """
    Convert the end_positions map from the previous window into a supply
    vector for the next window.

    For a given home_base, total crew at each airport = how many crew
    from that home base are there.  This becomes the new supply so the
    MIP correctly starts crew where they actually are.
    """
    supply: dict[str, int] = defaultdict(int)
    for airport, base_counts in end_positions.items():
        count = base_counts.get(home_base, 0)
        if count > 0 and airport in airports:
            supply[airport] += count
    return dict(supply)


def solve_rolling_horizon(
    all_flights: list[Flight],
    crew: list[CrewMember],
    days: int,
    window_days: int = WINDOW_DAYS,
    step_days: int = STEP_DAYS,
    return_window: int = RETURN_WINDOW,
    verbose: bool = True,
) -> dict:
    """
    Solve a long planning horizon by rolling a shorter solve window forward.

    Parameters
    ----------
    all_flights   : full set of flights (absolute dep/arr times from day 0)
    crew          : crew list (from assign_crew_bases on the full horizon)
    days          : total days to plan (DAYS_TO_SOLVE)
    window_days   : length of each solve window in days
    step_days     : days committed per step (overlap = window_days - step_days)
    return_window : tail days appended for return-arc feasibility
    verbose       : print progress

    Returns
    -------
    Aggregated result dict compatible with save_result / the visualiser.
    """
    import time as _t

    airports = sorted(set(c.base for c in crew))
    total_min = days * 1440
    window_min = window_days * 1440
    step_min   = step_days * 1440
    tail_min   = return_window * 1440

    all_routes: list[dict] = []
    all_uncovered: list[tuple] = []
    total_cost = 0.0
    total_flight_cost = 0.0
    total_dh_cost = 0.0
    total_wait_cost = 0.0
    total_uncovered_slots = 0.0
    covered_count = 0
    flight_count = 0

    # {crew_id: airport} — where each crew member ends each window, so the next
    # window can start each crew from their actual location and preserve identity.
    # Also used to derive position_supply so both sources are always consistent.
    crew_positions: dict[int, str] | None = None
    hb_states: dict[int, dict] = {}   # home-break clock carry-over per crew

    step = 0
    win_start = 0
    while win_start < total_min:
        win_commit = min(win_start + step_min, total_min)
        win_horizon = min(win_start + window_min + tail_min, total_min + tail_min)

        if verbose:
            print(f"\n{'='*60}")
            print(f"ROLLING WINDOW {step+1}  |  "
                  f"days {win_start//1440+1}–{win_commit//1440}  "
                  f"(solve horizon: day {win_horizon//1440})")
            print(f"{'='*60}")

        # ── Slice flights for this window ─────────────────────────────────────
        win_flights = _slice_flights_for_window(
            all_flights,
            win_start_min=win_start,
            win_end_min=win_commit,
            horizon_end_min=win_horizon,
        )

        if not win_flights:
            if verbose:
                print("  No flights in this window — skipping.")
            win_start += step_min
            step += 1
            continue

        # ── Build position_supply from crew_positions (single source of truth) ──
        # Derive {home_base: {airport: count}} directly from the per-crew
        # endpoint map so the flow supply and route decomposition are always
        # consistent — no separate _extract_end_positions needed here.
        if crew_positions is not None:
            win_position_supply: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
            base_map = {c.id: c.base for c in crew}
            for cid, airport in crew_positions.items():
                home = base_map.get(cid)
                if home:
                    win_position_supply[home][airport] += 1
            win_position_supply = {k: dict(v) for k, v in win_position_supply.items()}
        else:
            win_position_supply = None

        # ── Fix 3c: assert supply totals match crew counts ────────────────────
        # If the position map is set, every base must inject exactly the right
        # number of crew units — any mismatch means the prior window's position
        # tracker and the crew roster have diverged.
        if win_position_supply is not None:
            base_crew_counts = defaultdict(int)
            for c in crew:
                base_crew_counts[c.base] += 1
            for base, airport_counts in win_position_supply.items():
                injected = sum(airport_counts.values())
                expected = base_crew_counts.get(base, 0)
                if injected != expected:
                    print(
                        f"  WARNING [window {step+1}]: supply mismatch for base "
                        f"{base}: position map injects {injected} but roster "
                        f"has {expected}. Correcting."
                    )
                    # Absorb the difference at the home depot entry.
                    diff = expected - injected
                    win_position_supply[base][base] = (
                        win_position_supply[base].get(base, 0) + diff
                    )

        # ── Build and solve the window network ────────────────────────────────
        win_horizon_local = win_horizon - win_start   # time in window-local minutes
        win_commit_local  = win_commit  - win_start

        is_last = (win_commit >= total_min)
        net = CrewFlowNetwork(
            flights=win_flights,
            crew=crew,
            horizon_end=win_horizon_local,
            flight_end=win_commit_local,
            time_bucket=TIME_BUCKET,
            verbose=verbose,
            window_offset=win_start,
            position_supply=win_position_supply,
        )
        net.is_last_window      = is_last
        net._win_start_min      = win_start   # absolute minute offset for clock arithmetic
        net._hb_violation_count = 0
        net._hb_states          = hb_states   # carry-over detection state from prior window

        # ── Carry home-break network state into build_initial_network ─────────
        # hb_states[crew_id] = {reset_at, worked, last_day, home_arr}
        # We need a per-BASE aggregate: the worst-case (highest worked, lowest
        # reset_at) across all crew from that base, so the network pruning is
        # conservative (never allows more than the most-constrained crew).
        # Using worst-case means some crew may still have budget the network
        # doesn't give them, but it guarantees no violations slip through.
        if hb_states:
            base_map = {c.id: c.base for c in crew}
            # {base: (reset_at, worked, last_day, home_wait_acc)}
            hb_carry_state: dict[str, tuple[int,int,int,int]] = {}
            for cid, state in hb_states.items():
                base = base_map.get(cid)
                if base is None:
                    continue
                # Convert absolute reset_at to window-local by subtracting win_start
                reset_abs = state.get('reset_at', 0)
                reset_local = reset_abs - win_start   # may be negative for old resets
                worked   = state.get('worked',   0)
                last_day = state.get('last_day', -1)
                # Window-local last_day: convert abs day to local day index
                last_day_local = (last_day * 1440 - win_start) // 1440 if last_day >= 0 else -1
                # home_wait_acc: if crew are at home at window end, carry forward
                # however many minutes they've accumulated; otherwise reset to 0.
                # hb_states tracks home_arr (when they last arrived home).
                home_arr = state.get('home_arr', None)
                if home_arr is not None:
                    home_wait_acc = max(0, win_start + (win_commit - win_start) - home_arr)
                    home_wait_acc = min(home_wait_acc, HOME_BREAK)  # cap at threshold
                else:
                    home_wait_acc = 0

                existing = hb_carry_state.get(base)
                if existing is None:
                    hb_carry_state[base] = (reset_local, worked, last_day_local, home_wait_acc)
                else:
                    # Keep worst case: highest worked, then earliest (lowest) reset,
                    # then lowest home_wait_acc (least rested — most conservative).
                    ex_reset, ex_worked, ex_last, ex_hw = existing
                    if (worked > ex_worked
                            or (worked == ex_worked and reset_local < ex_reset)
                            or (worked == ex_worked and reset_local == ex_reset
                                and home_wait_acc < ex_hw)):
                        hb_carry_state[base] = (reset_local, worked, last_day_local, home_wait_acc)
            net._hb_carry_state = hb_carry_state
        else:
            net._hb_carry_state = {}

        net.build_initial_network()
        net.build_model()
        result = net.solve(crew_positions=crew_positions)

        t_solve = _t.time()

        status = result.get("status", "unknown")
        if verbose:
            print(f"  Window status : {status}")
            cost_val = result.get('cost') or 0.0
            print(f"  Window cost   : {cost_val:,.1f}")
            print(f"  Flights in window     : {result.get('num_flights', 0)}")
            print(f"  Covered               : {result.get('covered_flights', 0)}")
            print(f"  Uncovered slots       : {result.get('uncovered_slots', 0):.1f}")

        if verbose:
            hb_viols = getattr(net, '_hb_violation_count', 0)
            if hb_viols:
                print(f"  Home-break violations : {hb_viols} (crew rescheduling needed)")
            else:
                print(f"  Home-break violations : 0  ✓")

        # ── Translate routes back to absolute time ────────────────────────────
        win_abs_routes = []
        for route in result.get("routes", []):
            abs_route = dict(route)
            abs_legs = []
            for leg in route["legs"]:
                abs_legs.append({
                    **leg,
                    "dep": leg["dep"] + win_start,
                    "arr": leg["arr"] + win_start,
                })
            abs_route["legs"] = abs_legs
            all_routes.append(abs_route)
            win_abs_routes.append(abs_route)

        if verbose:
            print(f"\n  Routes this window ({len(win_abs_routes)} crew with legs):")
            for route in win_abs_routes:
                if not route["legs"]:
                    continue
                print(f"    Crew #{route['crew_id']:>4d} | base {route['base']}")
                for leg in route["legs"]:
                    dep_total = leg["dep"]
                    arr_total = leg["arr"]
                    day_dep, hm_dep = divmod(dep_total, 1440)
                    day_arr, hm_arr = divmod(arr_total, 1440)
                    h_dep, m_dep = divmod(hm_dep, 60)
                    h_arr, m_arr = divmod(hm_arr, 60)
                    print(f"      [{leg['type']:8s}] {leg['from']} -> {leg['to']}"
                          f"  Day{day_dep+1} {h_dep:02d}:{m_dep:02d}"
                          f" -> Day{day_arr+1} {h_arr:02d}:{m_arr:02d}")

        # ── Translate uncovered flights back to absolute time ─────────────────
        for f, slots in result.get("uncovered_flights", []):
            # Reconstruct absolute-time flight object for reporting
            abs_f = Flight(
                id=f.id,
                origin=f.origin,
                dest=f.dest,
                dep_min=f.dep_min + win_start,
                arr_min=f.arr_min + win_start,
                duration=f.duration,
                min_crew=f.min_crew,
                flight_num=f.flight_num,
                distance=f.distance,
                seats=f.seats,
            )
            object.__setattr__(abs_f, 'needs_coverage', True)
            all_uncovered.append((abs_f, slots))

        total_cost            += result.get("cost") or 0.0
        total_flight_cost     += result.get("flight_cost", 0.0)
        total_dh_cost         += result.get("deadhead_cost", 0.0)
        total_wait_cost       += result.get("wait_cost", 0.0)
        total_uncovered_slots += result.get("uncovered_slots", 0.0)
        covered_count         += result.get("covered_flights", 0)
        flight_count          += result.get("num_flights", 0)

        crew_positions = result.get("crew_positions") or crew_positions
        hb_states = getattr(net, "_hb_states", hb_states)  # carry clock state forward

        win_start += step_min
        step += 1

    # ── Merge per-window routes into one route per crew member ──────────────
    # Each window emits a separate route dict per crew_id.  The visualiser
    # indexes routes by crew_id, so multiple entries for the same crew_id
    # mean only the last window's legs are visible — all earlier windows
    # appear empty.  Merge all legs into a single chronological route entry.
    merged: dict[int, dict] = {}
    for route in all_routes:
        cid = route["crew_id"]
        if cid not in merged:
            merged[cid] = {
                "crew_id":    cid,
                "base":       route["base"],
                "crew_count": 1,
                "legs":       [],
            }
        merged[cid]["legs"].extend(route["legs"])

    # Sort each crew member's legs chronologically and deduplicate
    # (the rolling overlap can produce the same leg twice in adjacent windows).
    seen_legs: dict[int, set] = {}
    for cid, route in merged.items():
        unique_legs = []
        seen = seen_legs.setdefault(cid, set())
        for leg in sorted(route["legs"], key=lambda l: l["dep"]):
            key = (leg["from"], leg["to"], leg["dep"], leg["arr"])
            if key not in seen:
                seen.add(key)
                unique_legs.append(leg)
        route["legs"] = unique_legs

    merged_routes = list(merged.values())

    return {
        "status": "rolling_horizon",
        "cost": total_cost,
        "flight_cost": total_flight_cost,
        "deadhead_cost": total_dh_cost,
        "wait_cost": total_wait_cost,
        "uncovered_slots": total_uncovered_slots,
        "uncovered_flights": all_uncovered,
        "routes": merged_routes,
        "num_flights": flight_count,
        "covered_flights": covered_count,
    }


# ─────────────────────────────────────────────
# RESULT SERIALISATION
# ─────────────────────────────────────────────

def save_result(result: dict, flights: list[Flight], crew: list[CrewMember],
                horizon_end: int, flight_end: int,
                out_path: str = "crew_result.json"):
    import json

    routed_crew_ids = {r["crew_id"] for r in result.get("routes", [])}
    planning_flights = [f for f in flights if getattr(f, 'needs_coverage', True)]

    payload = {
        "meta": {
            "days": flight_end // 1440,
            "horizon_end": horizon_end,
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
            for c in crew
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
    n_sitting = len(crew) - n_routed
    print(f"\nResult saved to {out_path}")
    print(f"  {n_routed} crew with routes, {n_sitting} crew sitting at base")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def _network_cache_path(csv_path: str, days: int) -> str:
    import os
    base = os.path.splitext(os.path.basename(csv_path))[0]
    return (f"{base}_d{days}_r{RETURN_WINDOW}_rest{MIN_REST//60}_duty"
            f"{MAX_DUTY_MINUTES//60}_days{MAX_DUTY_DAYS}_hb{HOME_BREAK_DAYS}"
            f"_flow_network.pkl")


def main(csv_path: str, days: int = DAYS_TO_SOLVE,
         use_cache: bool = True,
         rolling: bool = ROLLING_HORIZON):
    import time as _time
    import pickle, os

    t0 = _time.time()

    # ── Two-pass parse: find true week_start first, then load flights ─────────
    print("Scanning CSV for earliest date (two-pass fix)...")
    week_start = _find_week_start(csv_path)
    print(f"  Anchor date: {week_start.date()}")

    horizon_days = days + RETURN_WINDOW
    flights, _ = parse_flights(csv_path, days,
                               horizon_days=horizon_days,
                               week_start=week_start)
    if not flights:
        print("No flights loaded. Check CSV path and format.")
        return

    flight_end  = days * 1440
    horizon_end = flight_end + RETURN_WINDOW * 1440
    print(f"Flight window : day 1–{days}  |  Return deadline: day {days + RETURN_WINDOW}")

    # ── Crew base assignment (done once on the full horizon) ──────────────────
    crew = assign_crew_bases(flights)

    # ── Solve: rolling horizon or monolithic ──────────────────────────────────
    if rolling:
        print(f"\nUsing ROLLING HORIZON  "
              f"(window={WINDOW_DAYS}d, step={STEP_DAYS}d, "
              f"overlap={WINDOW_DAYS - STEP_DAYS}d)")
        result = solve_rolling_horizon(
            all_flights=flights,
            crew=crew,
            days=days,
            window_days=WINDOW_DAYS,
            step_days=STEP_DAYS,
            return_window=RETURN_WINDOW,
        )
    else:
        print("\nUsing MONOLITHIC solve")
        cache_path = _network_cache_path(csv_path, days)

        if use_cache and os.path.exists(cache_path):
            print(f"Loading cached network from {cache_path} ...")
            with open(cache_path, "rb") as fh:
                net = pickle.load(fh)
            print(f"  Loaded: {len(net.nodes)} nodes, {len(net.arcs)} arcs  "
                  f"({_time.time()-t0:.1f}s)")
        else:
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
        net._win_start_min = 0   # monolithic: all times are absolute from day 0
        result = net.solve()

    t1 = _time.time()

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "="*60)
    print("SOLUTION SUMMARY")
    print("="*60)
    print(f"Mode            : {'Rolling horizon' if rolling else 'Monolithic'}")
    print(f"Status          : {result['status']}")
    print(f"Total cost      : {result['cost']:,.1f}" if result['cost'] else "No solution")
    print(f"  Flight hours  : {result.get('flight_cost', 0):,.1f}")
    print(f"  Deadhead      : {result.get('deadhead_cost', 0):,.1f}")
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

    first_five: list[int] = []
    for route in result['routes']:
        if route['crew_id'] not in first_five:
            first_five.append(route['crew_id'])
        if len(first_five) == 5:
            break

    for i, route in enumerate(result['routes']):
        if route['crew_id'] not in first_five:
            continue
        print(f"\n  Route {i+1} | Crew #{route['crew_id']} | Base: {route['base']}")
        for leg in route['legs']:
            dep_h, dep_m = divmod(leg['dep'], 60)
            arr_h, arr_m = divmod(leg['arr'], 60)
            day_dep = dep_h // 24
            day_arr = arr_h // 24
            print(f"    [{leg['type']:8s}] {leg['from']} -> {leg['to']}  "
                  f"Day{day_dep+1} {dep_h%24:02d}:{dep_m:02d} -> "
                  f"Day{day_arr+1} {arr_h%24:02d}:{arr_m:02d}")

    result_path = os.path.splitext(csv_path)[0] + "_result.json"
    save_result(result, flights, crew,
                horizon_end=horizon_end,
                flight_end=flight_end,
                out_path=result_path)

    return result, flights, crew


if __name__ == "__main__":
    import sys
    path    = sys.argv[1] if len(sys.argv) > 1 else "data/flights_enriched.csv"
    rolling = "--monolithic" not in sys.argv   # default: rolling horizon
    main(path, days=DAYS_TO_SOLVE, rolling=rolling)