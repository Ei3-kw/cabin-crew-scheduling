#import "@preview/touying:0.5.5": *
#import themes.university: *
#import "@preview/cetz:0.3.1"
#import "@preview/fletcher:0.5.3" as fletcher: node, edge
#import "@preview/ctheorems:1.1.3": *
#import "@preview/numbly:0.1.0": numbly

// cetz and fletcher bindings for touying
#let cetz-canvas = touying-reducer.with(reduce: cetz.canvas, cover: cetz.draw.hide.with(bounds: true))
#let fletcher-diagram = touying-reducer.with(reduce: fletcher.diagram, cover: fletcher.hide)

// Theorems configuration by ctheorems
#show: thmrules.with(qed-symbol: $square$)
#let theorem = thmbox("theorem", "Theorem", fill: rgb("#eeffee"))
#let corollary = thmplain(
  "corollary",
  "Corollary",
  base: "theorem",
  titlefmt: strong
)
#let definition = thmbox("definition", "Definition", inset: (x: 1.2em, top: 1em))
#let example = thmplain("example", "Example").with(numbering: none)
#let proof = thmproof("proof", "Proof")

#let two-columns(left-content, right-content) = {
  grid(
    columns: (1fr, 1fr),
    gutter: 7pt,
    left-content,
    right-content
  )
}

#show: university-theme.with(
  aspect-ratio: "16-9",
  // config-common(handout: true),
  config-info(
    title: [Cabin Crew Scheduling],
    subtitle: [],
    author: [Ella Wang],
    date: datetime.today(),
    institution: [],
  ),
)

// #set heading(numbering: numbly("{1}.", default: "1.1"))

#title-slide()

== Outline <touying:hidden>

= The Problem

== Cabin Crew Scheduling
#v(1cm)
- $21%$ of operating expenses (second only to fuel)
- $45.8%$ of airline staff
- Bad schedules have consequences -- missed duty limits stranded passengers for hours
#pause
#v(2cm)
*Cut costs while keeping every flight covered*


#pagebreak()

== Complications
#v(1cm)
- Cross-qualified across multiple aircraft types
#pause
- Categorised into multiple classes according to skills and experiences
  // - stewards
  // - hostesses
  // - cabin mates
  // - head cabin mates
#pause
- One flight delay/ cancellation may disturb future scheduling
#image("media/pairing_side_by_side.svg")



#pagebreak()
== Assumptions
- The flight schedule, fleet assignment and aircraft routing have already been solved upstream
- The planning horizon is one week

= Original Paper Formulations
== Sets

#table(
  columns: (auto, 1fr),
  stroke: none,
  inset: (x: 10pt, y: 10pt),
  $R$,       [Set of cabin crew classes],
  $F$,       [Set of scheduled flights],
  $B$,       [Set of home bases],
  $T$,       [Set of feasible *team* pairings for cabin crews],
  $P_r$, [Set of feasible *individual* pairings for Class $r$ *available* crew.],
  $P^e_r$,  [Set of feasible *individual* pairings for Class $r$ *extra* cabin crews],
)
Each pairing must start and end at the crew's home base

== Data

#table(
  columns: (auto, 1fr),
  stroke: none,
  inset: (x: 10pt, y: 16pt),

  $"cost"_t$,
  [TAFB (Time Away From Base) cost of team pairing $t in T$],

  $"cost"^"avail"_(r p)$,
  [TAFB cost of individual pairing $p in P_r$ for Class $r$ available cabin crew],

  $"cost"^"extra"_(r p)$,
  [TAFB cost of individual pairing $p in P^e_r$ for Class $r$ extra cabin crew],

  $"covers"_(f t)$,
  $= cases(1 & "if team pairing" t "covers flight" f in F, 0 & "otherwise")$,

  $"covers"^"avail"_(f r p)$,
  $= cases(1 & "if individual pairing" p in P_r "covers flight" f in F, 0 & "otherwise")$,

  $"covers"^"extra"_(f r p)$,
  $= cases(1 & "if extra individual pairing" p in P^e_r "covers flight" f in F, 0 & "otherwise")$,

  $"req"_(r f)$,
  [Number of Class $r$ cabin crews required by flight $f$],

  $"avail"_r$,
  [Total number of available Class $r$ cabin crews],

  $mu$,
  [Unit substitution penalty cost; satisfies $"cost"^"avail"_(r p), "cost"^"extra"_(r p) << mu << M$],

  $M$,
  [penalty cost for employing extra cabin crew],
)

== Variables

#table(
  columns: (auto, 1fr),
  stroke: none,
  inset: (x: 10pt, y: 20pt),

  $"select"_t$,
  $display(= cases(1 & "if team pairing" t "is selected", 0 & "otherwise"))$,

  $"assign"_(r p) in ZZ_(>=0)$,
  [Number of times individual pairing $p in P_r$ is used for Class $r$ available cabin crew],

  $"extra"_(r p) in ZZ_(>=0)$,
  [Number of times individual pairing $p in P^e_r$ is used for Class $r$ extra cabin crew],

  $"sub"_(r f) in ZZ_(>=0)$,
  [Number of times Class $r$ cabin crew is substituted by another class on flight $f$],
)

