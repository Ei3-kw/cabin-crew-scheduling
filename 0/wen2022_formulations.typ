// ─── Wen et al. (2022) — Mathematical Formulations ───────────────────────────
// Individual scheduling approach for multi-class airline cabin crew
// with manpower requirement heterogeneity
// Transportation Research Part E 163 (2022) 102763

#set page(margin: 2.5cm)
#set text(font: "New Computer Modern", size: 11pt)
#set math.equation(numbering: "(1)")

#show heading: set text(weight: "bold")

== Assumptions and Preconditions

=== Problem scope
+ The planning horizon is one week. Short-haul and long-haul flights are always scheduled separately; the model covers one category at a time.
+ The flight schedule — including departure/arrival airports, times, aircraft types, and per-flight cabin crew requirements — is fixed and given as input. Fleet assignment and aircraft routing have already been solved upstream.

=== Crew structure
+ Cabin crews are partitioned into $|R|$ classes (e.g. stewards, hostesses, cabin mates, head cabin mates) based on skills and experience. Class membership is fixed for the planning period.
+ Each crew member is cross-qualified to serve any aircraft type in the schedule, unlike cockpit crew who are type-restricted.
+ Each crew member is assigned to exactly one home base $b in B$. Every pairing must start and end at that base.
+ The number of available crew per class $"avail"_r$ is known in advance and fixed for the week.

=== Pairing feasibility
+ A pairing is feasible only if it satisfies all regulatory constraints (maximum TAFB, maximum flights per duty, minimum rest between duties, briefing and debriefing times, etc.) as specified by labor unions, civil aviation authorities, and the airline.
+ Pairing feasibility is enforced implicitly via the duty-based network $G^r = (N_r, A_r)$ for each class $r$. The node set $N_r$ contains duty nodes plus a source and sink node both representing the home base; the arc set $A_r$ contains starting, rest, deadhead, day-off, and ending arcs. Only source-to-sink paths that respect all resource constraints are valid pairings. Since working rules are identical across classes, a single network is constructed and shared.
+ The sets $P_r$ and $P^e_r$ therefore contain only pre-verified feasible pairings. No explicit base-return or regulatory constraints appear in the IP.

=== Flight coverage and substitution
+ Every scheduled flight $f in F$ must be covered — i.e. assigned sufficient crew to meet its total manpower requirement $sum_(r in R) "req"_(r f)$.
+ At least one qualified crew member from *each* class $r$ must be present on every flight (minimum satisfaction constraint). Cross-class substitution (CCS) can only supplement this minimum, not replace it.
+ CCS is triggered only when a class is in shortage. Unnecessary substitutions are penalised by $mu$ in the objective to prevent them occurring when sufficient manpower is available.
+ When CCS alone cannot resolve a shortage (total available crew $T A < "MS"$), extra crew — temporary or part-time — may be hired at a large penalty $M$.

=== Cost model
+ Pairing cost is approximated by TAFB (Time Away From Base): the total elapsed time from departing the home base at the start of the first duty to returning at the end of the last duty. This is a simplification; real pairing costs involve non-linear components (minimum guaranteed pay, deadhead costs, short-sit penalties, etc.).
+ The penalty hierarchy $"cost" << mu << M$ ensures the solver
  - (i) first minimises pairing cost using available crew,
  - (ii) uses CCS only when necessary, and
  - (iii) hires extra crew only as a last resort.

#pagebreak()
== Sets

#table(
  columns: (auto, 1fr),
  stroke: none,
  inset: (x: 10pt, y: 4pt),
  $R$,       [Set of cabin crew classes, indexed by $r$],
  $F$,       [Set of scheduled flights, indexed by $f$],
  $B$,       [Set of home bases, indexed by $b$. Each pairing must start and end at the crew's home base],
  $T$,       [Set of potential *team* pairings for cabin crews, indexed by $t$],
  $P_r$, [Set of feasible individual pairings for Class $r$ *available* crew.
        A pairing $p in P_r$ is only generated if it starts and ends
        at the crew's home base $b in B$ — enforced by the
        duty-based network $G^r = (N_r, A_r)$ during column generation.],
  $P^e_r$,  [Set of potential *individual* pairings for Class $r$ *extra* cabin crews, indexed by $p$],
)

#v(0.5em)

== Data

