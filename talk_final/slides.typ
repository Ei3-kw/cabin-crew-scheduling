#import "@preview/touying:0.5.5": *
#import themes.university: *
#import "@preview/cetz:0.3.1"
#import "@preview/fletcher:0.5.3" as fletcher: node, edge
#import "@preview/ctheorems:1.1.3": *
#import "@preview/numbly:0.1.0": numbly

#let cetz-canvas = touying-reducer.with(reduce: cetz.canvas, cover: cetz.draw.hide.with(bounds: true))
#let fletcher-diagram = touying-reducer.with(reduce: fletcher.diagram, cover: fletcher.hide)

#show raw.where(block: true): it => text(size: 0.6em, it)
#show: thmrules.with(qed-symbol: $square$)
#let theorem   = thmbox("theorem", "Theorem", fill: rgb("#eeffee"))
#let corollary = thmplain("corollary", "Corollary", base: "theorem", titlefmt: strong)
#let definition = thmbox("definition", "Definition", inset: (x: 1.2em, top: 1em))
#let example   = thmplain("example", "Example").with(numbering: none)
#let proof     = thmproof("proof", "Proof")

#show: university-theme.with(
  aspect-ratio: "16-9",
  config-info(
    title: [Cabin Crew Pairing],
    subtitle: [Network Flow · DDD · Individual Assignment],
    author: [Ella],
    date: datetime.today(),
    institution: [],
  ),
)

#title-slide()

== Outline <touying:hidden>

// ─────────────────────────────────────────────────────────────
// SECTION 1 — PROBLEM
// ─────────────────────────────────────────────────────────────
= Problem

== Problem Overview

#table(
  columns: (auto, 1fr),
  stroke: none,
  inset: (x: 10pt, y: 10pt),
  [*Objective*],      [Minimise total cost of cabin crew scheduling],
  [*Coverage*],       [Every flight must be covered],
  [#pause],
  [*Home-return*],    [All crew start and end at their home base airport],
  [*Deadhead*],       [Crew may ride as a passenger to reposition],
  [*Layover*],        [Crew wait at airports between flights],
  [*Rests*],          [Mandatory overnight breaks & home time],
  [#pause],
  [*Methods*],        [MICCPP-ACCS — paper approach (Wen et al. 2022)\
  #text(fill: rgb("#00aacc"))[aggregate flow (LP/MIP)]\ #text(fill: rgb("#e87c2b"))[DDD with individual assignment]],
)

= OG

== MICCPP-ACCS — Wen et al. 2022

#v(0.4cm)

#grid(
  columns: (1fr),
  rows: (auto, auto),
  gutter: 1.2em,

  [
    *Model*
    #v(0.2cm)
    - Crew scheduled individually, not as teams
    - Each class (Senior / Junior / Trainee) has its own availability cap
    - Controlled crew substitution (CCS) covers shortfalls across classes
    - Big-M penalty for extra crew; substitutions penalised ($mu << M$)
  ],

  [
    *Solution — CG + MIP*
    #v(0.2cm)
    - Solve LP relaxation of master problem (Simplex)
    - Pricing problem generates columns via resource-constrained shortest path
    - Iterate until no negative reduced-cost column exists
    - Apply MIP on final column set to recover integer solution
    - Heuristic — genetic algorithm used for large instances
  ],

  [
    *Network*
    #v(0.2cm)
    - Duty-based: flights $arrow$ duties $arrow$ pairings
    - One network per crew class (identical structure)
    - All pairings start and end at home base
    - Feasibility rules enforced during construction
  ],

  [
    *Planning scope*
    #v(0.2cm)
    - Weekly horizon, Single base
  ],
)

==
```
════════════════════════════════════════════════════════════
  MODEL 2 : MICCPP-ACCS (Multi-Class + Controlled Crew Substitution)
════════════════════════════════════════════════════════════
  Solver status      : Optimal
  MIP gap            : 0.00%
  ── Workforce summary ──
  Class 0 (Senior ):  avail_cap=   5  used=    5  extra=    0  subs=    0
  Class 1 (Junior ):  avail_cap=   7  used=    7  extra=    0  subs=    0
  Class 2 (Trainee):  avail_cap=   5  used=    5  extra=    0  subs=    0

  Total assignments (available crew) : 17
  Total substitutions (CCS events)   : 0
  Total extra-crew assignments       : 0

  No cross-class substitutions required.

  MS (with CCS)      = 17  (total crew needed across all flights)
  TA (total avail)   = 17
```

// == Two Modelling Approaches

// #v(0.5cm)

// #table(
//   columns: (auto, 1fr, 1fr),
//   stroke: 0.5pt,
//   inset: (x: 10pt, y: 12pt),
//   [], [#text(fill: rgb("#00aacc"))[*Aggregate Flow*]], [#text(fill: rgb("#e87c2b"))[*DDD — Individual*]],
//   [*Variables*],
//     [Integer flows $f^"work"_(b,a)$, $f^"dh"_(b,a)$, $f^"wt"_(b,a)$ per base],
//     [Binary arc indicators $x_{c,a} in {0,1}$ per crew member],
//   [*Flow balance*],
//     [Aggregate supply $n_b$ routed from base depots],
//     [One unit per crew member; balance enforced individually],
//   [*Coverage*],
//     [$sum_b f^"work"_(b,a) >= m_f$],
//     [$sum_c x_{c,a} >= m_f$],
//   [*Route extraction*],
//     [Stage 2 flow decomposition required],
//     [Direct individual tracing — no decomposition needed],
//   [*Network*],
//     [Exact times; rolling horizon],
//     [Initial bucket = 15 min; refined iteratively by DDD],
// )

// TODO - REPLACE ME WITH FIGMA DIAGRAMS
// ─────────────────────────────────────────────────────────────
// SECTION 2 — SETS & DATA
// ─────────────────────────────────────────────────────────────
= Sets & Data

== Sets

#table(
  columns: (auto, 1fr),
  stroke: none,
  inset: (x: 10pt, y: 10pt),
  $cal(T)$, [Day and time],
  $cal(P)$,                                                     [Set of all airports],
  $cal(B) subset.eq cal(P)$,                                    [Crew home bases],
  $cal(S) subset.eq cal(P) without cal(B)$,                     [Satellite airports
                        ($r <= 250, >=3$ daily flights)],
  $cal(C)$,                                                     [Set of individual crew members],
  $cal(C)_b subset.eq cal(C)$,                                  [Crew members based at $b$],
  $cal(F)$,                                                     [Set of all flights],
  $cal(F)^"cov" subset.eq cal(F)$,                             [Flights requiring crew coverage (planning period only)],
  $cal(W)$,                                                     [Ordered solve windows (rolling horizon)]
)

