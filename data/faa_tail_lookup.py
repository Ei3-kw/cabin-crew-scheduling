# -*- coding: utf-8 -*-
"""
FAA Tail Number Cross-Reference
================================
Enriches a flight dataset (BTS format) with aircraft type/model
from the FAA Aircraft Registry, then resolves seat count and
minimum cabin crew per 14 CFR 121.391.

Seat count resolution priority:
  1. FAA registry NUM_SEATS  (per-tail registered value, most accurate)
  2. TCDS model fallback      (FAA Type Certificate Data Sheets / BTS Form 41 B43)

Two modes:
  --mode bulk    Download full FAA registry CSV and join locally (recommended)
  --mode scrape  Scrape registry.faa.gov per unique tail number (small datasets)

Usage:
  python3 faa_tail_lookup.py --input T_ONTIME_MARKETING.csv --output flights_enriched.csv --mode bulk
  python3 faa_tail_lookup.py --input T_ONTIME_MARKETING.csv --output flights_enriched.csv --mode scrape
"""

import argparse
import time
import zipfile
import io
import os
import requests
import pandas as pd
from bs4 import BeautifulSoup

# -- Constants -----------------------------------------------------------------

FAA_REGISTRY_ZIP_URL = (
    "https://registry.faa.gov/database/ReleasableAircraft.zip"
)

FAA_LOOKUP_URL = (
    "https://registry.faa.gov/AircraftInquiry/Search/NNumberResult"
)

# -- TCDS model-level seat fallback --------------------------------------------
#
# Used only when FAA registry NUM_SEATS is missing for a tail number.
# Seat counts are typical US domestic configurations derived from:
#   - FAA Type Certificate Data Sheets (TCDS): https://rgl.faa.gov
#   - BTS Form 41 Schedule B43 (aircraft configuration filings)
#
# The FAA registry uses specific variant codes (e.g. "737-823", "737-8H4")
# rather than generic family names, so we use regex patterns to map
# FAA codes -> aircraft family -> typical seat count.
#
# Pattern order matters: more specific patterns must come before broader ones.

import re

