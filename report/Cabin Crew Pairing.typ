#set heading(numbering: "1.1")

#let notation(..rows) = table(
  columns: (auto, 1fr),
  stroke: none,
  column-gutter: 1.2em,
  inset: (x: 0pt, y: 3pt),
  align: (left + top, left + top),
  ..rows.pos()
)

#align(center)[
  #v(15em)
  #text(18pt, weight: "bold")[
    Cabin Crew Pairing with a Break-Enforcing Time-Expanded Flow Network
  ]
  #v(0.8em)
  #text(11pt)[University of Queensland]
  #v(0.3em)
  #text(11pt)[#emph[Ella WANG]]
  #v(1.5em)
  #text(11pt)[June 2026]
]

#pagebreak()

= Introduction

When I was a kid I thought flight attendants had the best job in the world. They flew for free, they got to walk around the plane while everyone else sat strapped in, they had a whole galley of food within arm's reach, and they were paid to travel everywhere. It looked like someone had taken the fun parts of a holiday and handed them over as a salary.

What I never once thought about was how any of them ended up on a particular flight. I only started wondering after watching a video on how airlines actually schedule their crew — the bidding, the trip assignments, the rules stacked on top of rules — and coming away thinking the whole thing looked kind of broken for something so important. Underneath that mess is a real and surprisingly hard optimization problem, and that is what pulled me into this report.

This report reproduces, and tries to improve on, the individual cabin-crew scheduling model of Wen, Chung, Ji, and Sheu @wen2022individual, focusing on its integer-programming core.

== Background Problem

Crew is an airline's second-largest operating cost, behind only fuel, so how flights are staffed has a real effect on profitability. The cabin-crew pairing problem asks for a set of legal duty sequences, or _pairings_, that each begin and end at a crew member's home base, respect working-time rules, and together cover every scheduled flight at minimum cost. Cabin crew are harder to schedule than cockpit crew because they are heterogeneous, cross-qualified across aircraft types and split into classes, and how many of each class a flight needs depends on the aircraft's size and cabin layout. A regional turboprop might need a single attendant, while a widebody needs eight.

We model this in a deliberately simple form. Each flight needs between one and eight crew. Exactly one must be a _senior_, the rest can be anyone. A senior can fill any seat, but a normal can never fill the senior's. The senior is therefore the scarce, binding resource, the thing that decides whether a flight can be staffed at all.

A pairing is legal only if it respects the operational clocks the model tracks. Two consecutive flights need at least a 45-minute turnaround between them. A duty period can build up at most 14 hours of block time before an 8-hour rest clears it. And a crew member can stay away from base for at most 4 days before owing a 48-hour home break.

We also model the real geography. Crew are based at many airports, can only originate and terminate duties at their own base, and must be physically positioned, by flying or deadheading, to wherever a flight departs. Coverage is thus limited not just by how many crew exist, but by whether the right class can legally reach the right place at the right time. That is the central tension the rest of this report addresses.

== Original Paper's Methodology

Modelling cabin crews as teams is wasteful. A team flies together, so its size must meet the maximum requirement across all flights on the pairing, leaving surplus crew idle on the lighter ones. Scheduling crew individually avoids this and raises utilisation. The paper also formalises _Controlled Crew Substitution_ (CCS): when demand fluctuation leaves a class short, a crew member of another class temporarily covers the duty, so a flight is covered as long as total crew meets total demand. Combining these gives the proposed MICCPP-ACCS model, which schedules each crew individually, adds per-class availability limits, and embeds CCS.

Concretely, the paper builds three integer programs. TCCPP is the traditional team-based set-covering model, minimising time-away-from-base (TAFB) with no availability limit. MICCPP-ACCS is the proposed individual model whose objective orders pairing cost, a substitution penalty, and a heavily penalised extra-crew cost, so available crew are used first, CCS only on real shortage, and extra crew last; its constraints handle total satisfaction, per-class minimum satisfaction, substitution recording, and availability. MICCPP-A removes CCS and is used to derive three benchmarks that feed an eight-scenario analysis of when CCS or extra crew are needed. Solutions come from a column-generation-plus-MIP heuristic (with a DPIA initialiser and a labelling algorithm for the pricing problem) and, on large instances, a genetic algorithm. Experiments on a Hong Kong–Singapore route and on large hypothetical instances show MICCPP-ACCS eliminates idle crews, lowers cost against the team-based and no-substitution variants, and uses CCS to cut extra-manpower demand.

== Limitations We Respond To

Three modelling choices in the original motivate the changes this report makes.

First, the formulation plans around a *single home base* (Hong Kong). Availability is just a cap on pairings per class, and any shortage is filled by "extra" crew that the model can effectively import from anywhere at a fixed penalty. This sidesteps the real geography of crew bases, positioning, and where spare crew actually are, so the availability constraint is a rough proxy rather than a true resourcing model. We replace the import penalty with explicit multi-base geography: an unmet seat is then a real positioning failure — no crew could legally reach it — rather than a fixed import fee.

Second, the model lets *any class substitute any other*. If substitution is unrestricted in both directions it is unclear what the class distinction buys; in practice higher classes substitute lower ones, not the reverse, and free two-way substitution risks overstating the flexibility CCS delivers. We restrict it to one direction: a senior may take a normal seat, but a normal can never take the senior's, which keeps the senior the genuinely binding resource.

Third, cost is approximated purely by *time-away-from-base*, treating every minute of a pairing as equal. In reality the segments of a duty are priced differently — active flight time, ground waiting, and deadhead repositioning are not interchangeable — so a single TAFB figure can misrank pairings an airline would price very differently. We split cost into per-minute flight, wait, and deadhead rates (the last load-scaled by a displaced-seat fare), so the objective reflects what each segment actually costs.

= Problem Setups
== Flight Data Source

The flight schedule is real U.S. domestic data from the Bureau of Transportation Statistics (BTS) on-time performance dataset, which lists for every flight $f$ its operating carrier, tail number, origin and destination, scheduled departure and arrival times, and distance. What BTS does not record is how many cabin crew a flight needs, so we derive it from the aircraft itself. Each tail number is joined against the FAA Aircraft Registry to recover the aircraft model and its seat count $s_f$ — the per-tail registered value where available, otherwise a type-level estimate from FAA Type Certificate Data Sheets — and $s_f$ is mapped to a minimum cabin crew $r_f$ under U.S. regulation 14 CFR 121.391, where one attendant is required for every 50 seats.

$ r_f = ceil(s_f \/ 50) $

After enrichment the dataset spans 21 carriers, from mainline operators down to small regionals. This gives a natural range of network sizes and crew requirements to test against. We treat each carrier independently, with its own flights, airports, and crew pool, so an airline is one self-contained instance. The planning period is 30 days, extended by a return tail to a 34-day horizon so crew committed late can still be routed home. Only flights departing within the planning period must be covered. The rest sit in the tail.

The crew requirement is uneven across carriers, but within a single regional carrier it is essentially constant. A regional flies a near single-class fleet, one aircraft type and one seat band, so every flight maps to the same $r_f$. G7 is two attendants throughout (its regional jets seat 51 to 100), as are ZW and QX. Real per-flight variation appears only at mainline scale, where the fleet spans seat classes. B6 ranges over two to four, HA three to six, and AA three to eight. Size and density climb the same way. The regionals are small and sparse. G7 touches 51 airports over 143 routes, ZW 44 over 93. A mainline such as AA spans 119 airports and 836 routes at several times the density (@instance-table).

This leaves an awkward trade-off when picking test instances. The carriers with genuinely varied requirements (B6, HA, AA) are exactly the ones too large to solve in reasonable time. The small regionals that do solve have a flat requirement that never stretches the senior/normal split past a fixed one-plus-one. So we use two complementary instances. On the real data we focus on G7. It is a regional small enough to solve, and its true requirement of two (one senior, one normal) still puts the full two-layer machinery to work on a genuine schedule. For varied demand we keep that same small ZW network but swap its requirement for a random $r_f$, uniform over 1 to 8 (`data/flights_2025-01-random.csv`). That gives the mixed demand of a mainline on a network small enough to solve. It exercises cancellation, multi-seat fill, and substitution in ways the uniform regionals never would.

== Two-Layer Structure

Each flight needs one senior plus $r_f - 1$ normals. Rather than solve for both at once, we run two passes that each place a single crew member per flight. This is about tractability. The crew-flow relaxation is integral only at unit demand, so the barrier method reaches an integer optimum without branching. The moment a flight needs two or more, the coverage constraints tie the flow across crew groups together, the relaxation turns fractional, and the solver has to branch. Treating the seniors (one per flight) and the normals ($r_f - 1$ per flight) as separate unit-demand passes keeps both in that easy regime. We solve the seniors first because they are the binding resource, and the normals then fill in around the schedule the seniors fix.

=== Senior Layer (1 senior per flight)

Pass one stamps every flight with demand one and solves over a dedicated senior pool. Each flight gets exactly one senior, or none if none can legally reach it. A flight that gets no senior is _cancelled_: a normal can never fill the senior seat, so it is dropped and reported uncovered. The rest pass to the normal layer.

=== Normal Fill Layer ($r_f - 1$)

Pass two takes those surviving flights, re-stamps each with demand $r_f - 1$, and solves over a separate normal pool. Flights with $r_f = 1$ are already complete and carry no demand here. $r_f = 2$ stays unit-demand, higher values give small multiplicity. Normal identifiers are offset so they never collide with senior ones on merge.

== Crew Base Allocation

Where crew are based, and how many to base at each, is a single heuristic run once per layer. The base set is every airport the carrier flies both to and from. Each such airport is a full crew base. For a base $b$ we estimate two loads. The first is its duration-weighted demand, the crew-minutes that originate there.

$ "dem"_b = sum_(f : "orig"_f = b) m_f delta_f $

where $m_f$ is the layer's per-flight demand (one for the senior pass, $r_f - 1$ for the normal pass) and $delta_f$ the block duration. The second is the peak concurrent load $"peak"_b$, the largest number of crew on duty at $b$ at once, found by sweeping the day and adding $m_f$ at each departure, removing it at each arrival.

The pool must exceed the raw demand, since crew are not available the whole horizon. An 8-hour overnight rest separates duty days, and a 48-hour home break follows every rotation of at most 4 days away, so over a six-day cycle only about four days are workable. A utilisation factor $u = 0.55$ absorbs these overheads. It is hand-tuned rather than derived, set empirically so busy bases are not undersized. For sizing we credit each crew a nominal eight duty-hours a day, well below the enforced 14-hour cap, so over the $|D|$-day horizon one crew supplies $tau = 8"h" times |D| times u$ duty-minutes. This eight-hour figure is a sizing assumption only. The schedules themselves are bound by the 14-hour duty limit. The base size is then