== Data

#table(
  columns: (auto, auto, 1fr),
  stroke: none,
  inset: (x: 10pt, y: 10pt),
  $d_f$,          [], [Duration of flight $f$ in minutes],
  $"dist"_f$,     [], [Great-circle distance of flight $f$],
  $lambda_f$,     [], [Passenger load factor of flight $f$ $in [0,1]$],
  $m_f$,          [], [Required working crew count for flight $f$],
  $n_b$,          [], [Crew supply at base $b in cal(B)$],
  $n_"min"$,      [5], [Minimum number of crew at a base],

  $n_"max"$,      [40], [Maximum number of crew introduced by noise at a base],
  $pi^w_(b,p)$,   [], [Crew from base $b$ at airport $p$ at window $w$ start; $quad sum_p pi^w_(b,p) = n_b$],
  $c^"work"_a$,   [], [Working crew cost on arc $a in cal(A)^"fl"$],
  $c^"fl"$,       [100 / min], [Cost per minute of flight time worked],
  $c^"dh"_a$,     [20 / min],  [Deadhead cost on arc $a in cal(A)^"dh"$ (base per-diem + opp.cost],
  $c^"wt"$,       [0.5 / min], [Wait cost rate],
  $C^"ov"$,       [500],       [Flat penalty per overnight stay away from base (wait $>= 4$ h)],
  $C^"unc"$,      [$10^7$],    [Penalty per uncovered crew slot (infeasibility proxy)],
  $Delta^"ta"$,   [45 min],    [Minimum turnaround between flights at same airport],
  $Delta^"rest"$, [8 h],       [Minimum rest before next duty period],
  $Delta^"duty"$, [14 h], [Maximum on-duty time per day],
  $Delta^"hb"$,   [48 h],      [Minimum home break],
  $D^"work"$,     [5 days],    [Maximum consecutive days on duty],
  $D^"away"$,     [7 days],    [Return-window: crew must be back at base],
  $T_"hor,days"$, [],          [Planning horizon length in days],
  $tau_"duty"$, [], [Available duty time per crew member in min],
  $Delta_"bucket"$, [15 min], [Initial DDD time-bucket tolerance; arc is a violation if $|t_v' - t^"true"| > Delta_"bucket"$],
)

== Crew Base Sizing

For each base $p in cal(B)$

$
n_p = max lr(( n_"min", ceil(lr(( frac(sum_(f: f."orig" = p) m_f dot d_f, tau_"duty") dot 1.5 )) + "noise" )))
$


#v(0.5cm)

- Each base-count yields that many distinct `CrewMember` objects with unique IDs

// ─────────────────────────────────────────────────────────────
// SECTION 3 — NETWORK BUILDING
// ─────────────────────────────────────────────────────────────
= Network Building

== Time-Expanded Network

#table(
  columns: (auto, 1fr),
  inset: 8pt,
  align: (center + horizon, left),
  table.header([*Symbol*], [*Definition*]),

  $v^("hor",w)_b$,
  [Horizon node for base $b$ in window $w$ $ v^("hor",w)_b = (b, cal(T)^("hor",w)) $],

  $cal(N) subset.eq cal(P) times ZZ_(>=0)$,
  [Time-expanded nodes $(p,t)$ at each dep/arr time],

  $cal(N)^w subset.eq cal(N)$,
  [Nodes active in window $w$  $ cal(N)^w = { (p,t) in cal(N) | t <= cal(T)^("hor",w) } $],
)


// Nodes at *exact* flight departure/arrival times — no time discretisation needed.


+ *Depot & horizon nodes*: $forall p in cal(B) union cal(S)$, add $(p, 0)$ and $(p, T^("hor",w))$
+ *Flight nodes*: $forall f in cal(F)$, add $(f."orig", f."dep")$ and $(f."dest", f."arr")$

#pagebreak()

#table(
  columns: (auto, 1fr),
  inset: 8pt,
  align: (center + horizon, left),
  table.header([*Symbol*], [*Definition*]),

  $cal(A) = cal(A)^"fl" union cal(A)^"dh" union cal(A)^"wt"$,
  [Flight, deadhead, and wait arcs],

  $cal(A)^+_v, cal(A)^-_v subset.eq cal(A)$,
  [Out-arcs and in-arcs of node $v$],

  $cal(K)_b$,
  [Epochs of length $D^"away" times 1440$ min covering the horizon for base $b$],

  $cal(A)^"hb"_(b,k) subset.eq cal(A)^"wt"$,
  [Home-wait arcs at base $b$ in epoch $k$ with cumulative home-wait $>= Delta^"hb"$],
)

1. *Wait arcs*: Crew waits at airport
  - cost = $c^"wt" dot Delta t + C^"ov" dot bb(1)[Delta t >= 4"h"]$
  - chain consecutive nodes at each airport

#pagebreak()
2. *Flight arcs*: Crew works the leg
    - cost = $c^"fl" dot d_f$
    - forward pass in time order
      - snap arrival node
      - check duty limit
      - propagate home-break clocks
#pause
3. *Deadhead arcs*: Crew repositions as passenger
    - cost = $c^"dh" dot d_f + "opp_cost"(f)$