# Each entry: (compiled regex pattern, typical seats)
# Patterns match against the FAA AC_MODEL string (uppercased).
TCDS_PATTERNS = [
    # ── Boeing 737 ──────────────────────────────────────────────────────────
    # 737 MAX variants: model codes contain "8200", "7 MAX", "8 MAX" or
    # suffix like "-8200". MAX 8 = 737-8 (without further suffix like 23/H4)
    (re.compile(r"737-8200"),                   186),  # MAX 10 (high-density)
    (re.compile(r"737-8\s*(MAX)?$"),            172),  # MAX 8 bare
    (re.compile(r"737-9\s*(MAX)?$"),            178),  # MAX 9 bare
    (re.compile(r"737-9GPER"),                  178),  # MAX 9 (United variant)
    # Classic 737-700 family (7xx suffix codes)
    (re.compile(r"737-7"),                      128),
    # Classic 737-800 family (8xx suffix codes, not MAX)
    (re.compile(r"737-8"),                      162),
    # Classic 737-900 family
    (re.compile(r"737-9"),                      178),
    # 737-990ER / 737-900ER
    (re.compile(r"737-990|737-900ER"),          178),
    # Generic 737-700/800/900 labels
    (re.compile(r"737-700"),                    128),
    (re.compile(r"737-800"),                    162),
    (re.compile(r"737-900"),                    178),
    # ── Boeing 757 ──────────────────────────────────────────────────────────
    (re.compile(r"757-3"),                      243),  # 757-300 family
    (re.compile(r"757-2"),                      199),  # 757-200 family
    # ── Boeing 767 ──────────────────────────────────────────────────────────
    (re.compile(r"767-4"),                      245),  # 767-400 family
    (re.compile(r"767-3"),                      218),  # 767-300 family
    # ── Boeing 777 ──────────────────────────────────────────────────────────
    (re.compile(r"777-3"),                      386),  # 777-300 family
    (re.compile(r"777-2"),                      305),  # 777-200 family
    # ── Boeing 787 ──────────────────────────────────────────────────────────
    (re.compile(r"787-10"),                     330),
    (re.compile(r"787-9"),                      285),
    (re.compile(r"787-8"),                      234),
    # ── Boeing 717 ──────────────────────────────────────────────────────────
    (re.compile(r"717-2"),                      117),
    # ── Airbus A319 ─────────────────────────────────────────────────────────
    (re.compile(r"A319"),                       128),
    # ── Airbus A320 (NEO variants have 2[5-9]x suffix) ──────────────────────
    (re.compile(r"A320-2[5-9]"),               165),  # A320neo family
    (re.compile(r"A320"),                       150),  # A320ceo family
    # ── Airbus A321 (NEO: 2[5-9]x or 271NX) ────────────────────────────────
    (re.compile(r"A321-2[5-9]|A321-271N"),     196),  # A321neo/XLR
    (re.compile(r"A321"),                       185),  # A321ceo
    # ── Airbus A220 ─────────────────────────────────────────────────────────
    (re.compile(r"BD-500-1A11"),               130),  # A220-300 (CS300)
    (re.compile(r"BD-500-1A10"),                99),  # A220-100 (CS100)
    # ── Airbus A330 ─────────────────────────────────────────────────────────
    (re.compile(r"A330-9"),                    260),  # A330neo
    (re.compile(r"A330-3"),                    277),  # A330-300
    (re.compile(r"A330-2"),                    247),  # A330-200
    # ── Airbus A350 ─────────────────────────────────────────────────────────
    (re.compile(r"A350-10"),                   369),
    (re.compile(r"A350-9"),                    306),
    # ── Embraer E-jets ──────────────────────────────────────────────────────
    # FAA codes: ERJ 170-200 LR/LL = E175; ERJ 170-100 = E170
    # ERJ 190-100 = E190; EMB-145 = ERJ-145 (50 seats)
    (re.compile(r"ERJ\s*170-200|ERJ\s*175"),    76),  # E175
    (re.compile(r"ERJ\s*170-100|ERJ\s*170"),    70),  # E170
    (re.compile(r"ERJ\s*190-100"),              96),  # E190
    (re.compile(r"ERJ\s*190-200"),             114),  # E195
    (re.compile(r"EMB-145"),                    50),  # ERJ-145
    (re.compile(r"EMB-135"),                    37),  # ERJ-135
    # ── Bombardier CRJ / CL-600 ─────────────────────────────────────────────
    # FAA codes: CL-600-2C10 = CRJ-700; CL-600-2D24 = CRJ-900;
    #            CL-600-2C11 = CRJ-550 (70 seats); CL-600-2B19 = CRJ-200
    (re.compile(r"CL-600-2D24"),               76),  # CRJ-900
    (re.compile(r"CL-600-2C10"),               70),  # CRJ-700
    (re.compile(r"CL-600-2C11"),               70),  # CRJ-550 (70 seats, 50 sold)
    (re.compile(r"CL-600-2B19"),               50),  # CRJ-200
    (re.compile(r"CRJ-900"),                   76),
    (re.compile(r"CRJ-700"),                   70),
    (re.compile(r"CRJ-200"),                   50),
    (re.compile(r"CRJ-1000"),                 100),
    # ── ATR ─────────────────────────────────────────────────────────────────
    (re.compile(r"ATR.?72"),                   70),
    (re.compile(r"ATR.?42"),                   48),
    # ── De Havilland / Dash 8 ───────────────────────────────────────────────
    (re.compile(r"DHC-8-4"),                   78),
    (re.compile(r"DHC-8-3"),                   50),
]


def get_seats(row):
    """
    Return seat count for a flight row.
    Priority: (1) FAA registry NUM_SEATS, (2) TCDS regex pattern fallback.
    """
    # 1. FAA registry per-tail value
    try:
        seats = int(float(str(row.get("NUM_SEATS", "")).strip()))
        if seats > 0:
            return seats
    except (ValueError, TypeError):
        pass

    # 2. Regex pattern match on AC_MODEL
    model = str(row.get("AC_MODEL", "")).strip().upper()
    if model:
        for pattern, seats in TCDS_PATTERNS:
            if pattern.search(model):
                return seats

    return None


