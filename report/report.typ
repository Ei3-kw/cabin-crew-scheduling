// Compile with: typst compile report.typ
// If the Introduction is the top-level chapter, promote headings one level
// (== -> =, === -> ==) for "1", "1.1", ... numbering.

#set page(paper: "a4", margin: 2.3cm)
#set text(font: "New Computer Modern", size: 10.5pt, lang: "en")
#set par(justify: true, leading: 0.6em, spacing: 0.95em, first-line-indent: 0pt)
#set heading(numbering: "1.1")
#show heading: set block(above: 1.1em, below: 0.6em)
#show raw: set text(size: 9pt)

== Introduction

I have always been a little fascinated by flight crews: the idea of being paid to
fly somewhere and then getting to travel once you land (at least that is what I
imagined the job to be). That curiosity is what drew me to this report, which aims
to reproduce and potentially improve upon the results of the paper "Individual
scheduling approach for multi-class airline cabin crew with manpower requirement
heterogeneity" #footnote[X. Wen, S.-H. Chung, P. Ji, and J.-B. Sheu,
_Transportation Research Part E_, vol. 163, 102763, 2022.] [1]. This report focuses
on the integer programming aspects of the paper.

=== Background Problem

The paper tackles the cabin crew pairing problem (CPP): building legal sequences of
flights (pairings) that start and end at a home base and cover every flight, while
minimising cost. Crew cost is an airline's second-largest expense after fuel, so
small scheduling gains matter. Cockpit crews are qualified for one aircraft type
with fixed requirements and are scheduled as teams. Cabin crews are different: they
are split into multiple classes, are cross-qualified across aircraft types, and the
number of each class required varies by aircraft, cabin layout, and flight. This
heterogeneity is the core difficulty.

Modelling cabin crews as teams is wasteful here. Since a team flies together, its
size must meet the maximum requirement across all flights on the pairing, leaving
surplus crew idle on lighter flights (manpower waste). Scheduling crew individually
avoids this and raises utilisation. The paper also formalises Controlled Crew
Substitution (CCS): when flight fluctuation leaves a class short, a crew member of
another class temporarily covers the duty, so a flight is fine as long as total crew
meets total demand. Combining these ideas gives the proposed MICCPP-ACCS model,
which schedules each crew individually, adds per-class availability limits, and
embeds CCS.

=== Original Paper's methodology

The paper's contribution is as much conceptual as computational: it argues that
cabin crews should be scheduled as individuals rather than as fixed teams, and backs
this with three integer programming models. The traditional model (TCCPP) is the
team-based baseline the literature has long used. The proposed model (MICCPP-ACCS)
schedules each crew member of each class individually, caps how many crew each class
has available, and embeds Controlled Crew Substitution so a surplus class can
temporarily cover a short one. A stripped-down variant (MICCPP-A) drops substitution
and is used mainly to derive benchmarks. In every case the objective is driven by
time-away-from-base, with substitution and extra hiring penalised heavily enough
that the model only reaches for them when genuinely short of crew.

From these models the paper derives manpower-requirement benchmarks for a schedule
and uses them to sort any availability situation into a handful of scenarios,
showing when substitution alone suffices and when extra crew must be hired. The
models are solved with a column-generation heuristic: pairings are generated on
demand by solving shortest-path subproblems over a duty network, and integer
solutions are recovered by running a MIP over only the generated columns, with a
genetic algorithm standing in for the largest instances. Experiments on a Hong
Kong--Singapore route and on large synthetic instances report that the individual
approach removes idle-crew waste and lowers cost relative to the team baseline, with
substitution further cutting the need for extra hires.

=== Limitations of the original models

Three modelling choices stand out as limitations. First, the formulation plans
around a single home base (Hong Kong): availability is just a cap on pairings per
class, and any shortage is filled by "extra" crew that the model can effectively
import from anywhere at a fixed penalty. This sidesteps the real geography of crew
bases and positioning, so the availability constraint is a rough proxy rather than a
true resourcing model. Second, the model lets any class substitute any other class
in either direction. If substitution is unrestricted, it is unclear what the class
distinction is buying; in practice higher classes cover lower ones, not the reverse,
and free two-way substitution risks overstating the flexibility CCS delivers. Third,
cost is approximated purely by time-away-from-base, treating every minute of a
pairing as equal. In reality the segments of a duty cost differently --- active
flight time, ground waiting (sit) time, and deadhead repositioning are not
interchangeable --- so a single TAFB figure can misrank pairings an airline would
price very differently.

