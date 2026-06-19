# Cabin Crew Scheduling

Honours thesis project (MATH6007, UQ): solving the **cabin-crew pairing problem** on real
U.S. domestic flight data with a **time-expanded crew-flow network**, a rolling-horizon
decomposition, and a two-layer senior/normal crew model.

Given a month of flights — each needing between one and eight cabin crew, exactly one of
whom must be a *senior* — the solver builds legal duty pairings (respecting turnaround,
rest, duty, away-from-base and home-break rules) that cover as many flights as possible at
minimum cost, with crew based at many airports and positioned by flying or deadheading.

---

## Pipeline at a glance

```
BTS on-time CSV
      │  data/faa_tail_lookup.py   (tail → FAA registry → seats → 14 CFR 121.391 min crew)
      ▼
data/flights_enriched.csv
      │  crew_two_layer.py          (orchestrates two layers; calls the solver twice)
      │    └─ crew_solver.py        (time-expanded network + rolling horizon + group-flow MIP)
      ▼
results/result_<AIRLINE>_twolayer.json
      │  results/validate_availability.py   (re-checks every rule on the merged routes)
      │  flight-solver-visualizer/          (interactive map/globe of the schedule)
      ▼
report/  ·  talk_final/             (Typst write-up and slides)
```

## Core components

| Path | Role |
|------|------|
| `crew_solver.py` | **The solver.** Time-expanded crew-flow network with break-clock node expansion, clock-group aggregation, integer group-flow MIP (Gurobi), and flow decomposition back into per-crew routes. Rolling horizon over the planning period. |
| `crew_two_layer.py` | **Orchestrator** for multi-crew flights. Layer 1 places one senior per flight (`min_crew = 1`); flights with no senior are cancelled. Layer 2 fills the remaining `min_crew − 1` seats with normal crew. Idle seniors can substitute into normal seats. |
| `data/faa_tail_lookup.py` | Enriches raw BTS flight data with aircraft type and seat count (FAA Aircraft Registry), then maps seats → minimum cabin crew via 14 CFR 121.391 (`ceil(seats / 50)`). |
| `results/validate_availability.py` | Independent validator: reconstructs each crew's physical timeline from the result JSON and flags continuity, overlap, turnaround, away-cap (`d_away`) and consecutive-duty (`duty_block`) violations. |
| `compute_cost.py` | Costs a two-layer result under a senior/normal per-minute labour-rate model (flying, deadhead, waiting) plus an uncovered-slot penalty. Rates are constants at the top of the file. |
| `flight-solver-visualizer/` | SvelteKit + deck.gl visualizer (its own README). Loads a result JSON and shows, at any instant, airborne flights, crew positions, and active breaks. |
| `report/` , `talk_final/` | Typst thesis (`report/draft.typ`) and presentation. |
| `archive/` | Superseded solver versions and abandoned approaches (CP, column generation, DIDP, earlier DDD/flow iterations). Git-ignored; kept locally for reference. |

## Model rules (constants in `crew_solver.py`)

| Rule | Value |
|------|-------|
| Turnaround between flights (same airport) | 45 min |
| Minimum rest before next duty | 8 h |
| Maximum on-duty span per day | 14 h |
| Mandatory home break | 48 h |
| Maximum time away from base | 4 days |
| Maximum consecutive duty days | 3 |

Rolling horizon: `T_solve = 7 d` solved per window, `T_commit = 3 d` committed each step,
`T_tail = 4 d` return tail (`= D_AWAY`) → an 11-day window. Crew positions and break-clock
state are carried across the seam between windows.

## Requirements