# -- Cabin crew helper ---------------------------------------------------------
#
# Minimum cabin crew per 14 CFR 121.391 (regulatory floor).
# Reference: https://www.ecfr.gov/current/title-14/section-121.391
#
# Seat range    Min FAs
# ----------    -------
# 1 - 50        1
# 51 - 100      2
# 101 - 150     3
# 151 - 200     4
# 201 - 250     5
# 251+          2 + ceil((seats - 100) / 50)
#
# Airlines frequently staff above this minimum based on service standards,
# route length, and collective bargaining agreements.

def estimate_min_cabin_crew(seats):
    """
    Minimum cabin crew per FAA 14 CFR 121.391.
    Input: seat count (int or str). Returns int or None.
    """
    try:
        seats = int(float(str(seats).strip()))
        if seats < 1:
            return None
    except (ValueError, TypeError):
        return None

    if seats <= 50:
        return 1
    elif seats <= 100:
        return 2
    else:
        # ceiling division: 2 + ceil((seats - 100) / 50)
        return 2 + (-(-(seats - 100) // 50))


# -- Bulk mode -----------------------------------------------------------------

def download_faa_registry(cache_path="faa_registry.csv"):
    """Download and parse the FAA releasable aircraft database."""

    if os.path.exists(cache_path):
        print(f"[bulk] Using cached registry at {cache_path}")
        return pd.read_csv(cache_path, dtype=str, low_memory=False)

    print("[bulk] Downloading FAA registry (~60 MB)...")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": (
            "https://www.faa.gov/licenses_certificates/aircraft_certification"
            "/aircraft_registry/releasable_aircraft_download"
        ),
    }
    r = requests.get(FAA_REGISTRY_ZIP_URL, timeout=120, headers=headers)
    r.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        master  = pd.read_csv(z.open("MASTER.txt"),  dtype=str, low_memory=False)
        acftref = pd.read_csv(z.open("ACFTREF.txt"), dtype=str, low_memory=False)

    master.columns  = master.columns.str.strip()
    acftref.columns = acftref.columns.str.strip()

    merged = master.merge(
        acftref[["CODE", "MFR", "MODEL"]],
        left_on="MFR MDL CODE",
        right_on="CODE",
        how="left",
    )

    keep = {
        "N-NUMBER":      "N_NUMBER",
        "MFR MDL CODE":  "MFR_MDL_CODE",
        "MFR":           "MANUFACTURER",
        "MODEL":         "AC_MODEL",
        "TYPE AIRCRAFT": "TYPE_AIRCRAFT",
        "NO-ENG":        "NUM_ENGINES",
        "NO-SEATS":      "NUM_SEATS",
        "YEAR MFR":      "YEAR_MFR",
    }
    out = merged[[c for c in keep if c in merged.columns]].rename(columns=keep)
    out["N_NUMBER"] = out["N_NUMBER"].str.strip()

    out.to_csv(cache_path, index=False)
    print(f"[bulk] Registry saved to {cache_path} ({len(out):,} aircraft)")
    return out


def enrich_bulk(flights):
    registry = download_faa_registry()

    flights["_N_NUM"] = (
        flights["TAIL_NUM"]
        .str.strip()
        .str.upper()
        .str.lstrip("N")
    )

    enriched = flights.merge(
        registry,
        left_on="_N_NUM",
        right_on="N_NUMBER",
        how="left",
    ).drop(columns=["_N_NUM", "N_NUMBER"])

    matched = enriched["MANUFACTURER"].notna().sum()
    print(
        f"[bulk] Matched {matched:,} / {len(enriched):,} flights "
        f"({matched / len(enriched) * 100:.1f}%)"
    )
    return enriched


# -- Scrape mode ---------------------------------------------------------------