// #pause
// Working and deadheading crew share the same arc both land at $v'$. Depot departures $(p, 0)$ are exempt.


// #align(right)[_No arc = no variable — constraints enforced structurally_]

// == Arc Types

// #table(
//   columns: (auto, 1fr),
//   stroke: none,
//   inset: (x: 10pt, y: 12pt),
//   [`flight`],    [Crew works the leg; cost = $c^"fl" dot d_f$],
//   [`deadhead`],  [Crew repositions as passenger; cost = $c^"dh" dot d_f + "opp_cost"(f)$],
//   [`wait`],      [Crew waits at airport; cost = $c^"wt" dot Delta t + C^"ov" dot bb(1)[Delta t >= 4"h"]$],
//   [`return`],    [Crew travels back to home base at horizon],
// )

// == Opportunity-Cost Deadhead Pricing

// #v(0.5cm)

// Deadhead displaces a revenue passenger. Estimated fare:

// $
// hat(r)(f) = r_0 + r_"mi" dot "dist"_f
// quad (r_0 = 50,; r_"mi" = 0.15)
// $

// Scaling by load factor $lambda_f$:

// $
// alpha(lambda) = cases(
//   0                                    & lambda <= 0.75,
//   frac(lambda - 0.75, 0.90 - 0.75)    & 0.75 < lambda < 0.90,
//   1                                    & lambda >= 0.90,
// )
// $

// Total deadhead cost on arc $a$ for flight $f$: $c^"dh"_a = c^"dh" dot d_f + hat(r)(f) dot alpha(lambda_f)$

== Turnaround Enforcement
#v(1cm)

// The 45-min turnaround is enforced by *arc construction*, not a MIP constraint.

Arrival end of a flight arc is not placed at $f."arr"$, but snapped forward to the earliest existing node at $f."dest"$ satisfying the gap

$cal(T)_p = { t : (p,t) in cal(N) }$ is the set of timestamps at airport $p$

$
v' = lr(( f."dest": min{ t' in cal(T)_(f."dest") and t' >= f."arr" + Delta^"ta" } ))
$

#pause
Working and deadheading crew share the same arc both land at $v'$. Depot departures $(p, 0)$ are exempt.


// If no such node exists, *no arc is created* — the connection is infeasible by construction.


// == Turnaround — Diagram
// #v(1cm)

// #align(center)[
//   #fletcher-diagram(
//     node-stroke: 0.5pt,
//     spacing: (3cm, 1.5cm),
//     node((0,0), $("ORD", t_1)$, name: <a>),
//     node((2,0), $("LAX", t_1 + "dur")$, name: <b>),
//     node((2,-1), $("LAX", t_1 + "dur" + 45')$, name: <c>, fill: rgb("#eeffee")),
//     edge(<a>, <b>, "->", label: $f$, label-pos: 0.5),
//     edge(<b>, <c>, "->", stroke: gray, label: [wait $Delta^"ta"$], label-side: right),
//     edge(<c>, (4,-1), "->", label: [next flight], label-pos: 0.5),
//   )
// ]

== Reachability Pruning
#v(1cm)

// Only arcs on a *valid complete route* become variables. Variables with $a in.not cal(R)^w_b$ are *never created*.


For each base $b in cal(B)$

$cal(G)^w := (cal(N)^w, cal(A))$ - the window's time-expanded graph

#pause

$
cal(R)^w_b = "Fwd"_b (cal(G)^w) sect "Bwd"_b (cal(G)^w)
$
#pause
- $"Fwd"_b$:
  - Dijkstra from depot nodes $(p, 0)$, $p in cal(B) union cal(S)$
  - home-break clock state — arcs violating $D^"work"$ or $D^"away"$ are excluded
- $"Bwd"_b$: Dijkstra backwards from $(b, T^("hor",w))$

// ─────────────────────────────────────────────────────────────
// SECTION 5 — ROLLING HORIZON
// ─────────────────────────────────────────────────────────────
= Rolling Horizon

== Window Structure

For window $w in cal(W)$, with $M = 1440$ min/day:

$
T^("start",w) &= (w-1) dot S_"days" dot M \
T^("commit",w) &= T^("start",w) + S_"days" dot M \
T^("hor",w) &= T^("start",w) + (W_"days" + R_"days") dot M
$

#v(0.5cm)

#table(
  columns: (auto, auto, 1fr),
  stroke: none,
  inset: (x: 10pt, y: 12pt),
  $W_"days"$,             [7 days], [Solve window length],
  $S_"days"$,             [3 days], [Days committed per step],
  $W_"days" - S_"days"$, [4 days], [Look-ahead overlap],
  $R_"days"$,             [7 days], [Return tail],
)

== Flight Slicing
#v(1cm)

Flights in window $w$:

$
cal(F)^w = { f in cal(F) | T^("start",w) <= "dep"_f < T^("hor",w) }
$

#v(1cm)

Flights needing coverage:

$
cal(F)^("cov",w) = { f in cal(F)^w | "dep"_f < T^("commit",w) }
$

== Carry-Over: Positions
#v(1cm)

Let $rho^w_c in cal(P)$ be crew member $c$'s location at $T^("commit",w)$, derived from the path-tracing step as the airport of the last node visited at or before $T^("commit",w)$:

$
pi^(w+1)_(b,p) = |{ c in cal(C)_b | rho^w_c = p }|
quad forall b in cal(B),; p in cal(P)
$

#v(1cm)

Supply totals validated: $sum_p pi^(w+1)_(b,p) = n_b$; rounding absorbed at home depot.

== Carry-Over: Home-Break Clocks
#v(1cm)

Clock state per crew $c$: $quad tau_c$ last reset time (min) $quad d_c$ work-days since $tau_c$ $quad h_c$ home-wait since last departure.

Worst-case clock state tuple $Gamma^(w+1)_b = (d_c, tau_c, h_c)$ carried forward:

$
Gamma^(w+1)_b = arg max_(c in cal(C)_b) d^w_c
$

