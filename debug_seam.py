"""Confirm the across-seam away-clock behaviour for one crew.
Runs ONLY the senior layer (min_crew=1) of G7 for the first CREW_MAX_WINDOWS
windows, tracing CREW_DEBUG_ID's seed/carry clock state at each seam."""
import sys
from dataclasses import replace
import crew_solver as solver

CSV = "data/flights_enriched.csv"
AIRLINE = "G7"
DAYS = 30

fba, _ = solver.parse_flights_by_airline(CSV, DAYS)
flights = fba[AIRLINE]
flights_L1 = [replace(f, min_crew=1) for f in flights]
seniors = solver.size_crew_bases(flights_L1)
print(f"senior pool: {len(seniors)} crew")
solver.solve_airline(f"{AIRLINE}_dbg", flights_L1, DAYS,
                 out_dir="results", crew_override=seniors, verbose=False)