== Traditional Cabin Crew Pairing Problem (TCCPP)

// The baseline model from the literature. Cabin crews are treated as homogeneous teams separated by aircraft type. Manpower availability and crew class heterogeneity are not considered.
$space$ \ \

- Minimise the total TAFB pairing cost
$
min quad sum_(t in T) "cost"_t dot "select"_t
$ <tccpp-obj>

#pagebreak()
$space$ \ \

- Each flight $f$ must be covered by at least one selected team pairing
$
sum_(t in T) "covers"_(f t) dot "select"_t >= 1,
quad forall f in F
$ <tccpp-c1>

// - Binary integrality of the team pairing selection variables.
// $
// "select"_t in {0, 1},
// quad forall t in T
// $ <tccpp-c2>

== Multi-class Individual Cabin Crew Pairing Problem with Availability and Controlled Crew Substitution
$space$ \ \
$
min quad
  &underbrace(
    sum_(r in R) sum_(p in P_r) "cost"^"avail"_(r p) dot "assign"_(r p),
    "available crew cost"
  ) \
  #pause
  + &underbrace(
    sum_(r in R) sum_(f in F) mu dot "sub"_(r f),
    "substitution penalty"
  ) \
  #pause
  + &underbrace(
    sum_(r in R) sum_(p in P^e_r) ("cost"^"extra"_(r p) + M) dot "extra"_(r p),
    "extra crew cost"
  )
$ <accs-obj>

== MICCPP-ACCS
=== Total satisfaction constraint
the total number of cabin crews of all classes assigned to flight $f$ must meet the total demand across all classes.
#v(1cm)
$
sum_(r in R) sum_(p in P_r) "covers"^"avail"_(f r p) dot "assign"_(r p) \
+ sum_(r in R) sum_(p in P^e_r) "covers"^"extra"_(f r p) dot "extra"_(r p)
>= sum_(r in R) "req"_(r f),
quad forall f in F
$ <accs-c1>

#pagebreak()
=== Minimum satisfaction constraint
At least one qualified crew member from each class must be assigned to every flight $f$.
#v(1cm)
$
\ \
sum_(p in P_r) "covers"^"avail"_(f r p) dot "assign"_(r p)
+ sum_(p in P^e_r) "covers"^"extra"_(f r p) dot "extra"_(r p)
>= 1 \
quad forall f in F,forall r in R
$ <accs-c2>
#pagebreak()

=== Substitution recording constraint
Tracks the number of times Class $r$ is substituted on flight $f$ // Each substitution incurs penalty $mu$.
#v(1cm)
$
\ \
sum_(p in P_r) "covers"^"avail"_(f r p) dot "assign"_(r p)
+ sum_(p in P^e_r) "covers"^"extra"_(f r p) dot "extra"_(r p)
+ "sub"_(r f) >= "req"_(r f) \
quad forall f in F, forall r in R
$ <accs-c3>
#pagebreak()
=== Crew availability constraint
#v(3cm)
$ \ \
sum_(p in P_r) "assign"_(r p) <= "avail"_r,
quad forall r in R
$ <accs-c4>
#pagebreak()
=== Non-negativity
#v(3cm)
$
"assign"_(r p) in ZZ_(>=0),
quad forall r in R, forall p in P_r
$ <accs-c5>

$
"extra"_(r p) in ZZ_(>=0),
quad forall r in R, forall p in P^e_r
$ <accs-c6>

$
"sub"_(r f) in ZZ_(>=0),
quad forall f in F, forall r in R
$ <accs-c7>



== Simplified MICCPP-A
// A simplified version of MICCPP-ACCS where CCS is *forbidden*. Each class is scheduled independently. Used to derive manpower requirement benchmarks $"MC"_r$ and $"MM"_r$.

$
space
\ \ \
min quad
  sum_(p in P_r) "cost"^"avail"_(r p) dot "assign"_(r p)
  \+ sum_(p in P^e_r) ("cost"^"extra"_(r p) + M) dot "extra"_(r p)
$ <a-obj>

#pagebreak()
For each $r in R$, subject to:

$
sum_(p in P_r) "covers"^"avail"_(f r p) dot "assign"_(r p)
+ sum_(p in P^e_r) "covers"^"extra"_(f r p) dot "extra"_(r p)
>= "req"_(r f),
quad forall f in F
$ <a-c1>

$
sum_(p in P_r) "assign"_(r p) <= "avail"_r
$ <a-c2>

$
"assign"_(r p) in ZZ_(>=0),
quad forall p in P_r
$ <a-c3>

$
"extra"_(r p) in ZZ_(>=0),
quad forall p in P^e_r
$ <a-c4>

= Original Paper Approach