where $d^w_c$, $tau^w_c$, $h^w_c$ denote the values of the crew-clock state variables at the end of window $w$.

Ties broken by earliest reset $tau^w_c$, then lowest home-wait $h^w_c$.

// Using the worst case ensures no violations propagate across windows.

// ─────────────────────────────────────────────────────────────
// SECTION 5 — DDD INDIVIDUAL MODEL
// ─────────────────────────────────────────────────────────────
= #text(fill: rgb("#e87c2b"))[DDD — Individual Model]

== #text(fill: rgb("#e87c2b"))[Variables]
#v(1cm)

- $x_(c,a) in [0,1]$:
  Arc indicator for crew member $c in cal(C)$ on arc $a in cal(A)$;
  relaxed to continuous in LP

- $sigma_f in [0, m_f]$:
  Coverage slack, penalised at $C^"unc"$

== #text(fill: rgb("#e87c2b"))[Objective]
$
min quad
  &underbrace(
    sum_(c in cal(C)) sum_(a in cal(A)^"fl") c^"fl" dot d_f(a) dot x_(c,a),
    "flight hours"
  ) \
+ &underbrace(
    sum_(c in cal(C)) sum_(a in cal(A)^"dh") c^"dh"_a dot x_(c,a),
    "deadhead"
  ) \
+ &underbrace(
    sum_(c in cal(C)) sum_(a in cal(A)^"wt") (c^"wt" dot Delta t(a) + C^"ov" dot bb(1)[Delta t(a) >= 4"h"]) dot x_(c,a),
    "layover / wait"
  ) \
+ &underbrace(
    sum_(f in cal(F)^"cov") C^"unc" dot sigma_f,
    "uncovered slots"
  )
$

== #text(fill: rgb("#e87c2b"))[Constraint 1′: Flow Balance]
#v(0.8cm)

Crew *cannot* start from or end at another base's depot/horizon node.
#v(0.3cm)

For each crew member $c$ with home base $b$ and each node $v in cal(N)$:
#v(1cm)

$
sum_(a in cal(A)^+_v) x_(c,a) - sum_(a in cal(A)^-_v) x_(c,a) = cases(
  +1 quad &v = (b, 0) && "home depot",
  -1 quad &v = (b, T^("hor",w)) && "home horizon",
   0 & "otherwise",
)
$



== #text(fill: rgb("#e87c2b"))[Constraint 2′: Flight Coverage]

Each flight must be staffed to $m_f$ working crew, or pay $C^"unc"$ per missing slot.

Let $cal(A)_f = { a in cal(A)^"fl" | a."flight" = f }$ be the working arcs for flight $f$.
#v(1cm)

$
sum_(c in cal(C)) sum_(a in cal(A)_f) x_(c,a) + sigma_f >= m_f
quad forall f in cal(F)^"cov"
$


== #text(fill: rgb("#e87c2b"))[Constraint 3′: Duty Time & Home Break (Structural)]
#v(1.5cm)

Both enforced during *arc construction* — no MIP constraints needed.

#v(0.5cm)

*Duty time:* $d^"duty"(v) + d_f > Delta^"duty" ==> $ arc not created. \
Rest arc (wait $>= Delta^"rest"$) resets: $d^"duty"(v') arrow.l 0$.

#v(0.5cm)

*Home break & return window:* arcs violating $D^"work"$ or $D^"away"$ excluded during reachability pruning (Fwd pass carries clock state).

== #text(fill: rgb("#e87c2b"))[Constraint 4′: Non-Negativity & Integrality]
#v(3.5cm)

$
x_(c,a) in (0,1) quad forall c in cal(C), a in cal(A)
$

$
sigma_f in ZZ_(>=0) quad forall f in cal(F)^"cov"
$

#v(0.5cm)

== #text(fill: rgb("#e87c2b"))[Solver]
#v(0.5cm)

DDD iteratively refines the network till LP *arc-feasible*

1. *Solve LP* on current network
#v(0.5cm)
#pause
2. *Detect violations*:
  - node too coarse to represent true arrival
  $
  lr(|t_(v') - t^"true"|) > Delta_"bucket"
  $
  - turnaround infeasible in current discretisation
  $
   t^"true" + Delta^"ta" > t_"next dep"
  $
  - capped at 500 per iteration, sorted by airport activity
#pagebreak()
3. *Refine*:
  - insert $(p, t^*)$ at each violated time
  - bisect spanning wait arc
  - expose new flight/deadhead arcs
  - update flow balance and coverage in-place
#v(0.5cm)
#pause
4. *Repeat* until no violations
#v(0.5cm)
5. *Switch to MIP*:
  - $x_(c,a)$ to binary, $sigma_f$ to integer
  - `MIPGap = 0.01`, `TimeLimit = 300s`

== #text(fill: rgb("#e87c2b"))[Individual Route Tracing]
#v(0.8cm)

// Because variables are per-individual, *no flow decomposition* is needed.

// #v(0.3cm)

For each crew member $c$ with active arcs $cal(A)^*_c$:

+ Build adjacency $"out"(v) = a$ for each node (prefer `flight` > `deadhead` > `wait`)
+ Trace path from home depot $(b, 0)$ following $"out"(dot)$
+ Emit each non-wait leg as a route entry: type, origin, destination, departure, arrival

$
"Route"(c) = { ell in cal(A)^*_c | ell."type" != "wait" }
$

== #text(fill: rgb("#e87c2b"))[Route Output Schema]

Each route entry:

#table(
  columns: (auto, 1fr),
  stroke: none,
  inset: (x: 10pt, y: 10pt),
  [`crew_id`],   [Unique crew member identifier],
  [`base`],      [Home base airport code],
  [`legs`],      [Ordered list of leg dictionaries],
)

Each leg:

#table(
  columns: (auto, 1fr),
  stroke: none,
  inset: (x: 10pt, y: 10pt),
  [`type`],      [`"flight"` or `"deadhead"`],
  [`from`, `to`],[Airport codes],
  [`dep`, `arr`],[Minutes from week start],
  [`flight_id`], [Reference to the covered flight (or `null`)],
)

== 42069 Entry Fake Data
```
============================================================
ROLLING WINDOW 1  |  days 1–3  (solve horizon: day 14)
============================================================
Building network...
  Depot/horizon nodes: 20  (0.0s)
  All nodes added: 13804  (0.0s)
  Wait arcs built  (0.0s)
  WARNING: 39 flights have no connectable arc!
  Turnaround-pruned: 18575 arcs (< 45 min ground time)
  Duty-pruned  : 11938 arcs (exceeded 20h duty / 5d period since last 8h rest)
  Flight arcs: 6637  (0.2s)
  Network complete: 13804 nodes, 20431 arcs  (total 0.2s)
Set parameter Username
Academic license - for non-commercial use only - expires 2027-04-23
  Building flow model: 20431 arcs, 13804 nodes, 10 bases  (0.0s)
  Computing reachability per base (parallel Dijkstra)... done (0.5s)
  Reachable (base,arc) pairs: 114,556 / 204,310 = 56.1% of full cross-product
  Variables: 19,348,099  (incl. 15,574,980 per-crew flight, 3,608,811 per-crew home-wait)  (40.1s)
  Flow balance constraints: 138,040  (46.1s)
  Linking constraints (var_work_crew==var_work): 59,481  (54.8s)
  Per-crew home-balance constraints: 3,611,433  (84.1s)```

// ─────────────────────────────────────────────────────────────
// SECTION 4 — AGGREGATE FLOW MODEL
// ─────────────────────────────────────────────────────────────
= #text(fill: rgb("#00aacc"))[Aggregate Flow Model]

== #text(fill: rgb("#00aacc"))[Variables]
#v(1cm)

#table(
  columns: (auto, 1fr),
  stroke: none,
  inset: (x: 10pt, y: 10pt),
  $f^"work"_(b,a) in ZZ_(>=0)$,  [Working crew from base $b$ on arc $a in cal(A)^"fl"$],
  $f^"dh"_(b,a) in ZZ_(>=0)$,    [Deadheading crew from base $b$ on arc $a in cal(A)^"dh"$],
  $f^"wt"_(b,a) in ZZ_(>=0)$,    [Waiting crew from base $b$ on arc $a in cal(A)^"wt"$],
  $phi_(b,a)$,                    [$f^"work"_(b,a) + f^"dh"_(b,a) + f^"wt"_(b,a)$ — total flow from base $b$ on arc $a$],
  $sigma_f in ZZ_(>=0)$,          [Uncovered crew slots for flight $f$],
)