def scrape_tail(n_number):
    """
    Scrape a single N-number from the FAA registry website.
    n_number should include the leading 'N', e.g. 'N104NN'.
    """
    query = n_number.strip().upper().lstrip("N")
    try:
        r = requests.get(
            FAA_LOOKUP_URL,
            params={"nNumberTxt": query},
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (research script)"},
        )
        r.raise_for_status()
    except requests.RequestException as e:
        return {"TAIL_NUM": n_number, "error": str(e)}

    soup = BeautifulSoup(r.text, "html.parser")
    result = {"TAIL_NUM": n_number}

    field_map = {
        "Manufacturer Name": "MANUFACTURER",
        "Model":             "AC_MODEL",
        "Aircraft Type":     "TYPE_AIRCRAFT",
        "Number of Engines": "NUM_ENGINES",
        "Number of Seats":   "NUM_SEATS",
        "Year Manufactured": "YEAR_MFR",
        "Serial Number":     "SERIAL_NUM",
    }

    for row in soup.select("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) >= 2:
            label = cells[0].get_text(strip=True)
            value = cells[1].get_text(strip=True)
            if label in field_map:
                result[field_map[label]] = value

    return result


def enrich_scrape(flights, delay=0.5):
    unique_tails = flights["TAIL_NUM"].dropna().str.strip().str.upper().unique()
    print(f"[scrape] Looking up {len(unique_tails)} unique tail numbers...")

    records = []
    for i, tail in enumerate(unique_tails, 1):
        print(f"  [{i}/{len(unique_tails)}] {tail}", end="\r")
        records.append(scrape_tail(tail))
        time.sleep(delay)

    print()
    lookup_df = pd.DataFrame(records)
    lookup_df["TAIL_NUM"] = lookup_df["TAIL_NUM"].str.strip().str.upper()

    flights["_TAIL_CLEAN"] = flights["TAIL_NUM"].str.strip().str.upper()
    enriched = flights.merge(
        lookup_df,
        left_on="_TAIL_CLEAN",
        right_on="TAIL_NUM",
        how="left",
        suffixes=("", "_faa"),
    ).drop(columns=["_TAIL_CLEAN"])

    matched = enriched["MANUFACTURER"].notna().sum()
    print(
        f"[scrape] Matched {matched:,} / {len(enriched):,} flights "
        f"({matched / len(enriched) * 100:.1f}%)"
    )
    return enriched


# -- Main ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Enrich BTS flight data with FAA aircraft type, seats, and min cabin crew."
    )
    parser.add_argument("--input",  required=True, help="Input CSV (BTS format)")
    parser.add_argument("--output", required=True, help="Output enriched CSV")
    parser.add_argument(
        "--mode",
        choices=["bulk", "scrape"],
        default="bulk",
        help="bulk = download full FAA registry (recommended); scrape = per-tail HTTP lookup",
    )
    args = parser.parse_args()

    print(f"[main] Reading {args.input}...")
    flights = pd.read_csv(args.input, dtype=str, low_memory=False)
    print(f"[main] {len(flights):,} rows loaded.")

    # Step 1: enrich with FAA registry (manufacturer, model, seats, etc.)
    if args.mode == "bulk":
        enriched = enrich_bulk(flights)
    else:
        enriched = enrich_scrape(flights)

    # Step 2: resolve seat count (FAA registry -> TCDS fallback)
    print("[main] Resolving seat counts...")

    # Diagnostic: show what columns came back from the registry
    print(f"[debug] Columns after FAA enrichment: {list(enriched.columns)}")
    if "NUM_SEATS" in enriched.columns:
        sample = enriched["NUM_SEATS"].dropna().head(5).tolist()
        print(f"[debug] NUM_SEATS sample values: {sample}")
        print(f"[debug] NUM_SEATS non-null count: {enriched['NUM_SEATS'].notna().sum():,}")
    else:
        print("[debug] NUM_SEATS column NOT present in enriched data")
    if "AC_MODEL" in enriched.columns:
        sample = enriched["AC_MODEL"].dropna().head(5).tolist()
        print(f"[debug] AC_MODEL sample values: {sample}")

    # Diagnostic: warn if registry match rate is low (likely download failure)
    if "MANUFACTURER" in enriched.columns:
        match_rate = enriched["MANUFACTURER"].notna().mean()
        if match_rate < 0.1:
            print(
                f"[warn] Only {match_rate*100:.1f}% of flights matched the FAA registry.\n"
                f"       The registry download may have failed. Seat data will fall back\n"
                f"       to the TCDS model lookup table where possible."
            )

    enriched["SEATS_RESOLVED"] = enriched.apply(get_seats, axis=1)

    faa_seats      = pd.to_numeric(
        enriched.get("NUM_SEATS", pd.Series(dtype=str)), errors="coerce"
    ).gt(0).sum()
    total_resolved = enriched["SEATS_RESOLVED"].notna().sum()
    tcds_seats     = max(total_resolved - faa_seats, 0)
    no_seats       = enriched["SEATS_RESOLVED"].isna().sum()

    print(
        f"[main] Seat source breakdown:\n"
        f"         FAA registry (per tail) : {faa_seats:,} flights\n"
        f"         TCDS fallback (by model): {tcds_seats:,} flights\n"
        f"         No seat data found      : {no_seats:,} flights"
    )

    # Step 3: minimum cabin crew per 14 CFR 121.391
    enriched["MIN_CABIN_CREW"] = enriched["SEATS_RESOLVED"].apply(
        estimate_min_cabin_crew
    )
    print(
        "[main] MIN_CABIN_CREW added "
        "(14 CFR 121.391: 1 FA<=50 seats, 2 FA<=100, +1 per 50 above 100)"
    )

    enriched.to_csv(args.output, index=False)
    print(f"\n[main] Saved to {args.output}")

    # Summary report: one table grouped by aircraft model
    print("\n" + "=" * 75)
    print("AIRCRAFT TYPE SUMMARY")
    print("=" * 75)

    missing = [c for c in ["AC_MODEL", "SEATS_RESOLVED", "MIN_CABIN_CREW"] if c not in enriched.columns]
    if missing:
        print(f"Skipping summary table -- missing columns: {missing}")
        print("This usually means the FAA registry download failed.")
        print("Try downloading manually from:")
        print("  https://www.faa.gov/licenses_certificates/aircraft_certification/aircraft_registry/releasable_aircraft_download")
        print("Unzip it into the same folder as the script, then re-run.")
    else:
        enriched["_seats"] = pd.to_numeric(enriched["SEATS_RESOLVED"], errors="coerce")
        enriched["_crew"]  = pd.to_numeric(enriched["MIN_CABIN_CREW"],  errors="coerce")

        summary = (
            enriched.groupby("AC_MODEL", dropna=False)
            .agg(
                num_flights    = ("AC_MODEL", "size"),
                seats          = ("_seats",   "median"),
                min_cabin_crew = ("_crew",    "median"),
            )
            .reset_index()
            .sort_values("num_flights", ascending=False)
        )

        summary["seats"] = summary["seats"].apply(
            lambda x: int(x) if pd.notna(x) else "n/a"
        )
        summary["min_cabin_crew"] = summary["min_cabin_crew"].apply(
            lambda x: int(x) if pd.notna(x) else "n/a"
        )

        col_widths = [30, 14, 18, 26]
        headers    = ["aircraft_model", "num_flights", "seats (median)", "min_crew (14 CFR 121.391)"]
        divider    = "-" * sum(col_widths)

        print("".join(h.ljust(w) for h, w in zip(headers, col_widths)))
        print(divider)
        for _, row in summary.iterrows():
            print(
                str(row["AC_MODEL"]).ljust(col_widths[0]) +
                str(row["num_flights"]).ljust(col_widths[1]) +
                str(row["seats"]).ljust(col_widths[2]) +
                str(row["min_cabin_crew"]).ljust(col_widths[3])
            )

        enriched.drop(columns=["_seats", "_crew"], inplace=True)

    print("=" * 75)
    print("\nNote: MIN_CABIN_CREW is the regulatory floor (14 CFR 121.391).")
    print("Actual airline staffing is typically higher.")


if __name__ == "__main__":
    main()