// Compile with: typst compile introduction.typ
// If this Introduction is the top-level chapter of your report, promote the
// headings one level (== -> =, === -> ==) to get "1", "1.1", "1.2" numbering.

#set page(paper: "a4", margin: 2.3cm)
#set text(font: "New Computer Modern", size: 10.5pt, lang: "en")
#set par(justify: true, leading: 0.6em, spacing: 0.95em, first-line-indent: 0pt)
#set heading(numbering: "1.1")
#show heading: set block(above: 1.1em, below: 0.6em)

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

The paper builds three integer programs. TCCPP is the traditional team-based
set-covering model minimising time-away-from-base (TAFB) with no availability
limit. MICCPP-ACCS is the proposed individual model whose objective orders pairing
cost, a substitution penalty, and a heavily penalised extra-crew cost so that
available crew are used first, CCS only on real shortage, and extra crew last; its
constraints handle total satisfaction, per-class minimum satisfaction, substitution
recording, and availability. MICCPP-A removes CCS and is used to derive three
benchmarks (MS, MC#sub[r], MM#sub[r]) that feed an eight-scenario case analysis of
when CCS or extra crew are needed. Solutions come from a column generation plus MIP
heuristic (with a DPIA initialiser and a labelling algorithm for the pricing
problem), and from a Genetic Algorithm on large instances. Experiments on a Hong
Kong--Singapore route and on large hypothetical instances show MICCPP-ACCS
eliminates idle crews, lowers cost against both traditional variants, and uses CCS
to cut extra-manpower demand.

=== Limitations of the original models

Two modelling choices stand out as limitations. First, the formulation plans around
a single home base (Hong Kong): availability is just a cap on pairings per class,
and any shortage is filled by "extra" crew that the model can effectively import
from anywhere at a fixed penalty. This sidesteps the real geography of crew bases,
positioning, and where spare crew actually are, so the availability constraint is a
rough proxy rather than a true resourcing model. Second, the model lets any class
substitute any other class. If substitution is unrestricted in both directions, it
is unclear what the class distinction is buying. In practice higher classes
substitute lower ones, not the reverse; allowing free two-way substitution risks
overstating the flexibility (and the savings) that CCS delivers. Third, cost is
approximated purely by time-away-from-base, treating every minute of a pairing as
equal. In reality the segments of a duty cost differently: active flight time,
ground waiting (sit) time, and deadhead repositioning are not interchangeable, so a
single TAFB figure can misrank pairings that an airline would price very
differently.