== #text(fill: rgb("#00aacc"))[Objective]
$
min quad
  &underbrace(
    sum_(b, a in cal(A)^"fl") c^"work"_a f^"work"_(b,a),
    "flight hours"
  ) \
+ &underbrace(
    sum_(b, a in cal(A)^"dh") c^"dh"_a f^"dh"_(b,a),
    "deadhead"
  ) \
+ &underbrace(
    sum_(b, a in cal(A)^"wt") c^"wt"_a f^"wt"_(b,a),
    "layover / wait"
  ) \
+ &underbrace(
    sum_(f in cal(F)^"cov") C^"unc" dot sigma_f,
    "uncovered slots"
  )
$

== #text(fill: rgb("#00aacc"))[Constraint 1: Flow Balance]
#v(1cm)

Flow conserved at every interior node; supply injected at depots.

$
sum_(a in cal(A)^+_v) phi_(b,a) - sum_(a in cal(A)^-_v) phi_(b,a) = cases(
  pi^w_(b,p) quad & v = (p, 0) quad forall p in cal(P),
  0           quad & "otherwise"
) \ \
 forall b in cal(B), forall v in cal(N)^w without {v^("hor",w)_b}
$

== #text(fill: rgb("#00aacc"))[Constraint 2: Horizon Flow Balance]
$space$ \ \

Final window: all crew return home. Intermediate: free-exit sink.

#v(1cm)

$
sum_(a in cal(A)^-_(v^("hor",w)_b)) phi_(b,a)
- sum_(a in cal(A)^+_(v^("hor",w)_b)) phi_(b,a)
= cases(n_b & "final window" (w = |cal(W)|), >= 0 & "intermediate window")
$



== #text(fill: rgb("#00aacc"))[Constraint 3: Flight Coverage]
#v(1cm)

Each flight must be staffed to $m_f$ working crew, or pay $C^"unc"$ per missing slot.


$
sum_(b in cal(B)) sum_(a in cal(A)_f) f^"work"_(b,a) + sigma_f >= m_f
quad forall f in cal(F)^"cov"
$

Where $cal(A)_f = { a in cal(A)^"fl" | a."flight" = f }$ be the working arcs for flight $f$


== #text(fill: rgb("#00aacc"))[Constraint 5: Duty Time (Structural)]
#v(0.8cm)

Enforced by *arc construction* — no MIP row added.

#v(0.5cm)

If accumulated duty at node $v$ would exceed the limit on flight $f$, the arc is never created:

$
d^"duty"(v) + d_f > Delta^"duty" ==> "arc not created"
$

Propagated forward through the network:

$
d^"duty"(v') = min(d^"duty"(v'), d^"duty"(v) + d_f)
$