$ s_b = max(ceil(1.8 thin "dem"_b \/ tau), thin ceil(1.8 thin "peak"_b), thin s_(m i n)) $

a $1.8$ slack over the larger load, floored at a small per-base minimum $s_(m i n)$, with a 10% Gaussian jitter on each count. The $1.8$ covers positioning (a crew often deadheads to where it is needed) and short demand peaks. It is ample. The pools supply two to nearly five times the crew-minutes flown (a coverage ratio of $2.8$ for G7, around four for the randomised ZW), so headcount is never binding. Coverage is limited by geography and legality, not numbers, so the slack could even be tightened to shrink the model.

Running this twice, on the senior demand and then the normal demand, gives two pools each matched to its own layer. Sizing one pool and splitting it by a fixed ratio was tried first but mismatched both layers.

= Formulations
#let defrow(sym, desc) = (align(right, sym), align(left, desc))

#let deftable(rows) = table(
  columns: (auto, 1fr),
  align: (right, left),
  stroke: none,
  inset: (x: 6pt, y: 4pt),
  ..rows.flatten()
)

== Sets

#deftable((
  defrow($P$,            [All airports]),
  defrow($A$,  [All airlines]),
  defrow($B_a subset.eq P$,[Crew home bases of airline $a in A$ (= every airport with flights to \& from)\ We use $B$ from here on, as we look at one airline at a time]),
  defrow($S_b$,            [senior crew members based at base $b in B$]),
  defrow($N_b$,            [normal crew members based at base $b in B$]),
  defrow($L$,             [Levels of crew $cases(1 "Senior", 0 "Normal")$]),
  defrow($F$,             [All flights]),
  defrow($W$,             [Ordered solve windows (rolling horizon)]),
  defrow($F_w subset.eq F$, [Flights requiring coverage in window $w in W$]),
  defrow($F_(w') subset.eq F$, [Flights requiring coverage in the half-day seam following window $w in W$]),
  defrow($tilde(F)_w subset.eq F$, [All legs touching window $w$: $t_w^"start" <= "dep"_f < t_w^"hor"$  ($F_w, F_(w') subset.eq tilde(F)_w$)]),
  defrow($D$,             [Days (0--30)]),
))

#pagebreak()
== Data

#deftable((
  defrow($delta_f$,     [Duration (minutes) of flight $f in F$]),
  defrow($"dist"_f$, [Great-circle distance (miles) of flight $f in F$]),
  defrow($"orig"_f$,     [Departure (origin) port of flight $f in F$]),
  defrow($"dep"_f$,    [Departure time of flight $f in F$]),
  defrow($"dest"_f$,     [Arrival (destination) port of flight $f in F$]),
  defrow($"arr"_f$,    [Arrival time of flight $f in F$]),
  defrow($l_f$,         [Passenger load factor $in [0,1]$ of flight $f in F$]),
  defrow($r_f$,         [Required working crew count of flight $f in F$]),
  defrow($s_b$,         [Crew supply at base $b in B$]),
  defrow($s_(m i n)$,   [Minimum crew at any base $b in B$]),
  defrow($c_(f l,1) = 420$,   [Cost of senior crew flying per min]),
  defrow($c_(f l,0) = 100$,   [Cost of normal crew flying per min]),
  defrow($c_(d h, f) = delta_f dot c_(f l) + "fare"_f dot phi(l_f)$,
       [Deadhead cost = labor + load-scaled seat opp-cost $>$ flight cost]),
  defrow($"fare"_f = c_("base") + c_(m i) dot "dist"_f$,
         [Displaced-seat fare as a function of flight distance]),
  defrow($c_("base") = 50$, [Base fare]),
  defrow($c_(m i) = 0.15$, [Fare per mile]),
  defrow($phi(l_f) = cases(
    0 & "if" l_f <= l_("lo"),
    1 & "if" l_f >= l_("hi"),
    (l_f - l_("lo"))/(l_("hi") - l_("lo")) & "otherwise"
  )$, [Load-factor opportunity-cost scale, $phi: [0,1] -> [0,1]$]),
defrow($l_("lo") = 0.75$, [Load factor below which opp-cost is zero]),
defrow($l_("hi") = 0.90$, [Load factor above which opp-cost is full fare]),
  defrow($c_(w t,1) = 1$,     [Cost of senior crew waiting per min]),
  defrow($c_(w t,0) = 0.5$,   [Cost of normal crew waiting per min]),
  defrow($c_(o v) = 500$,     [Overnight flat penalty, wait $gt.eq 4$ h]),
  defrow($c_(u n c) = 10^8$,  [Penalty per uncovered slot in the committed window]),
  defrow($c_(u n c') = 10^6$,[Penalty per uncovered slot in the half-day seam following the window]),
  defrow($Delta_(t a) = 45$,    [Minimum turnaround (min) when connecting flights]),
  defrow($Delta_(r e s t) = 8$, [Minimum rest (h) before next duty]),
  defrow($Delta_(d u t y) = 14$, [Maximum on-duty time (h) between 8h breaks]),
  defrow($Delta_(h b) = 48$,    [Minimum home break (h)]),
  defrow($Delta_(o v) = 4$, [Overnight threshold (h): a wait $>= Delta_(o v)$ triggers $c_(o v)$]),
  defrow($d_(w o r k) = 3$,    [Consecutive-duty-day bound — a graph-pruning aid, not an enforced limit]),
  defrow($d_(a w a y) = 4$,        [Maximum number of days without a home break]),
  defrow($T_(d a y s) = 7$,    [Solve window length (days)]),
  defrow($T_(c o m m i t) = 3$,[Committed per step (days)]),
  defrow($T_(t a i l) = 4$,    [Return tail (days) $= d_(a w a y)$]),
  defrow($T_("look") = 0.5$, [Seam look-ahead (days, $= 720$ min): soft coverage past $t_w^"commit"$]),
  defrow($m_f$, [Per-flight demand for the layer being solved, $1$ if $ell = 1$, else $r_f - 1$]),
  defrow($rho = 0.30$, [Home-return deadhead discount, applied when $"dest"_f = b$]),
))

== Network
Times are integer minutes from the horizon start. One day $= 1440$ min, and $"floor"_"day"(x) = 1440 floor(x \/ 1440)$ rounds a deadline down to a whole day.
#deftable((
  defrow($n = (p, t, e)$, [State node, airport $p in P$, minute $t$, break-expiry $e$ (latest minute the next $>= Delta_(h b)$ home break may finish)]),
  defrow($cal(N)_w$, [All nodes of window $w$'s time-expanded network]),
  defrow($cal(A)_w$, [All arcs, partitioned $cal(A)_w = cal(A)^"fl" union cal(A)^"dh" union cal(A)^"wt"$ (flight / deadhead / wait)]),
  defrow($delta^+(n), delta^-(n)$, [Arcs leaving / entering node $n$]),
  defrow($Delta t_a$, [Elapsed minutes of arc $a$]),
  defrow($c_a$, [Cost of arc $a$ (defined piecewise in the objective)]),
  defrow($cal(G)_w$, [Clock-groups, crew sharing an identical expanded graph, $g in cal(G)_w$]),
  defrow($K_g = |g|$, [Number of interchangeable crew in group $g$]),
  defrow($cal(N)_g, cal(A)_g$, [State nodes and expanded arcs available to group $g$]),
  defrow($cal(A)_(g,f)^"fl"$, [Flight arcs of leg $f$ within group $g$]),
  defrow($"src"_g, "snk"_g$, [Depot state and collapsed horizon sink of group $g$]),
))

== Variables
The model is solved once for each $ell in L$. The two instances share all structure —
same $cal(N)_w$, $cal(A)_w$, $cal(G)_w$, variables $x_(g,a)$ and $u_f$, and rows — and
differ only in their data, the pool ($S_b$ or $N_b$), the demand $m_f$, and the rates
$c_(f l,ell)$, $c_(w t,ell)$. Layer $0$ (normals) additionally runs only over legs that
received a senior in layer $1$.
#deftable((
  defrow($x_(g,a) in {0, ..., K_g}$, [Crew flow, number of group-$g$ crew traversing arc $a in cal(A)_g$]),
  defrow($u_f in {0, ..., m_f}$,     [Uncovered slack on leg $f in F_w union F_(w')$]),
))

== Rolling Horizon

The horizon $D$ is swept by the ordered windows $W$. Window $w$ runs
$ t_w^"start" = w thin T_(c o m m i t) dot 1440, quad
  t_w^"commit" = t_w^"start" + T_(c o m m i t) dot 1440, quad
  t_w^"hor" = t_w^"start" + (T_(d a y s) + T_(t a i l)) dot 1440 $
and its legs split by departure into
$ tilde(F)_w &= {f in F : t_w^"start" <= "dep"_f < t_w^"hor"}, \
  F_w &= {f in tilde(F)_w : "dep"_f < t_w^"commit"}, \
  F_(w') &= {f in tilde(F)_w : t_w^"commit" <= "dep"_f < t_w^"commit" + 1440 T_("look")} $

Each $w in W$ is solved as the single-window model below, in order. Only $F_w$ is committed (frozen and written out). Legs after $t_w^"commit"$ are re-solved by later windows.

=== Seaming
Two boundary terms close the seam. $T_(t a i l) = d_(a w a y)$ leaves room to route every committed crew home within the away cap, and the soft set $F_(w')$ pulls crew into position for the next window's first bank.

The seam exists because of where coverage fails. In a trial run on ZW, $14$ of $20$ uncovered legs fell in the first $8$ h of a window. A leg leaving a spoke early on day $t_w^"start"$ can only be worked by a crew already rested there, but the deadhead that positions them must depart the evening before — inside the previous window, which no longer sees the leg as its responsibility and so never pre-positions. Marking these legs as $F_(w')$ gives that previous window a reason to commit the positioning deadhead in its own commit region, so the next window inherits a rested crew and covers the leg for real. The penalty is deliberately soft, $c_(u n c') = 10^6$: above the positioning cost (deadhead $+$ wait $+$ leg, $tilde.eq 1.5 times 10^4$) so the solver does pre-position, but far below $c_(u n c) = 10^8$ so it never sacrifices a committed cover to chase a seam one. Seam legs are re-solved by the next window, so they never count as uncovered here.

=== Window Carry-over
At each seam the next window starts from the committed state of the previous one — the only link between windows. There is no constraint coupling them, so window $w + 1$'s depot is pinned to where $w$'s frozen route left each crew, and the boundaries match by construction. Two things carry across, each crew's committed end position — the airport it occupies when the committed region closes — and its break-clock state, summarised by a single anchor, the minute the crew last left home after a completed home break, from which the away budget keeps counting. The subtlety is what counts as a completed break. Only a 48-hour home stay whose full 48 hours elapse inside the committed region advances the anchor. A break the solver schedules in the uncommitted tail is deliberately not credited, because the tail is re-planned by the next window and that break may never actually be flown. A crew that is home at the seam but has served only part of its break carries the start of that home stay forward, so the next window finishes the remaining hours rather than restarting a fresh 48 — this neither forces an early return nor grants a free reset.

== Network Construction

Built per airline $a in A$, per window $w in W$, and per crew level $ell in L$. The
model is a *time-expanded crew-flow network* $G_w = (cal(N)_w, cal(A)_w)$ whose nodes
are airport--time events and whose arcs are the legal crew movements. The duty,
turnaround, rest, away-cap, and home-break rules are baked into $G_w$ during
construction, so the optimisation itself carries no rows for them.

=== Build stages
+ *Event nodes.* With $B_w^+$ the bases $B$ plus every carry-over start airport,
  $ cal(N)_w = {(p, t_w^"start"), (p, t_w^"hor") : p in B_w^+}
    thick union thick union.big_(f in tilde(F)_w) {("orig"_f, "dep"_f), ("dest"_f, "arr"_f), ("dest"_f, "arr"_f + Delta_(t a))}, $
  with the nodes at each airport ordered by time.

+ *Wait arcs.* Chain time-consecutive nodes at each airport:
  $ cal(A)^"wt" = { (p, t_i) -> (p, t_(i+1)) : p in P, thick (p,t_i), (p,t_(i+1)) "consecutive in" cal(N)_w }. $
  Any $a in cal(A)^"wt"$ with $Delta t_a >= 60 Delta_(r e s t)$ resets the duty accumulator.

+ *Flight and deadhead arcs.* For all $f in tilde(F)_w$ whose origin node exists, let
  $"snap"(f) = min {t : ("dest"_f, t) in cal(N)_w, thick t >= "arr"_f + Delta_(t a)}$ (turnaround). If the duty at $("orig"_f, "dep"_f)$ plus $delta_f <= 60 Delta_(d u t y)$, add the parallel arcs
  $ ("orig"_f, "dep"_f) -> ("dest"_f, "snap"(f)), quad forall f in tilde(F)_w, $
  a *flight* arc at $c_(f l,ell) delta_f$ (earns coverage) and a *deadhead* arc at $c_(d h,f)$ (does not, scaled by $1 - rho$ when $"dest"_f = b$).

+ *Reachability pruning.* For all crew $c$ with depot $o_c$ and home base $b_c$,
  $ cal(A)_c = { a in cal(A)_w : "both ends of" a in "Fwd"(o_c) inter "Bwd"(b_c, t_w^"hor") }, $
  the forward/backward Dijkstra reachable sets, dropping mid-sweep any arc that breaches the away cap $d_(a w a y)$. The sweep also cuts runs past $d_(w o r k)$ consecutive duty days — not a rule but a size bound (see Structural Constraints).

+ *Break-clock expansion.* Lift every reachable event $(p, t)$ to a state $(p, t, e)$. The extra coordinate $e$ is a _deadline_, the latest minute by which the crew's next home break must finish, and it is how the away cap and home break are baked into the graph instead of carried as constraints. A home stay of at least $Delta_(h b)$ earns a fresh budget, so a free reset arc re-anchors the deadline to
  $ e <- "floor"_"day"(t' + 1440 d_(a w a y) + 60 Delta_(h b)). $
  Every other arc is admitted only if its head time is at or before the current $e$, so no route can run past its deadline. All horizon-end states $(p, t_w^"hor", dot)$ collapse to a single sink. Rounding $e$ down to whole days ($"floor"_"day"$) is what keeps the state space finite. Crews whose deadlines fall on the same day then share one $e$ rather than each carrying a minute-resolution clock, and because rounding down only ever tightens a deadline, never loosens it, no illegal route slips through.

+ *Clock-group aggregation.* Crew with an identical expanded graph form a group $g in cal(G)_w$, $K_g = |g|$. The model carries one flow $x_(g,a)$ for all $g in cal(G)_w, thick a in cal(A)_g$.

=== Structural Constraints
The model carries only flow-balance and coverage rows. Every other rule is baked into $cal(N)_w$ and $cal(A)_w$, so any flow that conserves is already legal.

#deftable((
  defrow($Delta_(t a)$, [Turnaround — flight arcs land on the snapped node $"arr"_f + Delta_(t a)$]),
  defrow($Delta_(d u t y)$, [Duty cap — flight arcs whose cumulative duty exceeds $60 Delta_(d u t y)$ are dropped]),
  defrow($Delta_(r e s t)$, [Rest — a wait arc of $>= 60 Delta_(r e s t)$ zeroes the duty clock]),
  defrow($d_(a w a y), Delta_(h b)$, [Away cap and home break — carried by each state node's expiry $e$, reset only by a $>= Delta_(h b)$ home stay]),
))

The LP is thus small and integral — conservation plus unit-coefficient coverage, with all legality in the arcs. Conservation also hands each crew a single trajectory. It leaves every state on exactly one arc and time strictly advances, so no crew is booked on two legs — or a leg and a wait — at once. One thing at a time is structural, not a row.

$d_(w o r k) = 3$ is not enforced as a rule. The reachability sweep drops runs of more than three consecutive duty days purely to shrink the graph. Once the overnight rests between duty days are counted, such a run almost always already breaches the $d_(a w a y)$ away cap, so cutting it early discards essentially no legal route. The only place it actually binds is the greedy senior-substitution gate, which re-checks it explicitly.

== Objective

The model minimises routing cost plus a penalty for unmet demand. Writing the two
pieces separately,

$ f^"route" = sum_(g in cal(G)_w) sum_(a in cal(A)_g) c_a thin x_(g,a) $

$ f^"unc" = c_(u n c) sum_(f in F_w) u_f + c_(u n c') sum_(f in F_(w')) u_f $

the combined objective is

$ min thick (f^"route" + f^"unc") $

with the per-arc cost, for a group based at $b$,

$ c_a = cases(
  c_(f l,ell) thin delta_f & "if " a in cal(A)^"fl" "of leg " f,
  c_(d h,f) (1 - rho bb(1)["dest"_f = b]) quad quad & "if " a in cal(A)^"dh" "of leg " f,
  c_(w t,ell) thin Delta t_a + c_(o v) bb(1)[Delta t_a >= 60 Delta_(o v)] & "if " a in cal(A)^"wt" "away from base",
  0 & "if " a "a home-wait or break-reset arc."
) $

== Constraints

#[
#set math.equation(numbering: "(1)")

$ sum_(a in delta^+(n)) x_(g,a) - sum_(a in delta^-(n)) x_(g,a)
  = cases(K_g & "if " n = "src"_g, -K_g & "if " n = "snk"_g, 0 & "otherwise")
  #h(1fr) quad forall g in cal(G)_w, thin n in cal(N)_g $

$ sum_(g in cal(G)_w) sum_(a in cal(A)_(g,f)^"fl") x_(g,a) + u_f >= m_f
  #h(1fr) quad forall f in F_w union F_(w') $

$ 0 <= x_(g,a) <= K_g
  #h(1fr) quad forall g in cal(G)_w, thin a in cal(A)_g $

$ 0 <= u_f <= m_f
  #h(1fr) quad forall f in F_w union F_(w') $
]

- (1) Conserves crew flow — $K_g$ crew leave each group's depot state, $K_g$ are absorbed at its horizon sink, and in- and out-flow balance at every other state.
- (2) Covers each leg — crew flow on its flight arcs plus shortfall slack meets the layer demand $m_f$.
- (3) Bounds a group's flow on any arc by the group size $K_g$.
- (4) Bounds a leg's shortfall by its demand $m_f$.

== Solve Method
=== Recovering Schedules by Flow Decomposition
// A clock-group collects crew that are not merely at the same place but in the same legal situation, so that any continuation feasible for one is feasible for all. At the start of window $w$ each crew $c$ is keyed by the tuple

// $ (b, thick o_c, thick d^0_c, thick alpha_c, thick e^0_c, thick h_c), $

// its home base $b$, its start airport $o_c$ (where carry-over left it), the consecutive duty days $d^0_c$ carried in, the away anchor $alpha_c$ — the minute $c$ last left home after a completed $48$-h break, the point its $d_(a w a y)$ budget counts from — the break deadline $e^0_c$ (the initial value of the expiry coordinate $e$ at the depot state, the latest minute its next $>= Delta_(h b)$ break may finish), and a partial-break credit $h_c$ recording the start of an in-progress home stay. Crew sharing this tuple expand to a byte-identical state graph — same depot, same sink, same admissible arcs, same reset points — so they are interchangeable, and the model carries one integer flow $x_(g,a)$ with $K_g = |g|$ instead of a variable per crew.

// The clock fields are what make the pooling exact rather than approximate. Two crew at the same airport are not in the same group if their away clocks differ: a crew three days into its away spell and one a single day out have different remaining budgets and different break deadlines, so a leg legal for the second could strand the first past $d_(a w a y)$ — they fall in separate groups. The partial-break credit $h_c$ likewise keeps a crew that is mid-break at the seam distinct, so its group finishes the remaining hours rather than restarting a fresh $48$.

// In the first window every crew sits at home on a fresh clock, so each base contributes a single group; groups multiply only as later windows scatter crew across positions and clock states. Reachability is cached one level coarser — on $(b, o_c, d^0_c, alpha_c)$ but not the break deadline — because the forward/backward Dijkstra does not depend on the deadline, only the break-clock expansion does.

The solver returns an integer flow $x_(g,a)$ per group and arc, not crew routes — it is the aggregation of @fig-groups run in reverse. Each group's expanded graph is acyclic, so its $K_g$ units of flow carry no cycles and split into $K_g$ simple paths, one per crew. Each path runs from the depot state $"src"_g$ to the home-base sink $"snk"_g$, conserving at every state in between, and node time-ordering makes it a valid chronological route — the endpoints are pinned to a base, the middle is free state-to-state travel.

#figure(
  image("img/clock_group_aggregation.svg", width:75%),
  caption: [Crew with an identical carry-state signature collapse into one clock-group, decomposition reverses the arrows.],
) <fig-groups>

The split preserves everything the model enforced. Coverage is a constraint on the per-arc flow ($sum$ flight-arc flow $+ thin u_f >= m_f$), and decomposition keeps each arc's count exact, so every leg keeps its crew. Each path lies inside the expanded graph, so turnaround, duty, rest, the away cap, and the home break already hold — nothing is re-checked. Because a group's crew are interchangeable, the paths attach to crew-ids in any order (an optional tie-break balances duty hours or holds ids steady across windows). Seam continuity comes from carry-over.

=== Senior Substitution in Idle Gaps

// After both passes, some surviving flights are still short a normal. A senior can fill a normal seat, but only opportunistically: inside an idle gap of its own layer-1 route, never displacing a senior duty. This is a greedy post-processing pass, not part of either MIP. It fills one seat per gap and writes each accepted fill back as a real flight leg on the senior's route, so the schedule, visualiser and validator all reflect it.

// An idle gap runs from a senior's arrival at an airport up to its next senior departure, or to the end of its route. A candidate fill flight $f$ must clear two gates.

// ==== Geometric Feasibility
// The senior must be parked at $f$'s origin $"orig"_f$, rested across the gap (the 8-hour minimum, so $"dep"_f - "arr"_"prev" >= 480$ minutes), and, when a senior duty follows the gap, able to connect to it, with $f$ landing where that duty departs ($"dest"_f = "orig"_"next"$), leaving the 45-minute turnaround, $"arr"_f + 45 <= "dep"_"next"$. A gap with no following duty clears this trivially.

// ==== Route Legality
// This is what the gate geometry alone misses: a senior parked far from base could satisfy the geometry yet break the away cap by flying a fill hours or days later. So we insert $f$ into the senior's route, re-sort by departure, and re-check the whole route against the same rules the model enforces. No leg may depart more than 4 days after the senior last left home with no 48-hour break in between, and no run of consecutive duty days may exceed 3 days. The check is cumulative, so several fills on one senior stay jointly legal. A gap failing either gate yields nothing, which makes the pass best-effort: it recovers a handful of understaffed flights and leaves the rest short.

After both layers solve and decompose, some surviving flights are still short a normal. A senior can fill a normal seat, but only opportunistically, inside an idle gap of its own senior route ($ell = 1$), never displacing a senior duty. This is a greedy post-processing pass, not part of either MIP — it fills one seat per gap and writes each accepted fill back as a real leg on the senior's route, so the schedule, visualiser (@fig-viz), and validator all reflect it.

An idle gap runs from a senior's arrival at an airport up to its next senior departure, or to the end of its route (@fig-sub). A candidate fill flight $f$ must clear two gates.

#figure(
  image("img/senior_substitution_idle_gap.svg"),
  caption: [A fill flight slotted into an idle gap],
) <fig-sub>

==== Geometric Feasibility
The senior must be parked at $f$'s origin $"orig"_f$, rested across the gap ($"dep"_f - "arr"_"prev" >= 60 Delta_(r e s t)$), and, when a senior duty follows the gap, able to connect to it, with $f$ landing where that duty departs ($"dest"_f = "orig"_"next"$), leaving the turnaround $"arr"_f + Delta_(t a) <= "dep"_"next"$. A gap with no following duty clears this trivially.

==== Route Legality
Geometry alone misses a slower failure. A senior parked far from base could satisfy it yet break the away cap by flying a fill hours or days later. So we insert $f$ into the senior's route, re-sort by departure, and re-check the whole route against the rules the validator applies — no leg departing more than $d_(a w a y)$ days after the senior last left home without a $Delta_(h b)$ break, and no run of consecutive duty days past $d_(w o r k)$. The check is cumulative, so several fills on one senior stay jointly legal. A gap failing either gate yields nothing, which makes the pass best-effort, recovering a handful of understaffed flights and leaving the rest short.

=== Barrier vs Simplex
The per-window relaxation is large and sparse, so it is solved by barrier (interior-point) rather than simplex, which burned the whole limit pivoting at the root. For unit-demand models — the senior layer, or any single-crew airline — the group-flow relaxation is integral, so barrier lands straight on an integer optimum with no branching, and crossover to a simplex basis is pure overhead, so it is switched off. These windows solve in well under a minute.

Once a flight needs more than one crew, coverage couples flow across groups and breaks that integrality, so the relaxation is fractional and the model must branch (the consequences are in the results). Each solve stops at a 1% gap or 30 minutes. Unit-demand windows close at a zero gap, so the limit binds only on these branching windows. For them an optional probe — off by default — runs crossover-free for a few seconds and, if still far from optimal, restarts with crossover on and the solver set to favour integer-feasible solutions, giving branch-and-bound a basis it can warm-start from.

=== Deterministic Model Construction

The expanded graph is built by exploring states held in hash sets, whose iteration order depends on the process hash seed. Identical inputs therefore produced models with their variables in different column orders from one run to the next, and because the crew-flow model is highly degenerate — many interchangeable crew and equivalent paths — the solver's anti-degeneracy effort swung sharply with that order, the same window taking anywhere from thirty seconds to over a hundred. Sorting arcs and nodes by value before they are handed to the solver makes the constructed model byte-identical across runs — same fingerprint, same result, same time — which removes the variance, makes timings comparable, and as a side effect presolves slightly smaller, since the regular ordering is easier to reduce.

Reproducibility rests on one more fixed point. The synthetic crew pool — its per-base counts and the 10% jitter — and the random load factors are all drawn from a single fixed seed, so a given airline and horizon regenerate the identical instance every run.

= Results

== G7

G7 is a small regional (@instance-table): $5141$ flights over the 30-day horizon across $51$ airports and $143$ routes, requirement two — one senior, one normal. It is chosen not for size but because it solves to optimality in every window while still exercising the full two-layer machinery.

=== Coverage Breakdown
Of the $5141$ flights, $5069$ ($98.6%$) are fully crewed, $10$ fly one normal short, and $62$ ($1.2%$) are cancelled for want of a senior. The senior layer covered $5079$ of $5141$ ($98.8%$). The normal fill left $19$ seats short, of which substitution recovered $9$. The schedule is flat and saturated — about $171$ flights a day, fully crewed on almost every bar (@fig-schedule-G7). The residual is concentrated. Cancellations show only at the day-$30$ tail ($16$), with spoke days $11$ and $18$ ($6$ each) the only earlier blips. The short bars on days $6$ and $19$ are light-traffic days, not failures.

=== Cancelled Flights
All $62$ cancellations come from layer 1 (no senior). They fall into three bands (@uncovered-table-G7).

None of this is an optimality-gap artifact, and the reason also fixes what _recoverable_ means. Every window solves to optimality over a totally-unimodular network (integral LP, no gap), and each cancellation costs $c_("unc") = 10^8$ — $62 times 10^8 = 6.20 times 10^9$, or $96%$ of G7's $6.48 times 10^9$ objective. At that price the solver positions crew wherever it legally can, positioning being a deadhead arc chosen inside the MIP and far cheaper than $10^8$, so an uncovered flight is never a missed deadhead but one the constraints forbid. The directional band is blocked by the away cap $Delta_("away") = 4$ days — the round-trip to a one-way spoke will not fit it — and the tail runs past the data. Recovering either is a macro change, not better routing — that is all _recoverable_ means here.

The directional band also clusters at window starts (@onedir-window-G7), which looks like a seam effect — but a longer seam barely touches it. Most of these flights already sit inside the current half-day look-ahead, so the previous window saw them and still found no return crew. More reach adds no crew when the bind is the away cap.

=== Understaffing and Substitution
A flight that keeps its senior but loses its normal can be filled post-solve by an idle senior already at the origin — something the normal layer cannot do, as seniors are not in its pool (@sub-table-G7). Each fill is co-located, so no deadhead is needed, and they cluster early when the pool is freshest. The $10$ that remain had no co-located senior and no normal the solve could reach.

The $10$ still short all operate, one seat under (@understaffed-window-G7). The fixes that don't work are the informative part. The seat costs the same $10^8$ as a cancellation (layer 2's $19$ short seats are $1.9 times 10^9$, nearly its whole objective), so the solver would fill any reachable one — it is not under-priced. Nor is it scarcity, with $120$–$190$ normals idle system-wide each time, nor seam reach, since most already sit inside the look-ahead. The rested normals that show at the origin are a merged-timeline illusion. At $10^8$ a seat a truly free crew would have been used, so these are committed in an adjacent window or held by a home-break or away-cap rule — visible globally, absent from the per-window feasible set.

So the bind is the decomposition itself. Like the cancellations, understaffing is a $10^8$ penalty paid because the sub-problem has no assignable crew — but here the crew exists, in a neighbouring window. Recovering it needs a less decomposed solve, wider window overlap or a joint re-solve, plus the horizon extension for the day-$30$ flight — $10$ seats, $0.2%$, short because the crew that could fill them sit in another sub-problem.

From the crew side (@fig-crew-G7), both pools look much the same — only a thin slice is flying at any moment (blue), most crew are resting or on break, and the dominant grey band is crew simply available. Even at the daily peaks roughly half the pool is idle, and usually more. That deep reserve is what makes senior substitution cheap — an idle senior is often already where a normal is missing — and it is the same idle crew the decomposition cannot reach across window boundaries.

== ZW with Random $r_f$

ZW is a random-demand instance, with each flight's requirement $r_f$ drawn across $1$ to $8$ rather than fixed. It has $3594$ flights over $30$ days, $182$ seniors and $511$ normals, and solves to optimality in every window. The heavy, variable demand makes understaffing, not cancellation, the dominant residual.

=== Coverage Breakdown
Seat-demand totals 16,307 across the $3594$ flights, one senior seat each and 12,713 normal seats, a mean of $4.5$ crew per flight. The senior layer cancelled $24$ flights ($0.7%$). The normal layer left $510$ of the 12,713 seats short ($96.0%$ filled), of which substitution recovered $33$. By flight, $3400$ ($94.6%$) are fully crewed, $170$ fly under $r_f$, and $24$ are cancelled. The shortfall is not spread evenly. It is negligible through the first half and climbs across the back half to a peak near day $26$ — a gradual slope, unlike G7's sharp day-$30$ cliff (@fig-schedule-ZW).

=== Cancelled Flights
All $24$ cancellations are layer-1 failures (no senior), and every one is an ORD–spoke connection where the senior pool runs too thin. Fifteen are inbound spoke→ORD legs — LIT recurs three times, each needing $8$, with MHK and MKE twice each — four form two complete day-$2$ round trips (ORD↔IND, ORD↔COU) whose routes never get a senior, and five are a day-$30$ tail of ORD→spoke departures whose return runs past the horizon. This is similar pattern to that of G7.

=== Understaffing and Substitution
Understaffing is ZW's real residual, and its cause is not the one the build-up suggests. The empty seat is priced at $10^8$, the same as a cancellation — layer 2's $510$ short seats are $5.1 times 10^10$, its entire objective — so the solver fills any seat it can reach. And the pool is far from spent, with $350$–$400$ of the $511$ normals idle at midday on every day of the month, the worst understaffing days included. So it is neither under-pricing nor a headcount shortage. More crew would sit just as idle.

What concentrates the shortfall is random demand meeting thin geography. The worst-hit flights are high-$r_f$ departures into spokes that cannot cycle the crew back (@deadend-ZW). ORD→OMA needs seven, but the single daily OMA→ORD leg seats one, so six crew flown out would strand and the seats stay empty though dozens of normals idle at ORD. MCI, SPI and LIT repeat the pattern, their return capacity or its timing short of the outbound demand. The idle ORD reserve is real but unusable, since positioning crew out to a one-way spoke breaks the away cap on the way home. Because $r_f$ can reach $8$, an unreachable spoke shows up not as one cancelled flight but as a six- or seven-seat shortfall on a flight that still operates.

The back-half build-up is not depletion. Demand is nearly flat — mean normal seat-demand is $413$ a day early and $433$ late — and the pool stays idle throughout, so the rise is the rolling horizon, not exhaustion. As windows hand off, the crew distribution drifts hub-ward, leaving fewer normals pre-positioned at the spokes where late high-demand legs need them, and the per-window partition then widens those spoke shortfalls. The levers follow from the cause — a relaxed away cap or a permitted deadhead-home to let crew reach one-way spokes, wider window overlap to share the idle reserve across boundaries, a longer horizon for the day-$30$ tail — and not more staff, whose only effect would be a larger idle reserve.

The reserve is plain on the crew side (@fig-crew-ZW): the normal pool spends most of the month available, a deep idle band the schedule never draws down — because the seats it cannot fill are unreachable, not unstaffed.

Substitution works as it does in G7, with the timing reversed. The mechanism is identical — a co-located idle senior takes an empty normal seat post-solve, no deadhead — but where G7's fills land early and run dry as the pool thins, ZW's land late, 24 of the 33 falling in days 22–30 alongside the understaffing peak. The senior pool is what allows it. Only seven to ten of the 164 seniors fly at any moment, so a deep idle reserve sits available all month, exactly when and where the back-half shortfalls appear. Even so it only dents the residual, 33 seats of 510, because each gap takes one seat and the same away cap that strands a normal at a one-way spoke strands a senior just as surely — it patches the cheap co-located seats and leaves the dead-end multi-seat shortfalls untouched.

== Two-Layer Tractability
Solving the multi-crew requirement directly — one model with $r_f > 1$ — shows why the two-layer split matters. With every G7 flight needing two crew, the coverage constraints make the root relaxation fractional, so the barrier point is no longer integer and the model must branch. Most windows still resolve at the root, but the windows late in the horizon, where the return tail runs past the end of the flight data and connectivity is sparse, stall. The relaxation bound is loose — the LP "covers" flights with fractional flow that no whole crew can realise — and, with crossover off, each branch-and-bound node re-solves its LP without a warm-start basis, so node throughput collapses. Three of the ten windows hit the thirty-minute limit at 24 to 83 per cent gap with badly degraded coverage. The two-layer decomposition avoids this entirely. Each layer is unit-demand and therefore root-integral, so every window solves quickly — the same airline that stalls as one model runs cleanly as two layers.

== Schedule Validation
An earlier carry-over read each crew's break deadline straight off the last committed arc of the expanded graph. Because the cost-minimising solution within a window is free to park the mandatory 48-hour break in the uncommitted tail, that arc reported a deadline as if the break had already been served, so the anchor advanced every window even though no break was ever committed. The away clock therefore reset at each seam and drifted forward, letting crew accumulate five to seven days away and double-digit consecutive duty days across the merged schedule — on the G7 instance the independent validator flagged 352 away-cap and 341 consecutive-duty violations, none of which were visible window-by-window. Tracking the anchor on the committed legs only, exactly as the validator reconstructs it, and crediting a partly-served break across the seam, removes the drift, and the same instance then validates with zero violations of any rule while senior coverage is essentially unchanged at 5079 of 5141 flights, confirming the violations were a boundary-accounting artefact rather than illegal routes the solver actually wanted.

== Costs
The cost ended up mostly on heavily penalised uncovered flights (see @cost-G7 and @cost-ZW). As our dollar figures are relative, not calibrated to real operations — the per-minute crew rates sit far above actual cabin-crew pay, so every cost here, the $c_(u n c) = 10^8$ uncovered penalty included, is orders of magnitude above its real-world counterpart. A real regional-jet cancellation costs only about \$1,050 to \$2,750 @finlay2023canceled, below even a single crew leg in our cost structure, so the penalty is a big-M device that forces coverage on a committed schedule rather than an estimate of what cancelling actually costs.

The committed-schedule point is what makes the big-M the right choice rather than the real figure. Were $c_(u n c)$ set to the true cancellation cost, the cost-minimising schedule would cancel flights instead of crewing them, since covering a leg costs more than the penalty for leaving it uncovered, and the model would staff almost nothing.

= Things Attempted but Left Out

== Banning Overcovering
Model size is identical either way, about 2M variables and 1.2M constraints per window, so the slowdown is not from a larger model. Equality runs about 2.6× slower on G7 and 1.9× on ZW (@overstaffing-bench), and it pushes windows past the 1,800 s limit, six on G7 and one on ZW, where the inequality solves every window to optimality. The ZW window that times out covers only 170 of 339 flights.

== Per-crew Binary

The most direct formulation gives each crew member their own binary variables, one $x_(c,a)$ per crew $c$ and arc $a$, with a separate flow-balance constraint per crew member. It mirrors the slide formulation exactly and tracks every crew member individually rather than as an interchangeable pool. On ZW it solves cleanly, all ten windows optimal within the 1% tolerance, 3,465 of 3,485 flights covered with 20 slots left uncovered, in about 43 minutes across the horizon (@percrew-zw).

It works only because ZW is the smallest carrier in the enriched dataset, 3,790 flights against G7's 5,575. Giving each crew member distinct binaries makes interchangeable crew at a base distinct in the model, which floods branch-and-bound with symmetric solutions and leaves a relaxation far weaker than the aggregated model, so every window has to branch rather than settle at the root. At ZW's size that branching is affordable, 60 to 514 seconds per window, but it does not survive a step up to G7 or anything larger. This is what pushed the final solver to aggregate interchangeable crew into clock-groups carrying integer flow, which removes the symmetry and solves at the root.

== Looser Time Limit

Raising the per-window limit does not rescue the multi-crew windows that stall. The ones that stall are stuck in root relaxation processing rather than branching, so extra wall-clock buys more slow root work, not a closed gap. That is a model-size problem, which is why the limit stays at 1,800 s and the effort went into shrinking the model instead.

== Mainline-Scale Airlines

Running the solver on a mainline carrier directly is intractable. Their networks are far denser than the regionals, AA alone spans 119 airports and 836 routes against G7's 51 and 143, so a single rolling window blows past the roughly 2M variables a regional window already carries and never finishes within the limit. We kept to regional instances and recovered mainline-like varied demand by randomising $r_f$ on the small ZW network instead, leaving how to actually reach mainline scale to the proposals.

== DDD Dead Loop

The model was first built on Dynamic Discretisation Discovery, which starts from a coarse time grid, refines it wherever the relaxation violates a timing rule, re-solves, and repeats until the grid is fine enough. Here that loop was dead weight. The network is already event-exact, with a node at every flight departure and arrival plus the turnaround-snap node, so there is no coarse grid to refine, and the loop converged at iteration 0 in every window without ever inserting a node. We removed the whole DDD layer, the refinement loop, its LP-relaxation pre-solve, and the violation machinery, and hand the integer model straight to Gurobi, which processes its own root relaxation anyway.



// === Direct Multi-Crew (min_crew > 1) Solve

// === Barrier with Crossover Disabled

// Enabling crossover globally, to give branch-and-bound a warm-start basis on the fractional multi-crew windows, was a net loss. The easy windows that solve at the root and never branch paid a large crossover cost for nothing — forcing a basis onto a million-variable degenerate model pushes hundreds of thousands of variables to a vertex, with a restart, and one window grew from about seven to twenty-two minutes for no benefit. We kept a targeted variant as an opt-in instead, a probe with crossover off that switches crossover on and re-solves with the full budget only when a window times out while still branching far from optimal. This rescues the mid-size stalled windows without taxing the majority, though it does not help the largest window, which is stuck in root processing before branching even starts, a model-size problem rather than a missing-basis one.


== Separate Working-Day Counter per Away Window

We tried a separate counter tracking working days inside each time-away window. It duplicated most of what the away-time limit already enforces while enlarging the model substantially, so we folded it into a mandatory 48-hour home break every four days instead.

== Longer Away Window (7 Days to 4 Days)

The return tail $T_"tail"$ equals the away cap $D_"away"$, so a 7-day away cap forces a 7-day tail and a 14-day window horizon, against 11 days at a 4-day cap, a much larger graph and model per window. We dropped the cap to 4 days with a mandatory 48-hour home break, which shrinks each window and lets the separate consecutive-duty-days counter above fold in.

== Cutting Planes

Adding cutting planes for the duty rules was very slow and ultimately uninformative. The validator reported zero violations on every rule, but only because the away-day cut already holds every crew under four days, so the 48-hour-break-after-a-4-day-rotation rule never has a chance to fire, crew resting at home in shorter and more frequent stretches instead. The clean report reflects that side effect rather than the cutting planes doing useful work.

== Warm Starts

Warm-starting each window from the previous window's routes never paid off. The original code set `Var.Start` on the matched flight arcs only and left the wait arcs undefined, so Gurobi received a near-empty partial solution, five fixed variables out of 773,446, and ran a completion sub-MIP that consumed the whole budget with its first incumbent at 247 s. Rewriting the seed to reconstruct each crew's full in-window flow along the wait-arc spine matched perfectly, every leg carried across, but only the 11 to 14 legs that cross each window boundary could be seeded, around 0.1 to 0.4% of the variables. The cause is structural rather than a bug, each window flies only its committed three-day region and leaves the four-day overlap idle because tail flights carry no coverage reward, so there is almost nothing to transfer. Even that sparse start still triggered the completion sub-MIP and ran net-negative, 129.6 s against 106.1 s without it. Switching from `Var.Start` to `VarHintVal` removed the overhead, since hints guide Gurobi's heuristics without invoking the completion sub-MIP, though with so little to seed the warm start buys no real speed-up.



= Future Work / Proposals

== Two-Phase (Lexicographic) Objective

The big-M penalty $c_(u n c) = 10^8$ is roughly 98% of the objective in G7 and over 99% in ZW, swamping labour cost and weakening the LP relaxation. Minimising uncovered slots first and labour cost second drops the giant coefficient, keeps coverage strictly prioritised, and should help the windows that currently stall at a large gap.

== Joint Senior + Normal Two-Commodity Flow

Solving the senior layer, then the normal layer, then substituting is what strands the ten G7 flights left understaffed beside idle but partition-locked normals. A single two-commodity flow with substitution as a senior taking a normal arc would resolve this exactly, at the cost of a much larger MIP.

== Flow-Based Senior Substitution

Greedy substitution only fills a seat when an idle senior already lines up with it one-way, recovering just 9 G7 shortfalls and 33 of 510 ZW seats. Letting the senior deadhead back afterwards would free it to cover thin spokes without being stranded, exposing every feasible fill rather than only the aligned ones.

== Coordinated Two-Layer Positioning

The layers deadhead independently, so normals cannot reach the thin spokes the senior layer already moves crew toward. Seeding the normal layer with senior positions, or sharing deadhead arcs, would attack the dead-end geometry behind most ZW understaffing directly.

== Scaling to Mainline-Size Airlines

The model handles regional schedules, 5,141 flights for G7 and 3,594 for ZW, but mainline carriers run an order of magnitude more and the time-expanded flow grows with both flight count and time resolution. Three reductions trade accuracy or seams for size.

- *Shorter window horizon* — smaller per-window MIPs but more seams, exactly where the day-30 cliff and window-start cancellations already sit.
- *Coarser time bucketing* — wider buckets collapse nodes and arcs but blur the turnaround and rest thresholds.
- *Column generation* — price crew pairings on demand instead of building the full network up front, the standard mainline approach.

== Deterministic Tie-Break for Trajectory Stability

Equal-cost assignments let the solver return different routes each run, so crew-state trajectories jump between equivalent optima. A lexicographic tie-break on crew id or a small deadhead cost pins one canonical solution without changing labour cost.

#pagebreak()
= Appendix

== Instance Data

#figure(
  table(
    columns: (auto, auto, auto, auto, auto, auto),
    align: (left, right, right, right, right, left),
    table.header([*Carrier*], [*Flights*], [*Airports*], [*Routes*], [*Flights / airport*], [*Min. crew $r_f$*]),
    [ZW], [3,790],   [44],  [93],    [86],   [2 (uniform)],
    [G7], [5,575],   [51],  [143],   [109],  [2 (uniform)],
    [C5], [6,612],   [57],  [116],   [116],  [2 (uniform)],
    [YV], [6,628],   [69],  [178],   [96],   [2 (uniform)],
    [HA], [6,690],   [22],  [80],    [304],  [3--6],
    [QX], [7,754],   [53],  [201],   [146],  [2 (uniform)],
    [G4], [9,345],   [119], [789],   [79],   [3--4],
    [PT], [10,621],  [69],  [175],   [154],  [2 (uniform)],
    [F9], [15,526],  [80],  [624],   [194],  [3--4],
    [NK], [17,544],  [60],  [433],   [292],  [3--4],
    [B6], [17,918],  [57],  [281],   [314],  [2--4],
    [AS], [18,163],  [85],  [357],   [214],  [3--4],
    [9E], [18,279],  [107], [380],   [171],  [2 (uniform)],
    [OH], [21,092],  [94],  [375],   [224],  [2 (uniform)],
    [MQ], [21,888],  [145], [526],   [151],  [2 (uniform)],
    [YX], [27,854],  [83],  [600],   [336],  [2 (uniform)],
    [UA], [62,007],  [119], [811],   [521],  [3--8],
    [OO], [65,026],  [239], [1,231], [272],  [2 (uniform)],
    [AA], [75,088],  [119], [836],   [631],  [3--8],
    [DL], [76,306],  [140], [879],   [545],  [2--7],
    [WN], [105,307], [104], [1,606], [1013], [3--8],
  ),
  caption: [Network size, density (flights per airport) and per-flight crew requirement for every operating carrier in the January 2025 BTS data. _Routes_ counts distinct directed origin--destination pairs.],
) <instance-table>

== Solve Methods
#figure(
  image("img/Vizz.png", width: 100%),
  caption: [The interactive schedule visualiser, showing each crew's routed legs, deadheads, rest, and home breaks across the planning horizon. Source and a live demo at #link("https://github.com/Ei3-kw/flight-solver-visualizer")[flight solver visualizer].],
) <fig-viz>
\ \ \
== G7 Results

#figure(
  image("img/whole_schedule_coverage_by_day_G7.svg", width: 100%),
  caption: [Flights per day, stacked by crewing outcome.],
) <fig-schedule-G7>
#v(2pt)
\ \ \
#figure(
  table(
    columns: (2.3fr, auto, 4.3fr, 2.5fr),
    align: (left, center, left, left),
    inset: 6pt,
    table.header([*Cause*], [*Count*], [*Mechanism*], [*Verdict*]),

    [Structural spoke, sub-45 turnaround (CLT, RDU, GRR, MCI)],
    [18],
    [In-and-out connection sits below $Delta_(t a) = 45$, so no crew can self-chain the round-trip, and the airport is too thin to position a second crew. RDU is covered at a 56-min turnaround and uncovered at 41.],
    [Genuine geometry limit],

    [One-directional connectivity (AVL, SCE, ABE, MHT, …)],
    [18],
    [Reachable inbound but no rested crew for the return. EWR→AVL is reachable by 151 crew, AVL→EWR by 4. The away cap strands any crew flown in, and 10 have the reverse leg covered.],
    [Needs a relaxed away cap or a deadhead-home leg],

    [Rolling-horizon tail (hub origins, day $>= 28$)],
    [26],
    [Sit in the last window's tail, the return tail $T_(t a i l)$ runs past the flight data and no later window re-solves them. 16 depart on day 30.],
    [Needs a longer data horizon],
  ),
  caption: [The 62 uncovered G7 flights by cause.],
) <uncovered-table-G7>
#v(2pt)
\ \ \

#figure(
  table(
    columns: (auto, auto, auto, auto, auto, auto, auto, auto),
    align: (left, left, center, center, center, center, center, center),
    inset: 5pt,
    table.header([*Flight*], [*Route*], [*Day*], [*Time*], [*Win*], [*Frac*], [*Position*], [*In seam?*]),
    [4493], [RIC→EWR], [1], [09:21], [0], [0.13], [start], [yes],
    [4453], [DAY→ORD], [7], [10:20], [2], [0.14], [start], [yes],
    [4522], [SCE→ORD], [10], [06:48], [3], [0.09], [start], [yes],
    [4614], [PHL→IAD], [13], [06:15], [4], [0.09], [start], [yes],
    [4166], [ABE→ORD], [16], [06:30], [5], [0.09], [start], [yes],
    [4446], [EWR→AVL], [18], [10:17], [5], [0.81], [end], [—],
    [4444], [AVL→EWR], [18], [13:15], [5], [0.85], [end], [—],
    [4395], [ORD→DAY], [19], [14:40], [6], [0.20], [middle], [no],
    [4400], [DAY→ORD], [19], [17:40], [6], [0.25], [middle], [no],
    [4555], [SCE→ORD], [19], [17:45], [6], [0.25], [middle], [no],
    [4436], [MHT→EWR], [20], [18:09], [6], [0.59], [middle], [no],
    [4420], [MHT→EWR], [22], [06:00], [7], [0.08], [start], [yes],
    [4439], [PVD→EWR], [22], [13:01], [7], [0.18], [start], [just past],
    [4528], [LIT→ORD], [25], [07:30], [8], [0.10], [start], [yes],
    [4531], [LNK→ORD], [25], [07:45], [8], [0.11], [start], [yes],
    [4506], [STL→IAD], [25], [08:15], [8], [0.11], [start], [yes],
    [4521], [ROC→ORD], [25], [13:40], [8], [0.19], [start], [just past],
    [4436], [MHT→EWR], [25], [17:51], [8], [0.25], [middle], [no],
  ),
  caption: [The 18 one-directional flights by window position. _Frac_ is the fraction through the 3-day window. _In seam?_ marks those inside the half-day look-ahead.],
) <onedir-window-G7>
#v(2pt)
\ \ \