== Overview
#v(3cm)
#align(center)[
  #grid(
    columns: (1fr, 0.15fr, 1fr, 0.15fr, 1fr),
    gutter: 0pt,
    // boxes
    block(fill: rgb("#e8f0fb"), radius: 8pt, inset: 14pt, width: 100%)[
      #align(center)[
        #text(weight: "bold", fill: rgb("#1a3a5c"))[DPIA]\
        #v(4pt)
        #text(size: 18pt)[Dynamic Programming Initialisation Algorithm \
        warm-start Restricted Master Problem (RMP)]
      ]
    ],
    align(horizon + center)[#text(size: 28pt, fill: luma(120))[→]],
    block(fill: rgb("#e8f0fb"), radius: 8pt, inset: 14pt, width: 100%)[
      #align(center)[
        #text(weight: "bold", fill: rgb("#1a3a5c"))[Column Generation]\
        #v(4pt)
        #text(size: 18pt)[RMP ↔ Pricing\
        until LP optimal]
      ]
    ],
    align(horizon + center)[#text(size: 28pt, fill: luma(120))[→]],
    block(fill: rgb("#e8f0fb"), radius: 8pt, inset: 14pt, width: 100%)[
      #align(center)[
        #text(weight: "bold", fill: rgb("#1a3a5c"))[MIP\
        ]
        #text(size: 18pt)[Integer solution\
        from final pool]
      ]
    ],
  )
  \
  #pause
  For large instances: replace CG + MIP with Genetic Algorithm.
]

== DPIA
  // CG needs a *feasible starting solution* before the first RMP solve.

  #v(0.5cm)
  Provides the warm-start that makes CG tractable from the first iteration.

  - Quickly generates a small set of legal pairings covering every flight
  - Uses extra crew variables — feasibility always guaranteed
  - No crew availability constraints at this stage

== CG
  // The full set of pairings is too large to enumerate — CG builds it incrementally.
  RMP and PP alternate till no improving column exists → LP optimality

  #v(0.4cm)
  #grid(
    columns: (1fr, 1fr),
    gutter: 1cm,
    block(fill: rgb("#f0f4ff"), radius: 8pt, inset: 16pt)[
      #text(weight: "bold", fill: rgb("#1a3a5c"))[RMP]
      #v(6pt)
      - LP relaxation of the full model
      - Solved by *Simplex*
      - Outputs *dual prices* per constraint
    ],
    block(fill: rgb("#f0f4ff"), radius: 8pt, inset: 16pt)[
      #text(weight: "bold", fill: rgb("#1a3a5c"))[PP]
      #v(6pt)
      - Finds pairings with *negative reduced cost*
      - Resource Constrained Shortest Path (RCSPP)
      - Solved via *DP labelling* on duty network
    ],
  )

// ── MIP ───────────────────────────────────────────────────────────
== Integer recovery via MIP
  #v(0.3cm)
  Once CG terminates, the column pool is *fixed*
  #pause
  #v(0.4cm)
  - MIP is solved over only those columns
  #pause
  - Recovers integer (assignable) pairings from the LP solution
  #pause
  - global optimality is not guaranteed


= Original Paper Limitations
==
=== Method
Restricting MIP to the last RMP pool is fast, but may miss integer-optimal solutions that were never generated.
#v(1cm)
#pause
=== TAFB (Time Away From Base) Cost Approx
#pause
- Shorthaul VS longhaul flights
#pause
- Deadhead arcs
#pause
#v(1cm)
=== Substitution Order
- "All classes can substitute any other class in either direction"
#pagebreak()
=== Flight Delay and Cancellations
#align(center)[
#image("media/pairing_side_by_side.svg", width:100%)
]

= Extensions & \ Plan of Attack
== Extensions
#v(1cm)
- Compute costs for
  - Flight hours
  - Layovers
  - Deadhead
#pause
#v(1cm)
- Restricting only senior crew can substitude general cabin mate but not the other way around
#pause
#v(1cm)
- Update schedules upon flight delay and cancellations

== Plan of Attack
#v(1cm)
- Replicate MICCPP-ACCS
#pause
#v(1cm)
- Constraint Programming
#pause
#v(1cm)
- Domain independent dynamic programming
#pause
#v(1cm)
- D-star algo?

// - rrt/ prm tree based


== Data Collection
$$
== Data Collection
#align(center)[
  #image("media/data.png")

  #image("media/data_sources_flowchart.svg", width: 100%)
]

#pagebreak()
#align(horizon + center)[
  #text(size: 22pt, fill: luma(80))[
    The data that has been used is confidential.\
    #v(0.3cm)
    #pause
    So is the solution.\
    #pause
    #v(0.3cm)
    So is the model.\
    #pause
    #v(0.3cm)
    So is this slide.
  ]
]

== Reference
- https://doi.org/10.1016/j.tre.2022.102763
- https://doi.org/10.1016/j.tre.2021.102304
- https://doi.org/10.1007/s13676-015-0080-x
- https://www.transtats.bts.gov/Tables.asp?QO_VQ=EFD&QO_anzr=Nv4yv0r%FDb0-gvzr%FDcr4s14zn0pr%FDQn6n&QO_fu146_anzr=b0-gvzr

- https://github.com/crewml/crewml

- https://registry.faa.gov/database/ReleasableAircraft.zip

- https://youtu.be/Ri2ecbZBqy8?si=AP2mBHovBjGL1sMu