== Problem Setups

The instances are rebuilt from public data so the study is reproducible without the
paper's confidential figures.

=== Flight Data Source

Flights come from the BTS On-Time Performance dataset (tail number, origin /
destination, scheduled times, delay, cancellation). Aircraft attributes are taken
from the FAA registry and attached by a left join on the tail number, giving each
flight $f$ a seat count $s_f$. The seat count resolves from the per-tail registry
value, falling back to a model$->$seats table when missing.

The minimum cabin crew $m_f$ is the regulatory floor (14 CFR 121.391):

$ m_f = cases(
  1 & "if" s_f <= 50,
  2 & "if" 50 < s_f <= 100,
  2 + ceil((s_f - 100) \/ 50) & "if" s_f > 100
) $

which yields a heterogeneous per-flight requirement, as in the paper, but from open
data. (See the appendix for one enriched record.)

=== Crew Base Allocation

The paper's single base is replaced by one base per airport the airline both departs
from and arrives at. With $cal(O)$ the set of origin airports and $cal(D)$ the set
of destinations,

$ B = cal(O) inter cal(D) . $

Each base $p in B$ is sized from two signals. Let $d_f$ be flight duration and $u in
(0,1]$ a utilisation discount over an $H$-day horizon; the per-crew duty capacity and
the duration-weighted demand (with a slack factor $1.8$) are

$ tau_"duty" = 8 dot 60 dot H dot u, quad quad
  n_p^"dem" = ceil( 1.8/tau_"duty" sum_(f : "orig"(f) = p) m_f thin d_f ) . $

Writing $L_p (t)$ for the simultaneous crew load at $p$ (a sweep adding $+m_f$ at each
departure and $-m_f$ at each arrival), the peak signal and final count are

$ n_p^"peak" = ceil( 1.8 dot max_t L_p (t) ), quad quad
  n_p = "clip"( "round"( cal(N)(mu_p, (0.1 mu_p)^2) ), thin n_"min", thin n_"max" ), $

where $mu_p = max(n_p^"dem", n_p^"peak", n_"min")$ and the Gaussian draw mimics
real over-provisioning. Availability is thus demand-proportional and location-aware,
rather than an unlimited pool imported from anywhere.

=== Assumptions

The upstream stages (scheduling, fleet assignment, aircraft routing) are fixed
inputs; the planning horizon is $T = 1$ week; every pairing starts and ends at its
crew member's base; and crew are interchangeable within a class, subject to the
substitution rules. Disruption response and the early alternative solvers are out of
scope here and discussed later.

=== Rolling Horizon

The dataset spans more than one horizon, so planning advances in successive windows
of length $L = T$ with step $Delta$:

$ W_k = [ k Delta, thin k Delta + L ), quad k = 0, 1, 2, dots $

Each $W_k$ is solved as a static instance and its assignments committed before
advancing to $W_(k+1)$, keeping every solve tractable and leaving a seam where later
disruption handling can attach.

#pagebreak()

== Appendix: Enriched Record Format

One flight after the BTS$->$FAA join and crew derivation. Times are minutes since
local midnight; #raw("duration") is scheduled elapsed time.

```json
{
  "flight_id":     "AA0001_2025-01-01",
  "carrier":       "AA",
  "flight_number": 1,
  "date":          "2025-01-01",
  "origin":        "JFK",
  "dest":          "LAX",
  "dep_min":       419,
  "arr_min":       620,
  "duration":      381,
  "tail_num":      "N104NN",
  "aircraft": {
    "manufacturer": "Airbus",
    "model":        "A321-231",
    "seats":        185,
    "seat_source":  "faa_registry"
  },
  "min_crew":      4,
  "cancelled":     false
}
```

A crew member carries only an id and its home base; its class is assigned by the
model.

```json
{ "id": 0, "base": "JFK", "class": "general" }
```