#figure(
  table(
    columns: (auto, auto, auto, auto, auto, auto, auto),
    align: (left, left, center, center, center, center, left),
    inset: 5pt,
    table.header([*Flight*], [*Route*], [*Day*], [*Time*], [*Senior*], [*Base*], [*Idle at origin*]),
    [4426], [STL→ORD], [1], [10:35], [\#64], [EWR], [mid-trip],
    [4449], [BHM→ORD], [1], [07:30], [\#158], [ORD], [mid-trip],
    [4532], [MEM→ORD], [2], [11:26], [\#174], [ORD], [mid-trip],
    [4167], [DCA→EWR], [5], [08:00], [\#74], [EWR], [mid-trip],
    [4175], [ABE→ORD], [1], [17:30], [\#2], [ABE], [at base],
    [4430], [MLI→ORD], [7], [15:59], [\#144], [MLI], [at base],
    [4385], [EWR→ILM], [7], [10:50], [\#45], [DCA], [mid-trip],
    [4431], [ORD→ABE], [7], [18:15], [\#165], [ORD], [at base],
    [4457], [EWR→DCA], [4], [20:10], [\#59], [EWR], [at base],
  ),
  caption: [The 9 senior substitutions.],
) <sub-table-G7>
#v(2pt)
\ \ \

#figure(
  table(
    columns: (auto, auto, auto, auto, auto, auto, auto, auto),
    align: (left, left, center, center, center, center, center, center),
    inset: 5pt,
    table.header([*Flight*], [*Route*], [*Day*], [*Win*], [*Frac*], [*Position*], [*In seam?*], [*Idle @ orig*]),
    [4450], [ILM→EWR], [13], [4], [0.19], [start], [just past], [3],
    [4421], [ALB→EWR], [16], [5], [0.08], [start], [yes], [2],
    [4478], [CVG→EWR], [16], [5], [0.11], [start], [yes], [1],
    [4502], [LGA→IAD], [19], [6], [0.15], [start], [yes], [1],
    [4390], [ITH→EWR], [20], [6], [0.57], [middle], [no], [1],
    [4516], [GSO→EWR], [22], [7], [0.08], [start], [yes], [5],
    [4405], [GSO→ORD], [22], [7], [0.09], [start], [yes], [6],
    [4390], [ITH→EWR], [25], [8], [0.24], [middle], [~], [3],
    [4421], [ALB→EWR], [28], [9], [0.08], [start], [yes], [3],
    [4584], [IAD→PHL], [30], [9], [0.85], [end], [—], [2],
  ),
  caption: [The 10 flights still one normal short, by window position. _Idle @ orig_ counts rested normals at the origin in the merged timeline.],
) <understaffed-window-G7>
#v(2pt)
\ \ \

#figure(
  image("img/crew_states_two_layer_G7.svg", width: 100%),
  caption: [Crew states by layer],
) <fig-crew-G7>
#v(2pt)
\ \ \

#figure(
  table(
    columns: 4,
    align: (left, right, right, right),
    stroke: none,
    inset: (x: 9pt, y: 4pt),
    table.hline(),
    table.header([], [*Senior*], [*Normal*], [*Total*]),
    table.hline(stroke: 0.6pt),
    [Flying minutes],   [526,330],   [523,423],   [1,049,753],
    [Deadhead minutes], [55,388],    [35,637],    [91,025],
    [Waiting minutes],  [2,025,757], [1,910,574], [3,936,331],
    table.hline(stroke: 0.4pt),
    [Flying cost (\$)],   [221,058,600], [52,342,300], [273,400,900],
    [Deadhead cost (\$)], [23,262,960],  [3,563,700],  [26,826,660],
    [Waiting cost (\$)],  [2,025,757],   [955,287],    [2,981,044],
    table.hline(stroke: 0.4pt),
    [Crew labour cost (\$)], [246,347,317], [56,861,287], [303,208,604],
    table.hline(stroke: 0.4pt),
    [Uncovered penalty (\$)],
      table.cell(colspan: 2, align: right)[134 slots $times 10^8$],
      [13,400,000,000],
    [*Grand total* (\$)], [], [], [*13,703,208,604*],
    table.hline(),
  ),
  caption: [G7 two-layer cost breakdown by crew class (425 active routes, 238 senior and 226 normal crew).],
) <cost-G7>

== ZW Results

#figure(
  image("img/whole_schedule_coverage_by_day_ZW.svg", width: 100%),
  caption: [Flights per day, stacked by crewing outcome.],
) <fig-schedule-ZW>
#v(2pt)
\ \ \

#figure(
  table(
    columns: (auto, auto, auto, auto, auto, auto),
    align: (left, left, center, center, center, center),
    inset: 5pt,
    table.header([*Flight*], [*Route*], [*Day*], [*Need*], [*Short*], [*Idle @ ORD*]),
    [6034], [ORD→OMA], [4], [7], [6], [73],
    [6173], [ORD→MCI], [26], [8], [7], [55],
    [6045], [ORD→SPI], [26], [8], [7], [55],
    [6156], [ORD→LIT], [22], [8], [6], [45],
  ),
  caption: [Representative understaffed flights: high-demand departures into spokes, with rested normals idle at the origin.],
) <deadend-ZW>

#v(2pt)
\ \ \

// #figure(
//   table(
//     columns: (auto, auto, auto, auto, auto, auto),
//     align: (left, left, center, center, center, center),
//     inset: 4pt,
//     fill: (_, y) => if (5, 78, 121, 130).contains(y) { rgb("#FBE3A6") },
//     table.header([*Flight*], [*Route*], [*Day*], [*Need*], [*Short*], [*Idle @ ORD*]),
//     [6171], [ATW→ORD], [1], [4], [1], [0],
//     [6034], [ORD→OMA], [1], [7], [6], [92],
//     [6034], [ORD→OMA], [2], [7], [6], [72],
//     [6034], [ORD→OMA], [3], [7], [6], [74],
//     [6034], [ORD→OMA], [4], [7], [6], [73],
//     [6073], [LEX→ORD], [4], [3], [2], [8],
//     [6034], [ORD→OMA], [5], [7], [6], [49],
//     [6034], [ORD→OMA], [6], [7], [6], [43],
//     [6171], [ATW→ORD], [7], [4], [2], [2],
//     [6122], [CAE→ORD], [7], [5], [2], [14],
//     [6114], [FWA→ORD], [7], [7], [2], [1],
//     [6039], [DSM→ORD], [7], [6], [2], [14],
//     [6022], [HSV→ORD], [8], [2], [1], [0],
//     [6162], [PIA→ORD], [8], [2], [1], [0],
//     [6039], [DSM→ORD], [8], [6], [4], [15],
//     [6141], [MHK→ORD], [9], [5], [3], [1],
//     [6022], [HSV→ORD], [9], [2], [1], [0],
//     [6162], [PIA→ORD], [9], [2], [1], [0],
//     [6039], [DSM→ORD], [9], [6], [5], [15],
//     [6141], [MHK→ORD], [10], [5], [1], [1],
//     [6162], [PIA→ORD], [10], [2], [1], [0],
//     [6171], [ATW→ORD], [10], [4], [1], [3],
//     [6043], [CVG→ORD], [10], [5], [1], [6],
//     [6114], [FWA→ORD], [10], [7], [6], [4],
//     [6039], [DSM→ORD], [10], [6], [5], [15],
//     [6022], [HSV→ORD], [11], [2], [1], [0],
//     [6162], [PIA→ORD], [11], [2], [1], [0],
//     [6039], [DSM→ORD], [11], [6], [5], [15],
//     [6022], [HSV→ORD], [12], [2], [1], [0],
//     [6162], [PIA→ORD], [12], [2], [1], [0],
//     [6039], [DSM→ORD], [12], [6], [5], [15],
//     [6022], [HSV→ORD], [13], [2], [1], [0],
//     [6114], [FWA→ORD], [13], [7], [2], [3],
//     [6039], [DSM→ORD], [13], [6], [5], [15],
//     [6022], [HSV→ORD], [14], [2], [1], [0],
//     [6162], [PIA→ORD], [14], [2], [1], [0],
//     [6039], [DSM→ORD], [14], [6], [5], [15],
//     [6022], [HSV→ORD], [15], [2], [1], [0],
//     [6162], [PIA→ORD], [15], [2], [1], [0],
//     [6039], [DSM→ORD], [15], [6], [5], [15],
//     [6141], [MHK→ORD], [16], [5], [1], [1],
//     [6140], [RST→ORD], [16], [8], [2], [5],
//     [6153], [MLI→ORD], [16], [7], [1], [4],
//     [6041], [TVC→ORD], [16], [4], [2], [1],
//     [6022], [HSV→ORD], [16], [2], [1], [0],
//     [6162], [PIA→ORD], [16], [2], [1], [0],
//     [6171], [ATW→ORD], [16], [4], [2], [8],
//     [6043], [CVG→ORD], [16], [5], [1], [5],
//     [6038], [FSD→ORD], [16], [3], [1], [0],
//     [6114], [FWA→ORD], [16], [7], [5], [3],
//     [6039], [DSM→ORD], [16], [6], [5], [15],
//     [6022], [HSV→ORD], [17], [2], [1], [0],
//     [6162], [PIA→ORD], [17], [2], [1], [0],
//     [6039], [DSM→ORD], [17], [6], [5], [15],
//     [6022], [HSV→ORD], [18], [2], [1], [0],
//     [6162], [PIA→ORD], [18], [2], [1], [0],
//     [6107], [ATW→ORD], [19], [6], [3], [18],
//     [6086], [FNT→ORD], [19], [5], [1], [5],
//     [6022], [HSV→ORD], [19], [2], [1], [0],
//     [6156], [ORD→LIT], [19], [8], [5], [46],
//     [6038], [ORD→FSD], [19], [3], [2], [46],
//     [6044], [MCI→ORD], [19], [5], [1], [15],
//     [6063], [RST→ORD], [19], [6], [3], [6],
//     [6039], [DSM→ORD], [19], [6], [5], [15],
//     [6141], [MHK→ORD], [20], [5], [3], [3],
//     [6022], [HSV→ORD], [20], [2], [1], [0],
//     [6162], [PIA→ORD], [20], [2], [1], [0],
//     [6082], [MHK→ORD], [20], [2], [1], [3],
//     [6039], [DSM→ORD], [20], [6], [5], [15],
//     [6141], [MHK→ORD], [21], [5], [1], [3],
//     [6022], [HSV→ORD], [21], [2], [1], [0],
//     [6162], [PIA→ORD], [21], [2], [1], [0],
//     [6039], [DSM→ORD], [21], [6], [5], [15],
//     [6140], [RST→ORD], [22], [8], [4], [6],
//     [6041], [TVC→ORD], [22], [4], [1], [2],
//     [6022], [HSV→ORD], [22], [2], [1], [0],
//     [6162], [PIA→ORD], [22], [2], [1], [0],
//     [6156], [ORD→LIT], [22], [8], [6], [45],
//     [6038], [FSD→ORD], [22], [3], [1], [0],
//     [6039], [DSM→ORD], [22], [6], [5], [15],
//     [6183], [ORD→DAY], [22], [6], [2], [47],
//     [6053], [MSN→ORD], [22], [2], [1], [0],
//     [6181], [AZO→ORD], [22], [5], [3], [2],
//     [6141], [MHK→ORD], [23], [5], [4], [0],
//     [6022], [HSV→ORD], [23], [2], [1], [0],
//     [6162], [PIA→ORD], [23], [2], [1], [0],
//     [6173], [ORD→MCI], [23], [8], [7], [47],
//     [6109], [ORD→CMH], [23], [6], [2], [47],
//     [6128], [ORD→LEX], [23], [5], [3], [47],
//     [6082], [MHK→ORD], [23], [2], [1], [0],
//     [6029], [ORD→LAN], [23], [2], [1], [47],
//     [6062], [ORD→RST], [23], [8], [1], [47],
//     [6163], [ORD→ALO], [23], [6], [2], [47],
//     [6045], [ORD→SPI], [23], [8], [4], [47],
//     [6039], [DSM→ORD], [23], [6], [5], [15],
//     [6027], [SPI→ORD], [23], [7], [5], [3],
//     [6053], [MSN→ORD], [23], [2], [1], [1],
//     [6181], [AZO→ORD], [23], [5], [4], [6],
//     [6141], [MHK→ORD], [24], [5], [3], [0],
//     [6022], [HSV→ORD], [24], [2], [1], [0],
//     [6162], [PIA→ORD], [24], [2], [1], [0],
//     [6043], [CVG→ORD], [24], [5], [4], [4],
//     [6082], [MHK→ORD], [24], [2], [1], [0],
//     [6039], [DSM→ORD], [24], [6], [5], [15],
//     [6141], [MHK→ORD], [25], [5], [3], [3],
//     [6140], [RST→ORD], [25], [8], [1], [10],
//     [6031], [MKE→ORD], [25], [6], [2], [25],
//     [6086], [FNT→ORD], [25], [5], [1], [5],
//     [6022], [HSV→ORD], [25], [2], [1], [0],
//     [6162], [PIA→ORD], [25], [2], [1], [0],
//     [6171], [ATW→ORD], [25], [4], [2], [19],
//     [6044], [MCI→ORD], [25], [5], [1], [18],
//     [6082], [MHK→ORD], [25], [2], [1], [3],
//     [6114], [FWA→ORD], [25], [7], [4], [3],
//     [6053], [ORD→MSN], [25], [2], [1], [53],
//     [6039], [DSM→ORD], [25], [6], [5], [15],
//     [6110], [ORD→DAY], [25], [5], [1], [54],
//     [6141], [MHK→ORD], [26], [5], [3], [3],
//     [6022], [HSV→ORD], [26], [2], [1], [0],
//     [6162], [PIA→ORD], [26], [2], [1], [0],
//     [6173], [ORD→MCI], [26], [8], [7], [55],
//     [6109], [ORD→CMH], [26], [6], [2], [55],
//     [6038], [ORD→FSD], [26], [3], [2], [55],
//     [6082], [ORD→MHK], [26], [2], [1], [55],
//     [6128], [ORD→LEX], [26], [5], [3], [55],
//     [6054], [ORD→MCI], [26], [8], [1], [55],
//     [6029], [ORD→LAN], [26], [2], [1], [55],
//     [6163], [ORD→ALO], [26], [6], [1], [55],
//     [6178], [ORD→GRB], [26], [7], [2], [55],
//     [6045], [ORD→SPI], [26], [8], [7], [55],
//     [6039], [DSM→ORD], [26], [6], [5], [15],
//     [6027], [SPI→ORD], [26], [7], [5], [4],
//     [6053], [MSN→ORD], [26], [2], [1], [0],
//     [6181], [AZO→ORD], [26], [5], [4], [2],
//     [6131], [FWA→ORD], [26], [5], [1], [3],
//     [6141], [MHK→ORD], [27], [5], [2], [0],
//     [6022], [HSV→ORD], [27], [2], [1], [0],
//     [6162], [PIA→ORD], [27], [2], [1], [0],
//     [6067], [ORD→SDF], [27], [3], [1], [47],
//     [6038], [ORD→FSD], [27], [3], [1], [47],
//     [6044], [MCI→ORD], [27], [5], [1], [21],
//     [6043], [CVG→ORD], [27], [5], [4], [4],
//     [6038], [FSD→ORD], [27], [3], [2], [1],
//     [6082], [MHK→ORD], [27], [2], [1], [0],
//     [6114], [FWA→ORD], [27], [7], [4], [5],
//     [6039], [DSM→ORD], [27], [6], [4], [15],
//     [6181], [AZO→ORD], [27], [5], [2], [1],
//     [6141], [MHK→ORD], [28], [5], [3], [1],
//     [6162], [PIA→ORD], [28], [2], [1], [0],
//     [6171], [ATW→ORD], [28], [4], [1], [20],
//     [6173], [ORD→MCI], [28], [8], [4], [50],
//     [6156], [ORD→LIT], [28], [8], [2], [46],
//     [6043], [CVG→ORD], [28], [5], [3], [4],
//     [6038], [FSD→ORD], [28], [3], [1], [0],
//     [6114], [FWA→ORD], [28], [7], [5], [5],
//     [6039], [DSM→ORD], [28], [6], [5], [15],
//     [6183], [ORD→DAY], [28], [6], [5], [44],
//     [6181], [AZO→ORD], [28], [5], [4], [1],
//     [6141], [MHK→ORD], [29], [5], [4], [1],
//     [6173], [ORD→MCI], [29], [8], [6], [49],
//     [6109], [ORD→CMH], [29], [6], [1], [49],
//     [6156], [ORD→LIT], [29], [8], [2], [49],
//     [6128], [ORD→LEX], [29], [5], [4], [49],
//     [6114], [FWA→ORD], [29], [7], [1], [4],
//     [6029], [ORD→LAN], [29], [2], [1], [49],
//     [6025], [ORD→SGF], [29], [8], [1], [48],
//     [6039], [DSM→ORD], [29], [6], [5], [15],
//     [6183], [ORD→DAY], [29], [6], [4], [47],
//     [6075], [TYS→ORD], [29], [6], [4], [0],
//     [6181], [AZO→ORD], [29], [5], [4], [2],
//     [6110], [ORD→DAY], [29], [5], [4], [47],
//     [6141], [MHK→ORD], [30], [5], [4], [1],
//     [6022], [HSV→ORD], [30], [2], [1], [0],
//     [6162], [PIA→ORD], [30], [2], [1], [0],
//     [6173], [ORD→MCI], [30], [8], [7], [47],
//     [6043], [CVG→ORD], [30], [5], [4], [4],
//     [6067], [SDF→ORD], [30], [3], [1], [3],
//     [6156], [LIT→ORD], [30], [8], [2], [1],
//     [6082], [MHK→ORD], [30], [2], [1], [1],
//     [6114], [FWA→ORD], [30], [7], [2], [3],
//     [6045], [ORD→SPI], [30], [8], [2], [42],
//     [6113], [ORD→ATW], [30], [6], [1], [42],
//     [6039], [DSM→ORD], [30], [6], [5], [15],
//     [6072], [ORD→CLE], [30], [7], [4], [42],
//     [6181], [AZO→ORD], [30], [5], [1], [2],
//     [6089], [ORD→GRB], [30], [8], [1], [42],
//     [6102], [ORD→AZO], [30], [5], [1], [42],
//     [6174], [ORD→BMI], [30], [8], [1], [42],
//   ),
//   caption: [All 188 understaffed ZW flights, ordered by day. The four highlighted rows are the representative high-demand spoke departures discussed in the text, where rested normals idle at ORD cannot legally reach the one-way spoke.],
// ) <deadend-ZW>



#figure(
  image("img/crew_states_two_layer_ZW.svg", width: 100%),
  caption: [Crew states by layer (seniors on top, normals below).],
) <fig-crew-ZW>
#v(2pt)
\ \ \

#figure(
  table(
    columns: 4,
    align: (left, right, right, right),
    stroke: none,
    inset: (x: 9pt, y: 4pt),
    table.hline(),
    table.header([], [*Senior*], [*Normal*], [*Total*]),
    table.hline(stroke: 0.6pt),
    [Flying minutes],   [301,469],   [1,016,880], [1,318,349],
    [Deadhead minutes], [44,632],    [151,805],   [196,437],
    [Waiting minutes],  [1,412,225], [3,986,645], [5,398,870],
    table.hline(stroke: 0.4pt),
    [Flying cost (\$)],   [126,616,980], [101,688,000], [228,304,980],
    [Deadhead cost (\$)], [18,745,440],  [15,180,500],  [33,925,940],
    [Waiting cost (\$)],  [1,412,225],   [1,993,323],   [3,405,548],
    table.hline(stroke: 0.4pt),
    [Crew labour cost (\$)], [146,774,645], [118,861,823], [265,636,468],
    table.hline(stroke: 0.4pt),
    [Uncovered penalty (\$)],
      table.cell(colspan: 2, align: right)[578 slots $times 10^8$],
      [57,800,000,000],
    [*Grand total* (\$)], [], [], [*58,065,636,468*],
    table.hline(),
  ),
  caption: [ZW two-layer cost breakdown by crew class (668 active routes, 182 senior and 511 normal crew).],
) <cost-ZW>

== Things Abandoned
#figure(
  table(
    columns: 5,
    align: (left, center, center, center, center),
    stroke: none,
    inset: (x: 9pt, y: 4pt),
    table.hline(),
    table.header(
      [], table.cell(colspan: 2, align: center)[*G7*], table.cell(colspan: 2, align: center)[*ZW*],
      [], [$>= r_f$], [$= r_f$], [$>= r_f$], [$= r_f$],
    ),
    table.hline(stroke: 0.6pt),
    [Total solve time (s)],          [6,620],    [17,000], [3,965],    [7,501],
    [Slowdown vs inequality],        [—],        [2.6×],   [—],        [1.9×],
    [Windows at 1,800 s limit],      [0],        [6],      [0],        [1],
    [Suboptimal or infeasible],      [0],        [14],     [0],        [1],
    [Over-covered flights (L1, L2)], [282, 127], [0, 0],   [262, 194], [0, 0],
    table.hline(),
  ),
  caption: [Coverage as inequality ($sum "flow" >= r_f$) against equality ($sum "flow" + "slack" = r_f$, over-covering forbidden), per instance. All four columns solve the same model, about 2M variables and 1.2M constraints per window, so the runtime gap reflects lost LP integrality rather than problem size.],
) <overstaffing-bench>
#v(2pt)