A rest arc (wait $>= Delta^"rest"$) resets: $d^"duty"(v') arrow.l 0$.

#pagebreak()

== #text(fill: rgb("#00aacc"))[Constraint 6: Mandatory Home-Break]
#v(0.6cm)

Enforced by *both* arc pruning and an explicit MIP constraint.

#v(0.4cm)

*Structural:*
// during network construction, a home-break clock state $(tau, d, h)$ is propagated forward at each node.
Any arc that would be taken while $d > D^"work"$ or $(t - tau)/1440 > D^"away"$ is pruned

#v(0.5cm)
#pause

*MIP constraint:*
for each base $b$ and each epoch $k in cal(K)_b$ of length
$D^"away"$, total flow returning home must cover the full crew

$
sum_(a in cal(A)^"hb"_(b,k)) f^"wt"_(b,a) >= n_b
quad forall b in cal(B), k in cal(K)_b
$

#v(0.4cm)

// The pruning removes routes that obviously violate the limit; the MIP constraint guarantees every crew member actually completes a home break within each epoch.


// == #text(fill: rgb("#00aacc"))[Constraint 5: Duty Time (Structural)]
// #v(1cm)

// Duty-time limit enforced during *arc construction*, not as a MIP constraint:

// $
// d^"duty"(v) + d_f > Delta^"duty" ==> "arc not created"
// $

// where $d^"duty"(v)$ is the minimum accumulated duty at node $v$, propagated forward:

// $
// d^"duty"(v') = min(d^"duty"(v'), d^"duty"(v) + d_f)
// $

// A rest arc (wait $>= Delta^"rest"$) resets the duty clock: $d^"duty"(v') arrow.l 0$.

== #text(fill: rgb("#00aacc"))[Constraint 7: Non-Negativity & Integrality]

#v(1cm)

// Continuous for LP relaxation; switched to integer for the final MIP solve.

#v(0.5cm)

$
f^"work"_(b,a), f^"dh"_(b,a), f^"wt"_(b,a) in ZZ_(>=0) quad forall b in cal(B), a in cal(A)
$

$
sigma_f in ZZ_(>=0) quad forall f in cal(F)^"cov"
$

== #text(fill: rgb("#00aacc"))[42069 Entry Fake Data]
```
============================================================
ROLLING WINDOW 1  |  days 1–3  (solve horizon: day 14)
============================================================
Building network...
  Depot/horizon nodes: 20  (0.0s)
  All nodes added: 13804  (0.0s)
  Wait arcs built  (0.1s)
  WARNING: 39 flights have no connectable arc!
  Turnaround-pruned: 18575 arcs (< 45 min ground time)
  Duty-pruned  : 11938 arcs (exceeded 20h duty / 5d period since last 8h rest)
  Flight arcs: 6637  (0.2s)
  Network complete: 13804 nodes, 20431 arcs  (total 0.2s)
Set parameter Username
Academic license - for non-commercial use only - expires 2027-04-23
  Building flow model: 20431 arcs, 13804 nodes, 10 bases  (0.0s)
  Computing reachability per base (parallel Dijkstra)... done (0.6s)
  Reachable (base,arc) pairs: 114,556 / 204,310 = 56.1% of full cross-product
  Variables: 178,065  (1.0s)
  Flow balance constraints: 138,040  (1.9s)
  Coverage constraints: 4028  (2.0s)
  Mandatory home-break constraints: 20  (2.0s)
  Model built: 178,065 vars, 142,088 constrs  (2.0s)```

== #text(fill: rgb("#00aacc"))[42069 Entry Fake Data]
```
============================================================
SOLUTION SUMMARY
============================================================
Mode            : Rolling horizon
Status          : rolling_horizon
Total cost      : 2,489,313,613.4
  Flight hours  : 2,444,027,600.0
  Deadhead      : 25,176,100.0
  Layover/wait  : 20,109,913.4
Flights         : 40579
Covered         : 40579
Uncovered slots : 0.0
Individual routes: 2554
Solve time      : 385.6s```

== #text(fill: rgb("#00aacc"))[US Domestic 2025/01 (599k Entries)]
```
============================================================
ROLLING WINDOW 1  |  days 1–3  (solve horizon: day 14)
============================================================
Building network...
  Depot/horizon nodes: 388  (0.0s)
  All nodes added: 341447  (1.3s)
  Wait arcs built  (3.8s)
  WARNING: 458 flights have no connectable arc!
  Turnaround-pruned: 246036 arcs (< 45 min ground time)
  Duty-pruned  : 144079 arcs (exceeded 20h duty / 5d period since last 8h rest)
  HB-pruned    : 36 arcs fully pruned (all bases exceeded MAX_WORK_DAY=5 or MAX_AWAY_DAYS=7)
  Flight arcs: 101921  (96.9s)
  Network complete: 341447 nodes, 412355 arcs  (total 96.9s)
Set parameter Username
Academic license - for non-commercial use only - expires 2027-04-23
  Building flow model: 412355 arcs, 341447 nodes, 194 bases  (0.0s)
  Computing reachability per base (parallel Dijkstra)... done (28.8s)
  Reachable (base,arc) pairs: 34,855,562 / 79,996,870 = 43.6% of full cross-product
  Variables: 49,750,734  (343.8s)
  Flow balance constraints: 66,240,718  (1277.8s)
  Coverage constraints: 57854  (1298.1s)
  Mandatory home-break constraints: 388  (1305.2s)
  Model built: 49,750,734 vars, 66,298,960 constrs  (1305.2s)```




// // ─────────────────────────────────────────────────────────────
// // SECTION 6 — DDD SOLVE LOOP
// // ─────────────────────────────────────────────────────────────
// = #text(fill: rgb("#e87c2b"))[DDD Solve Loop]

// == #text(fill: rgb("#e87c2b"))[DDD Solve Loop]
// #v(0.6cm)

// // Iteratively refines time discretisation until the LP solution is *arc-feasible*.

// #v(0.5cm)

// + *Solve LP relaxation* on current network
// + *Detect violations*: active arcs where $|t_{v'} - t^"true"| > Delta_"bucket"$ or $t^"true" + Delta^"ta" > t_"next dep"$; capped at 500 per iteration, sorted by airport activity
// + *Refine*: insert node $(p, t^*)$ at each violated time; bisect the spanning wait arc; expose new flight/deadhead arcs; update flow balance and coverage constraints in-place
// + *Repeat* until no violations remain
// + *Switch to MIP*: $x_{c,a}$ to binary, $sigma_f$ to integer; `MIPGap = 0.01`, `TimeLimit = 300s`

// == Overview
// #v(1cm)

// DDD iteratively refines the time discretisation until the LP solution is *arc-feasible*.

// #v(0.5cm)

// + *Solve LP relaxation* on current network
// + *Inspect violations*: active arcs whose true arrival time is not exactly represented by a node
// + *Add nodes* at violated times → rewire wait arcs → add variables and constraints
// + Repeat until no violations remain
// + *Switch to MIP*: set all $x_{c,a}$ to binary; solve with 1% MIP gap and 5-min time limit

// == #text(fill: rgb("#e87c2b"))[Violation Detection]
// #v(0.8cm)

// An arc $(v, v')$ with true end-time $t^"true"$ is a *violation* if:

// $
// lr(|t_{v'} - t^"true"|) > Delta_"bucket"
// quad "or" quad
// t^"true" + Delta^"ta" > t_"next dep"
// $

// Violations are sorted by airport activity (most-used airports fixed first) and capped at 500 per iteration to control model growth.

// == #text(fill: rgb("#e87c2b"))[Refinement Step]
// #v(1cm)

// When node $(p, t^*)$ is added:

// + *Bisect wait arc* at $p$: remove the spanning wait arc; insert two new wait arcs through $(p, t^*)$
// + *Wire flow balance*: for each crew member $c$, add flow conservation at the new node
// + *Expose new arcs*: create flight/deadhead arcs from/to the new node; add variables for all crew members who can use them
// + *Update coverage*: new flight arcs are added to coverage constraints via `chgCoeff`

// == #text(fill: rgb("#e87c2b"))[Convergence & MIP]
// #v(1cm)

// LP convergence: zero violations in the current solution.

// #v(0.5cm)

// After convergence:

// - All $x_(c,a)$ switched from `CONTINUOUS` to `BINARY`
// - Slack $sigma_f$ switched to `INTEGER`
// - Gurobi parameters: `MIPGap = 0.01`, `TimeLimit = 300` s

// #v(0.3cm)

// #align(right)[_Network topology is shared; only the variable types change_]

// ─────────────────────────────────────────────────────────────
// SECTION 7 — STAGE 2: FLOW DECOMPOSITION
// ─────────────────────────────────────────────────────────────
= #text(fill: rgb("#00aacc"))[Stage 2: Flow Decomposition]

== Overview
#v(1cm)

// The MIP yields *aggregate integer flows*
// Flow Decomposition assigns one route per crew member.

#v(0.5cm)

*For each base $b$:*
1. Build residual: $"res"(b, a) = f^"work"_(b,a) + f^"dh"_(b,a) + f^"wt"_(b,a)$
#v(0.5cm)
2. For each depot group (non-home first, home last):
  - BFS to find arcs reachable from this depot
  - Trace one path per crew member; consume arcs from residual
#v(0.5cm)
3. Classify each leg: _working_ if $(b, a) in "arc_is_work"$, else _deadheading_

== #text(fill: rgb("#00aacc"))[Path Tracing]
#v(1cm)

BFS state: $(v, g)$ where $g = 1$ if home-wait $h_c >= Delta^"hb"$ (gate open).

*Home-break gate:
* at home base with $g = 0$, only wait or return-to-home arcs are traversable.

#pause
#v(0.5cm)

=== Home-wait accumulation

$
h_c' = cases(
  h_c + (t_"end" - t_"start") & "wait arc at home base",
  0                             & "flight/dh arrives at home base",
  h_c                           & "otherwise"
)
$