#table(
  columns: (auto, 1fr),
  stroke: none,
  inset: (x: 10pt, y: 8pt),

  $"cost"_t$,
  [TAFB (Time Away From Base) cost of team pairing $t in T$],

  $"cost"^"avail"_(r p)$,
  [TAFB cost of individual pairing $p in P_r$ for Class $r$ available cabin crew],

  $"cost"^"extra"_(r p)$,
  [TAFB cost of individual pairing $p in P^e_r$ for Class $r$ extra cabin crew],

  $"covers"_(f t)$,
  $display(= cases(1 & "if team pairing" t "covers flight" f, 0 & "otherwise"))$,

  $"covers"^"avail"_(f r p)$,
  $display(= cases(1 & "if individual pairing" p in P_r "covers flight" f, 0 & "otherwise"))$,

  $"covers"^"extra"_(f r p)$,
  $display(= cases(1 & "if extra individual pairing" p in P^e_r "covers flight" f, 0 & "otherwise"))$,

  $"req"_(r f)$,
  [Number of Class $r$ cabin crews required by flight $f$],

  $"avail"_r$,
  [Total number of available Class $r$ cabin crews (upper bound on pairings generated)],

  $mu$,
  [Unit substitution penalty cost; satisfies $"cost"^"avail"_(r p),\ "cost"^"extra"_(r p) << mu << M$],

  $M$,
  [Unit big-$M$ penalty cost for employing extra cabin crew],
)

== Variables

#table(
  columns: (auto, 1fr),
  stroke: none,
  inset: (x: 10pt, y: 8pt),

  $"select"_t$,
  $display(= cases(1 & "if team pairing" t "is selected", 0 & "otherwise"))$,

  $"assign"_(r p) in ZZ_(>=0)$,
  [Number of times individual pairing $p in P_r$ is used for Class $r$ available cabin crew],

  $"extra"_(r p) in ZZ_(>=0)$,
  [Number of times individual pairing $p in P^e_r$ is used for Class $r$ extra cabin crew],

  $"sub"_(r f) in ZZ_(>=0)$,
  [Number of times Class $r$ cabin crew is substituted by another class on flight $f$],
)

#pagebreak()

== Model 1: Traditional Cabin Crew Pairing Problem (TCCPP)

The baseline model from the literature. Cabin crews are treated as homogeneous teams separated by aircraft type. Manpower availability and crew class heterogeneity are not considered.

$
"(TCCPP)" quad min quad sum_(t in T) "cost"_t dot "select"_t
$ <tccpp-obj>

subject to:

$
sum_(t in T) "covers"_(f t) dot "select"_t >= 1,
quad forall f in F
$ <tccpp-c1>

$
"select"_t in {0, 1},
quad forall t in T
$ <tccpp-c2>

=== Constraint Descriptions

+ @tccpp-obj — Minimise the total TAFB pairing cost over all selected team pairings.
+ @tccpp-c1 — Each flight $f$ must be covered by at least one selected team pairing.
+ @tccpp-c2 — Binary integrality of the team pairing selection variables.

#v(1em)

== Model 2: MICCPP-ACCS
*Multi-class Individual Cabin Crew Pairing Problem with Availability and Controlled Crew Substitution.*

Cabin crews are modelled individually by class, with heterogeneous flight requirements, crew availability limits, and the Controlled Crew Substitution (CCS) strategy to hedge against manpower shortages.

$
min quad
  underbrace(
    sum_(r in R) sum_(p in P_r) "cost"^"avail"_(r p) dot "assign"_(r p),
    "available crew cost"
  )
  + underbrace(
    sum_(r in R) sum_(f in F) mu dot "sub"_(r f),
    "substitution penalty"
  )
  + underbrace(
    sum_(r in R) sum_(p in P^e_r) ("cost"^"extra"_(r p) + M) dot "extra"_(r p),
    "extra crew cost"
  )
$ <accs-obj>

subject to:

$
sum_(r in R) sum_(p in P_r) "covers"^"avail"_(f r p) dot "assign"_(r p)
+ sum_(r in R) sum_(p in P^e_r) "covers"^"extra"_(f r p) dot "extra"_(r p)
>= sum_(r in R) "req"_(r f),
quad forall f in F
$ <accs-c1>

$
sum_(p in P_r) "covers"^"avail"_(f r p) dot "assign"_(r p)
+ sum_(p in P^e_r) "covers"^"extra"_(f r p) dot "extra"_(r p)
>= 1,
quad forall f in F,\ forall r in R
$ <accs-c2>