\ \ \

#figure(
  table(
    columns: 5,
    align: (center, right, center, right, right),
    stroke: none,
    inset: (x: 9pt, y: 4pt),
    table.hline(),
    table.header([Window], [Variables], [Covered], [Gap], [Time (s)]),
    table.hline(stroke: 0.6pt),
    [0], [1,910,825], [316 / 320], [0.14%], [295.4],
    [1], [1,513,675], [285 / 285], [0.00%], [128.1],
    [2], [2,052,693], [362 / 362], [0.01%], [513.9],
    [3], [1,892,463], [350 / 352], [0.26%], [155.7],
    [4], [1,837,818], [364 / 364], [0.16%], [411.9],
    [5], [1,899,233], [345 / 346], [0.40%], [145.2],
    [6], [1,963,248], [357 / 357], [0.00%], [441.2],
    [7], [1,673,855], [368 / 369], [0.61%], [323.4],
    [8], [762,298],   [361 / 365], [0.12%], [59.5],
    [9], [218,047],   [364 / 365], [0.03%], [134.3],
    table.hline(stroke: 0.4pt),
    [*Total*], [], [], [], [*2,609*],
    table.hline(),
  ),
  caption: [Per-crew binary formulation on ZW, the smallest carrier in the dataset, across 10 rolling windows. Every window solves to optimality within the 1% gap. Stitched over the horizon the schedule covers 3,465 of 3,485 flights, 20 slots short, in about 43 minutes.],
) <percrew-zw>

#pagebreak()
= References
#bibliography("refs.bib", style: "elsevier-harvard", title: none, full: true)