Arc priority: flight $>$ deadhead $>$ wait.

#pagebreak()

// === Terminal selection

// The first (shallowest) node past the commit cutoff is used; BFS order does not guarantee the latest-time terminal.

== #text(fill: rgb("#00aacc"))[Route Assembly]
#v(1cm)

// Routes assembled across windows:

Let $"legs"^w (c)$ denote the set of legs (non-wait arcs) assigned to crew member $c$ in window $w$, obtained from path tracing.

$
"Route"(c) = union.sq.big_(w in cal(W)) { ell in "legs"^w (c) | "dep"(ell) < T^("commit",w) }
$

#v(1cm)

Legs sorted chronologically; overlap duplicates removed by $("from", "to", "dep", "arr")$ key.

== #text(fill: rgb("#00aacc"))[Home-Break Verification]

After path tracing, simulate clocks per crew $c$:

- $tau_c$: last clock reset (min)
- $d_c$: work-days since $tau_c$
- $h_c$: home-wait since last departure

*Reset* on departure from home with $h_c >= Delta^"hb"$:
$ tau_c arrow.l "dep" quad  d_c arrow.l 0 quad h_c arrow.l 0 $

#v(0.5cm)

*Violation flags* (first breach only):

$
text("Away"): (f."dep" - tau_c) / 1440 > D^"away"
quad
text("Work"): d_c > D^"work"
$

Worst-case state forwarded to next window via $Gamma^(w+1)_b$.


```
============================================================
ROLLING WINDOW 2  |  days 4–6  (solve horizon: day 17)
============================================================
  ...
  WARNING: crew 69 (base ATL) fallback to home — position gap: was at BOS, no flow found there; starting from ATL instead.
  WARNING: crew 1929 (base ORD) fallback to home — position gap: was at SEA, no flow found there; starting from ORD instead.
  WARNING: crew 1931 (base ORD) fallback to home — position gap: was at SEA, no flow found there; starting from ORD instead.
  WARNING: crew 2166 (base SEA) fallback to home — position gap: was at MIA, no flow found there; starting from SEA instead.
  WARNING: crew 2157 (base SEA) fallback to home — position gap: was at LAX, no flow found there; starting from SEA instead.
  Window status : optimal
  Window cost   : 240,588,185.0
  Flights in window     : 4028
  Covered               : 4028
  Uncovered slots       : 0.0
  Home-break violations : 97 (crew rescheduling needed)
```