$
sum_(p in P_r) "covers"^"avail"_(f r p) dot "assign"_(r p)
+ sum_(p in P^e_r) "covers"^"extra"_(f r p) dot "extra"_(r p)
+ "sub"_(r f) >= "req"_(r f),
quad forall f in F,\ forall r in R
$ <accs-c3>

$
sum_(p in P_r) "assign"_(r p) <= "avail"_r,
quad forall r in R
$ <accs-c4>

$
"assign"_(r p) in ZZ_(>=0),
quad forall r in R,\ forall p in P_r
$ <accs-c5>

$
"extra"_(r p) in ZZ_(>=0),
quad forall r in R,\ forall p in P^e_r
$ <accs-c6>

$
"sub"_(r f) in ZZ_(>=0),
quad forall f in F,\ forall r in R
$ <accs-c7>

=== Constraint Descriptions

+ @accs-obj — Three-part objective: (i) total TAFB cost of available-crew pairings selected; (ii) total substitution penalties penalising cross-class substitutions; (iii) total cost of extra crew pairings with big-$M$ penalty ensuring extra crews are used only as a last resort.
+ @accs-c1 — *Total satisfaction constraint* (Group 1): the total number of cabin crews of all classes assigned to flight $f$ must meet the total demand across all classes. This is the mechanism enabling CCS — a surplus in one class can cover a shortfall in another.
+ @accs-c2 — *Minimum satisfaction constraint* (Group 2a): at least one qualified crew member from *each* class must be assigned to every flight $f$.
+ @accs-c3 — *Substitution recording constraint* (Group 2b): tracks the number of times Class $r$ is substituted on flight $f$. Each substitution incurs penalty $mu$, discouraging unnecessary substitutions when sufficient manpower is available.
+ @accs-c4 — *Crew availability constraint* (Group 3): limits the total number of individual pairings assigned to Class $r$ available crew, approximating the workforce headcount.
+ @accs-c5 to @accs-c7 — Non-negativity and integrality of all decision variables (Group 4).

#pagebreak()

== Model 3: Simplified MICCPP-A

A simplified version of MICCPP-ACCS where CCS is *forbidden*. Each class is scheduled independently. Used to derive manpower requirement benchmarks $"MC"_r$ and $"MM"_r$.

$
"(MICCPP-A)" quad min quad
  sum_(p in P_r) "cost"^"avail"_(r p) dot "assign"_(r p)
  + sum_(p in P^e_r) ("cost"^"extra"_(r p) + M) dot "extra"_(r p)
$ <a-obj>

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

=== Constraint Descriptions

+ @a-obj — Minimise total TAFB pairing cost plus big-$M$ penalty for extra crew employment. No substitution term since CCS is forbidden.
+ @a-c1 — Each flight $f$ must be served by sufficient Class $r$ cabin crews (available and extra combined) to meet the per-class requirement $"req"_(r f)$.
+ @a-c2 — Limits the number of available Class $r$ pairings to $"avail"_r$.
+ @a-c3 to @a-c4 — Non-negativity and integrality of decision variables.

#v(1em)

== Manpower Requirement Benchmarks

Three benchmarks characterise the minimum manpower needs of a given flight schedule:

#table(
  columns: (auto, auto, 1fr),
  stroke: none,
  inset: (x: 8pt, y: 6pt),
  [*Benchmark*], [*Formula*], [*Definition*],

  $"MS"$,
  $display(sum_(r in R) sum_(p in P^e_r) "extra"_(r p))$,
  [Minimum *total* manpower demand *with* CCS. From MICCPP-ACCS with $"avail"_r = 0$ for all $r$.],

  $"MC"_r$,
  $display(sum_(p in P^e_r) "extra"_(r p))$,
  [Minimum manpower demand for Class $r$ *without* CCS. From MICCPP-A with $"avail"_r = 0$.],

  $"MM"_r$,
  $display(sum_(p in P^e_r) "extra"_(r p))$,
  [Minimum satisfaction constraint demand for Class $r$. From MICCPP-A with $"avail"_r = 0$ and $"req"_(r f) = 1$ for all $f in F$.],
)

The relationship $"MM"_r <= "MC"_r$ always holds; equality occurs only when all flights require exactly one Class $r$ crew member. Letting $T A = sum_(r in R) "avail"_r$ denote total available crew across all classes, comparing $T A$ and each $"avail"_r$ against $"MS"$, $"MC"_r$, and $"MM"_r$ determines whether a manpower shortage exists and whether CCS or extra hiring is required.