- Python 3.10+ with `gurobipy` (a licensed Gurobi install)
- For the visualizer: [Bun](https://bun.sh) (see `flight-solver-visualizer/README.md`)

## Usage

**1. Enrich raw BTS data** (once; output is git-ignored):

```sh
python3 data/faa_tail_lookup.py --input data/T_ONTIME_MARKETING.csv \
                                --output data/flights_enriched.csv --mode bulk
```

**2. Solve a single airline (two-layer senior + normal):**

```sh
# args: <flights csv> <planning days> <airline code or list number>
python3 crew_two_layer.py data/flights_enriched.csv 30 G7
```

The two instances analysed in the report (see [Notes](#notes)) are produced by:

```sh
# G7 — real enriched data, uniform 2 crew per flight (real-data case)
python3 crew_two_layer.py data/flights_enriched.csv 30 G7

# ZW — random heterogeneous demand, min_crew ∈ [1,8] (mixed-demand case)
python3 crew_two_layer.py data/flights_2025-01-random.csv 30 ZW
```

Running without the airline argument prints the carriers in the file and prompts for one.
Each two-layer run also caches its per-layer results as `results/result_<AIRLINE>_L1senior.json`
and `..._L2normal.json`.

**2b. Re-combine from cached layers** (no re-solve): rebuild the merged
`result_<AIRLINE>_twolayer.json` from the cached layer files — useful after a fix to the
combine step. The `<csv>` and `<days>` must match the original run so the flight geometry
lines up (a mismatch trips a loud guard rather than silently producing a bad file):

```sh
# args: recombine <csv> <planning days> <airline> [suffix]
python3 crew_two_layer.py recombine data/flights_2025-01-random.csv 30 ZW
```

**3. Solve the single-commodity model directly** (treats each flight's `min_crew` as-is):

```sh
python3 crew_solver.py data/flights_enriched.csv 30 G7
```

**4. Validate a result:**

```sh
python3 results/validate_availability.py results/result_G7_twolayer.json
```

**5. Cost a result** under a senior/normal labour-rate model:

```sh
python3 compute_cost.py results/result_ZW_twolayer.json
```

Reports flying / deadhead / waiting cost split by senior vs normal, plus an uncovered-slot
penalty. Rates (senior 420 $/min fly, normal 100; waiting 1.0 / 0.5; uncovered slots at
2× the senior-seat cost) are constants at the top of the script.

**6. Visualize:**

```sh
cd flight-solver-visualizer && bun install && bun run dev
```

## Environment knobs (`crew_solver.py`)

| Variable | Effect |
|----------|--------|
| `CREW_MAX_WINDOWS=N` | Stop after the first `N` rolling-horizon windows (debugging / fast iteration). |
| `CREW_DEBUG_ID=<crew_id>` | Trace one crew's seed/carry break-clock state across windows. |
| `CREW_PROBE=<seconds>` | Adaptive crossover (opt-in): probe each window with no crossover up to this many seconds; if it times out still far from optimal, re-solve with crossover + `MIPFocus=1`. Helps the hard multi-crew windows; off by default. |

The Gurobi `TimeLimit` (1800 s) and `Method` (barrier, `Crossover=0`) are set in `build_model`.

## Data files (`data/`)

| File | Description |
|------|-------------|
| `flights_enriched.csv` | Real BTS schedule enriched with seats + min crew (git-ignored; regenerate with step 1). |
| `flights_2025-01-random.csv` | Same network as the enriched data but with a random `min_crew ∈ [1,8]` per flight — the heterogeneous-demand instance (used for ZW). Generate via `faa_tail_lookup.py --random-min-crew`. |
| `flights_2025-01.csv` | January slice with the real seat-derived `min_crew`. |
| `flights_mini.csv`, `flights_42k.csv` | Tiny and large slices for quick or stress runs. |
| `faa_registry.csv` | Cached FAA Aircraft Registry (tail → type → seats). |

## Notes

- A flight is **cancelled** if it cannot be given a senior, and **understaffed** if it is
  short only on normal seats — the senior class is the binding resource.
- Coverage is an **equality** (`Σ flow + slack = r_f`): the slack absorbs any shortfall and
  the equality caps operators at `r_f`, so the flow can't over-cover a flight by routing
  surplus crew through it — they reroute onto deadhead arcs instead.
- All duty rules are preserved by construction: every decomposed crew path lives in the
  break-expanded graph, so they hold without a post-hoc re-check. `validate_availability.py`
  confirms this on the merged, cross-window output.
- **Test instances**: **G7** on the real enriched data (a tractable regional, uniform 2 crew
  per flight) is the real-data case; **ZW** on `flights_2025-01-random.csv` (random
  `min_crew ∈ [1,8]`) is the heterogeneous-demand case. Real per-flight variation only
  appears at mainline scale, which is too large to solve in reasonable time.