```
============================================================
ROLLING WINDOW 10  |  days 28–30  (solve horizon: day 37)
============================================================
  HOME-BREAK violation crew 2 (ATL): Day 28: worked 7d > 5d (BOS->ORD)
  WARNING: crew 105 (base ATL) fallback to home — position gap: was at DFW, no flow found there; starting from ATL instead.
  WARNING: crew 106 (base ATL) fallback to home — position gap: was at DFW, no flow found there; starting from ATL instead.
  WARNING: crew 109 (base ATL) fallback to home — position gap: was at DFW, no flow found there; starting from ATL instead.
  WARNING: crew 116 (base ATL) fallback to home — position gap: was at DFW, no flow found there; starting from ATL instead.
  WARNING: crew 129 (base ATL) fallback to home — position gap: was at DFW, no flow found there; starting from ATL instead.
  WARNING: crew 132 (base ATL) fallback to home — position gap: was at DFW, no flow found there; starting from ATL instead.
  WARNING: crew 133 (base ATL) fallback to home — position gap: was at DFW, no flow found there; starting from ATL instead.
  WARNING: crew 134 (base ATL) fallback to home — position gap: was at DFW, no flow found there; starting from ATL instead.
  WARNING: crew 139 (base ATL) fallback to home — position gap: was at DFW, no flow found there; starting from ATL instead.
  WARNING: crew 141 (base ATL) fallback to home — position gap: was at DFW, no flow found there; starting from ATL instead.
  WARNING: crew 149 (base ATL) fallback to home — position gap: was at DFW, no flow found there; starting from ATL instead.
  WARNING: crew 153 (base ATL) fallback to home — position gap: was at DFW, no flow found there; starting from ATL instead.
  WARNING: crew 154 (base ATL) fallback to home — position gap: was at DFW, no flow found there; starting from ATL instead.
  WARNING: crew 156 (base ATL) fallback to home — position gap: was at DFW, no flow found there; starting from ATL instead.
  WARNING: crew 161 (base ATL) fallback to home — position gap: was at DFW, no flow found there; starting from ATL instead.
  WARNING: crew 162 (base ATL) fallback to home — position gap: was at DFW, no flow found there; starting from ATL instead.
  WARNING: crew 168 (base ATL) fallback to home — position gap: was at DFW, no flow found there; starting from ATL instead.
  WARNING: crew 171 (base ATL) fallback to home — position gap: was at JFK, no flow found there; starting from ATL instead.
  WARNING: crew 179 (base ATL) fallback to home — position gap: was at JFK, no flow found there; starting from ATL instead.
  WARNING: crew 187 (base ATL) fallback to home — position gap: was at DFW, no flow found there; starting from ATL instead.
  WARNING: crew 188 (base ATL) fallback to home — position gap: was at DFW, no flow found there; starting from ATL instead.
  WARNING: crew 96 (base ATL) fallback to home — position gap: was at SFO, no flow found there; starting from ATL instead.
  WARNING: crew 97 (base ATL) fallback to home — position gap: was at SFO, no flow found there; starting from ATL instead.
  WARNING: crew 98 (base ATL) fallback to home — position gap: was at SFO, no flow found there; starting from ATL instead.
  WARNING: crew 102 (base ATL) fallback to home — position gap: was at SFO, no flow found there; starting from ATL instead.
  WARNING: crew 183 (base ATL) fallback to home — position gap: was at SFO, no flow found there; starting from ATL instead.
  WARNING: crew 184 (base ATL) fallback to home — position gap: was at SFO, no flow found there; starting from ATL instead.
  ...
  Window status : optimal
  Window cost   : 250,759,659.1
  Flights in window     : 4111
  Covered               : 4111
  Uncovered slots       : 0.0
  Home-break violations : 1962 (crew rescheduling needed)
```

= #text(fill: rgb("#00aacc"))[Demo]
// ─────────────────────────────────────────────────────────────
// SECTION 5b — COMPLEXITY COMPARISON
// ─────────────────────────────────────────────────────────────
= Complexity Comparison

== Complexity

#v(0.4cm)

#table(
  columns: (auto, auto, auto),
  stroke: none,
  inset: (x: 10pt, y: 10pt),
  $N$, [$= |cal(N)|$],   [nodes],
  $A$, [$= |cal(A)|$],   [arcs],
  $A^"reach"$, [$subset.eq A$], [arcs surviving reachability pruning \ (typically $A^"reach" << C dot A$)],
  $F$, [$= |cal(F)^"cov"|$], [covered flights],
  $B$, [$= |cal(B)|$],   [bases],
  $C$, [$= |cal(C)|$],   [crew],
  $W$, [$= |cal(W)|$],   [windows],
)

#pagebreak()

#table(
  columns: (auto, 1fr, 1fr),
  stroke: 0.5pt,
  inset: (x: 10pt, y: 12pt),
  [],
    [#text(fill: rgb("#00aacc"))[*Aggregate Flow*]],
    [#text(fill: rgb("#e87c2b"))[*DDD — Individual*]],
  // Decision variables
  [*Vars*],
    [$3 B A + F$ integer],
    [$C A^"reach" + F$ binary],
  // Flow balance constraints
  [*Flow balance*],
    [$B(N - B)$],
    [$C N$],
  // Coverage constraints
  [*Coverage*],
    [$F W$],
    [$F W$],
  // Home-break constraints
  [*Home-break*],
    [$B |cal(K)|$],
    [_structural_],
  // MIP solve complexity
  [*MIP*],
    [$O(B A)$],
    [$O(C A^"reach")$],
  // Route extraction
  [*Extraction*],
    [$O(B A)$ BFS],
    [$O(C A^"reach")$],
  // DDD refinement overhead
  [*DDD overhead*],
    [—],
    [$O(K_"iter" (A + C N_"new"))$],
)

= Future Work
==
=== Existing Models
- Extract the schedule correctly
- Senior, Junior substitutions

=== New Model
- Figure out a way to solve for US Domestic 2025/01 Dataset (599k Entries) in reasonable time running on my mac
