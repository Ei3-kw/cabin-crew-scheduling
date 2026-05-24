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

Key advantages over individual-variable formulation:
  - Variables: O(|arcs|) instead of O(|crew| × |arcs|)
  - Constraints: O(|nodes|) instead of O(|crew| × |nodes|)
  - Crew identity restored in Stage 2 (flow decomposition) — trivial post-solve
  - Same optimality guarantees; same result file format

Data:
  - flights_enriched.csv (BTS On-Time + FAA registry join)
  - Uses first 3 days; bases chosen by weighted k-median + satellite pre-positioning
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

MAX_DUTY_MINUTES   = 20 * 60
MAX_DUTY_DAYS      = 5

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

    args: (fwd_start_id, bwd_start_id)
    Returns (fwd_reachable_arc_ids, bwd_reachable_arc_ids).
    """
    import heapq
    fwd_start, bwd_start = args
    graph   = _WORKER_FWD_GRAPH
    bgraph  = _WORKER_BWD_GRAPH
    horizon = _WORKER_HORIZON

    def _run(start_id: int, g: dict) -> set[int]:
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

    return _run(fwd_start, graph), _run(bwd_start, bgraph)


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

        self.base_crew: dict[str, int] = defaultdict(int)
        for c in crew:
            self.base_crew[c.base] += 1
        self.airports = sorted(self.base_crew.keys())

        self.horizon_end = horizon_end
        self.flight_end  = flight_end if flight_end is not None else horizon_end
        self.time_bucket = time_bucket
        self.verbose     = verbose

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
        self.var_work:  dict[tuple[str, Arc], gp.Var] = {}
        self.var_dh:    dict[tuple[str, Arc], gp.Var] = {}
        self.var_wait:  dict[tuple[str, Arc], gp.Var] = {}
        self.slack_var: dict[int, gp.Var] = {}

        # Constraints
        self.flow_constrs:     dict[str, dict[Node, gp.Constr]] = {}
        self.coverage_constrs: dict[int, gp.Constr] = {}

    # ── Pickle compatibility ──────────────────
    CACHE_VERSION = 1
    _ATTR_DEFAULTS: dict = {'min_duty_at': {}, '_arcs_by_flight': {},
                            '_arc_key_set': set(), 'duty_period_start_at': {}}

    def __getstate__(self):
        state = self.__dict__.copy()
        state['_cache_version'] = self.CACHE_VERSION
        for key in ('model', 'var_work', 'var_dh', 'var_wait',
                    'slack_var', 'flow_constrs', 'coverage_constrs'):
            state.pop(key, None)
        return state

    def __setstate__(self, state):
        import copy
        for key in ('var_work', 'var_dh', 'var_wait', 'slack_var',
                    'flow_constrs', 'coverage_constrs'):
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

        task_args:  list[tuple[int, int]] = []
        base_order: list[str] = []
        for base in self.airports:
            depot_id   = node_to_id.get(Node(airport=base, time=DEPOT_TIME_START))
            horizon_id = node_to_id.get(Node(airport=base, time=horizon))
            if depot_id is None or horizon_id is None:
                continue
            task_args.append((depot_id, horizon_id))
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
        import time as _t
        self._build_flight_index()
        print("Building network...")
        t0 = _t.time()

        # 1. Depot + horizon nodes for each base
        for ap in self.airports:
            for t in (DEPOT_TIME_START, self.horizon_end):
                node = Node(airport=ap, time=t)
                self.nodes.add(node)
                self.nodes_by_airport[ap].add(node)
        print(f"  Depot/horizon nodes: {len(self.nodes)}  ({_t.time()-t0:.1f}s)")

        # 2. One node per exact flight dep/arr time — network is fully
        #    discretised at build time, so no iterative refinement is needed.
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

        # 3. Wait-arc chains
        for ap in self.airports:
            nodes = self.nodes_by_airport[ap]
            for i in range(len(nodes) - 1):
                self._create_wait_arc(nodes[i], nodes[i + 1])
        print(f"  Wait arcs built  ({_t.time()-t0:.1f}s)")

        # 4. Initialise duty tracking at every base depot.
        #    duty_period_start_at[node] = wall-clock minute when the current
        #    duty period began (used to enforce MAX_DUTY_DAYS).
        for ap in self.airports:
            depot = Node(airport=ap, time=DEPOT_TIME_START)
            self.min_duty_at[depot]          = 0
            self.duty_period_start_at[depot] = DEPOT_TIME_START

        # 5. Forward pass in time order: propagate duty through wait arcs
        #    (resetting on MIN_REST) and build flight arcs simultaneously.
        #
        #    Because the network is a DAG ordered by time, processing nodes
        #    earliest-first guarantees every node's duty is finalised before
        #    any outgoing arc is considered.
        #
        #    Duty rules:
        #      wait arc >= MIN_REST  →  duty resets to 0, new period starts
        #      wait arc <  MIN_REST  →  duty carries over (still in duty period)
        #      flight arc            →  duty += flight.duration
        #      duty > MAX_DUTY_MINUTES          →  arc pruned
        #      arr_min - period_start > MAX_DUTY_DAYS*1440  →  arc pruned

        # Index flights by (origin, dep_min) for O(1) lookup per node
        flights_departing: dict[tuple[str, int], list] = defaultdict(list)
        for f in self.flights:
            flights_departing[(f.origin, f.dep_min)].append(f)

        n_arcs = 0
        n_missing = 0
        n_pruned_duty = 0
        n_pruned_days = 0

        for node in sorted(self.nodes, key=lambda n: (n.time, n.airport)):
            duty_here = self.min_duty_at.get(node)
            if duty_here is None:
                continue  # node unreachable from any depot

            period_start = self.duty_period_start_at.get(node, node.time)

            # ── Propagate duty through outgoing wait arc ──────────────────────
            wait_arc = self.wait_arc_by_start.get(node)
            if wait_arc:
                next_node = wait_arc.end
                wait_dur  = next_node.time - node.time
                if wait_dur >= MIN_REST:
                    # Sufficient rest: duty counter and period clock both reset
                    new_duty         = 0
                    new_period_start = next_node.time
                else:
                    # Still within the same duty period
                    new_duty         = duty_here
                    new_period_start = period_start

                existing = self.min_duty_at.get(next_node, MAX_DUTY_MINUTES + 1)
                if new_duty < existing:
                    self.min_duty_at[next_node]          = new_duty
                    self.duty_period_start_at[next_node] = new_period_start

            # ── Build flight arcs departing from this node ────────────────────
            for f in flights_departing.get((node.airport, node.time), []):
                arr_node = self._find_node_at_or_after(f.dest, f.arr_min)
                if arr_node is None:
                    n_missing += 1
                    continue

                duty_after = duty_here + f.duration
                if duty_after > MAX_DUTY_MINUTES:
                    n_pruned_duty += 1
                    continue

                if f.arr_min - period_start > MAX_DUTY_DAYS * 1440:
                    n_pruned_days += 1
                    continue

                # Propagate duty forward to arrival node (keep minimum)
                existing = self.min_duty_at.get(arr_node, MAX_DUTY_MINUTES + 1)
                if duty_after < existing:
                    self.min_duty_at[arr_node]          = duty_after
                    self.duty_period_start_at[arr_node] = period_start

                self._make_arc(node, arr_node, f.arr_min,
                               f.duration * COST_FLIGHT_HOUR, 'flight', f.id)
                n_arcs += 1

        if n_missing:
            print(f"  WARNING: {n_missing} flights have no connectable arc!")
        if n_pruned_duty:
            print(f"  Duty-pruned: {n_pruned_duty} arcs exceeded MAX_DUTY_MINUTES "
                  f"({MAX_DUTY_MINUTES//60}h since last {MIN_REST//60}h rest)")
        if n_pruned_days:
            print(f"  Days-pruned: {n_pruned_days} arcs exceeded MAX_DUTY_DAYS "
                  f"({MAX_DUTY_DAYS} days)")
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

        for base in self.airports:
            reach = reachable[base]
            for arc in flight_arcs:
                if arc.id in reach:
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
            depot   = Node(airport=base, time=DEPOT_TIME_START)
            horizon = Node(airport=base, time=self.horizon_end)
            self.flow_constrs[base] = {}

            outs = out_terms[base]
            ins  = in_terms[base]

            for node in node_list:
                out_expr = gp.quicksum(outs.get(node, []))
                in_expr  = gp.quicksum(ins.get(node, []))

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

    def solve(self) -> dict:
        """
        Solve directly as MIP. No iterative refinement needed — the network
        already has a node at every exact flight departure and arrival time.
        """
        print("\n=== Solving MIP ===")
        self._ensure_attrs()

        # Switch all variables to integer
        for d in (self.var_work, self.var_dh, self.var_wait):
            for var in d.values():
                var.VType = GRB.INTEGER
        for var in self.slack_var.values():
            var.VType = GRB.INTEGER

        obj = (
            gp.quicksum(arc.cost * v for (base, arc), v in self.var_work.items())
            + gp.quicksum(arc.cost * v for (base, arc), v in self.var_dh.items())
            + gp.quicksum(arc.cost * v for (base, arc), v in self.var_wait.items())
            + gp.quicksum(COST_UNCOVERED * v for v in self.slack_var.values())
        )
        self.model.setObjective(obj, GRB.MINIMIZE)

        self.model.setParam("OutputFlag", 1)
        self.model.setParam("MIPGap", 0.01)
        # self.model.setParam("Threads", 0)          # all cores for presolve + B&B
        self.model.setParam("Method", 2)
        self.model.setParam("Presolve", 2)
        self.model.setParam("MIPFocus", 1)
        self.model.setParam("Heuristics", 0.3)
        self.model.setParam("DisplayInterval", 30)   # print every 30s
        self.model.setParam("TimeLimit", 7200)        # 2hr wall clock; takes best incumbent
        # self.model.setParam("CutPasses", 3)           # max 3 cut rounds at root (default: unlimited)
        self.model.setParam("Cuts", 1)                # moderate cuts — aggressive causes degeneracy on network flow
        self.model.setParam("GomoryPasses", 0)        # disable Gomory cuts — main culprit for stuck node 0
        # self.model.setParam("NodeMethod", 1)          # dual simplex at B&B nodes — faster for network structure
        self.model.setParam("Crossover", 1)
        self.model.setParam("NodefileStart", 4)
        self.model.setParam("Threads", 8)
        self.model.optimize()

        return self.extract_solution()

    # ── Solution extraction ───────────────────

    def extract_solution(self) -> dict:
        eps = 1e-4
        if self.model.Status not in (GRB.OPTIMAL, GRB.TIME_LIMIT, GRB.SUBOPTIMAL):
            return {"status": "infeasible", "cost": None, "routes": [],
                    "uncovered_flights": [], "uncovered_slots": 0}

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

        For each base b:
          Build a residual flow graph with integer capacities from solution.
          Repeatedly trace paths from depot(b) to horizon(b), each consuming
          1 unit of flow. Each path becomes one crew member's route.
          Assign named crew IDs from this base's crew pool.
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

        for base in self.airports:
            depot   = Node(airport=base, time=DEPOT_TIME_START)
            horizon = Node(airport=base, time=self.horizon_end)
            supply  = self.base_crew[base]

            base_crew_ids = [c.id for c in self.crew if c.base == base]
            crew_idx = 0

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
                    leg_type = 'flight' if (base, arc.id) in arc_is_work_for_base else 'deadhead'
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
        Prefers flight > deadhead > wait arcs for meaningful pairings.
        """
        type_priority = {'flight': 0, 'deadhead': 1, 'wait': 2}

        parent: dict[Node, Arc | None] = {source: None}
        stack = [source]

        while stack:
            node = stack.pop()

            if node == sink:
                path: list[Arc] = []
                cur = sink
                while parent[cur] is not None:
                    arc = parent[cur]
                    path.append(arc)
                    cur = arc.start
                path.reverse()
                return path

            candidates = [
                a for a in self.arcs_from.get(node, [])
                if residual.get(a, 0) > 0 and a.end not in parent
            ]
            candidates.sort(key=lambda a: (type_priority.get(a.arc_type, 9), a.end.time))

            for arc in reversed(candidates):
                parent[arc.end] = arc
                stack.append(arc.end)

        return None


# ─────────────────────────────────────────────
# RESULT SERIALISATION
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
    return f"{base}_d{days}_r{RETURN_WINDOW}_rest{MIN_REST//60}_duty{MAX_DUTY_MINUTES//60}_days{MAX_DUTY_DAYS}_flow_network.pkl"


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
    result = net.solve()

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

    for i, route in enumerate(result['routes']):
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
    save_result(result, net, out_path=result_path)

    return result, net


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "data/flights_enriched.csv"
    main(path, days=DAYS_TO_SOLVE)