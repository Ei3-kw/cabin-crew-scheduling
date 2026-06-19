#set heading(numbering: "1.1")

#let notation(..rows) = table(
  columns: (auto, 1fr),
  stroke: none,
  column-gutter: 1.2em,
  inset: (x: 0pt, y: 3pt),
  align: (left + top, left + top),
  ..rows.pos()
)

= Introduction

When I was a kid I thought flight attendants had the best job in the world. They flew for free, they got to walk around the plane while everyone else sat strapped in, they had a whole galley of food within arm's reach, and they were paid to travel everywhere. It looked like someone had taken the fun parts of a holiday and handed them over as a salary.

What I never once thought about was how any of them ended up on a particular flight. I only started wondering after watching a video on how airlines actually schedule their crew — the bidding, the trip assignments, the rules stacked on top of rules — and coming away thinking the whole thing looked kind of broken for something so important. Underneath that mess is a real and surprisingly hard optimization problem, and that is what pulled me into this report.

== Background Problem

Crew is an airline's second-largest operating cost, behind only fuel, so how flights are staffed has a real effect on profitability. The cabin-crew pairing problem asks for a set of legal duty sequences, or _pairings_, that each begin and end at a crew member's home base, respect working-time rules, and together cover every scheduled flight at minimum cost. Cabin crew are harder to schedule than cockpit crew because they are heterogeneous: they are cross-qualified across aircraft types and split into classes, and how many of each class a flight needs depends on the aircraft's size and cabin layout. A regional turboprop might need a single attendant, while a widebody needs eight.

We model this in a deliberately simple form. Each flight needs between one and eight crew. Exactly one must be a _senior_, the rest can be anyone. A senior can fill any seat, but a normal can never fill the senior's. The senior is therefore the scarce, binding resource: it decides whether a flight can be staffed at all.

A pairing is legal only if it respects the operational clocks the model tracks. Two consecutive flights need at least a 45-minute turnaround between them. A duty period can build up at most 14 hours of block time before an 8-hour rest clears it. And a crew member can stay away from base for at most 4 days before owing a 48-hour home break.

Unlike formulations that plan around a single base and import "extra" crew at a flat penalty, we model the real geography: crew are based at many airports, can only originate and terminate duties at their own base, and must be physically positioned, by flying or deadheading, to wherever a flight departs. Coverage is thus limited not just by how many crew exist, but by whether the right class can legally reach the right place at the right time. That is the central tension the rest of this report addresses.

== Original Paper's methodology

= Problem Setups
== Flight Data Source

The flight schedule is real U.S. domestic data from the Bureau of Transportation Statistics (BTS) on-time performance dataset, which lists for every flight $f$ its operating carrier, tail number, origin and destination, scheduled departure and arrival times, and distance. What BTS does not record is how many cabin crew a flight needs, so we derive it from the aircraft itself. Each tail number is joined against the FAA Aircraft Registry to recover the aircraft model and its seat count $s_f$ -- the per-tail registered value where available, otherwise a type-level estimate from FAA Type Certificate Data Sheets -- and $s_f$ is mapped to a minimum cabin crew $r_f$ under U.S. regulation 14 CFR 121.391, where one attendant is required for every 50 seats:

$ r_f = ceil(s_f \/ 50) $

After enrichment the dataset spans 21 carriers, from mainline operators down to small regionals. This gives a natural range of network sizes and crew requirements to test against. We treat each carrier independently, with its own flights, airports, and crew pool, so an airline is one self-contained instance. The planning period is 30 days, extended by a return tail to a 34-day horizon so crew committed late can still be routed home. Only flights departing within the planning period must be covered; the rest sit in the tail.

The crew requirement is uneven across carriers, but within a single regional carrier it is essentially constant. A regional flies a near single-class fleet: one aircraft type, one seat band. So every flight maps to the same $r_f$. G7 is two attendants throughout (its regional jets seat 51 to 100), as are ZW and QX. Real per-flight variation appears only at mainline scale, where the fleet spans seat classes. B6 ranges over two to four, HA three to six, and AA three to eight. Size and density climb the same way. The regionals are small and sparse: G7 touches 51 airports over 143 routes, ZW 44 over 93. A mainline such as AA spans 119 airports and 836 routes at several times the density (@instance-table).

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

This leaves an awkward trade-off when picking test instances. The carriers with genuinely varied requirements (B6, HA, AA) are exactly the ones too large to solve in reasonable time. The small regionals that do solve have a flat requirement that never stretches the senior/normal split past a fixed one-plus-one. So we use two complementary instances. On the real data we focus on G7. It is a regional small enough to solve, and its true requirement of two (one senior, one normal) still puts the full two-layer machinery to work on a genuine schedule. For varied demand we keep that same small ZW network but swap its requirement for a random $r_f$, uniform over 1 to 8 (`data/flights_2025-01-random.csv`). That gives the mixed demand of a mainline on a network small enough to solve. It exercises cancellation, multi-seat fill and substitution in ways the uniform regionals never would.

== Two Layer structure

Each flight needs one senior plus $r_f - 1$ normals. Rather than solve for both at once, we run two passes that each place a single crew member per flight. This is about tractability. The crew-flow relaxation is integral only at unit demand, so the barrier method reaches an integer optimum without branching. The moment a flight needs two or more, the coverage constraints tie the flow across crew groups together, the relaxation turns fractional, and the solver has to branch. Treating the seniors (one per flight) and the normals ($r_f - 1$ per flight) as separate unit-demand passes keeps both in that easy regime. We solve the seniors first because they are the binding resource; the normals then fill in around the schedule the seniors fix.

=== Senior Layer (1 senior per flight)

Pass one stamps every flight with demand one and solves over a dedicated senior pool. Each flight gets exactly one senior, or none if none can legally reach it. A flight that gets no senior is _cancelled_: a normal can never fill the senior seat, so it is dropped and reported uncovered. The rest pass to the normal layer.

=== Normal Fill Layer ($r_f− 1$)

Pass two takes those surviving flights, re-stamps each with demand $r_f - 1$, and solves over a separate normal pool. Flights with $r_f = 1$ are already complete and carry no demand here; $r_f = 2$ stays unit-demand, higher values give small multiplicity. Normal identifiers are offset so they never collide with senior ones on merge.


== Crew Base Allocation

Choosing where crew are based and how many to base there is one heuristic, run once per layer. The base set is every airport the carrier flies both to and from; each such airport is a full crew base. For a base $b$ we estimate two loads. The first is its duration-weighted demand, the crew-minutes that originate there:

$ "dem"_b = sum_(f : "orig"_f = b) m_f delta_f $

where $m_f$ is the layer's per-flight demand (one for the senior pass, $r_f - 1$ for the normal pass) and $delta_f$ the block duration. The second is the peak concurrent load $"peak"_b$, the largest number of crew on duty at $b$ at once, found by sweeping the day and adding $m_f$ at each departure, removing it at each arrival.

The pool must exceed the raw demand, since crew are not available the whole horizon. An 8-hour overnight rest separates duty days, and a 48-hour home break follows every rotation of at most 4 days away, so over a six-day cycle only about four days are workable. A utilisation factor $u = 0.55$ absorbs these overheads; it is hand-tuned rather than derived, set empirically so busy bases are not undersized. For sizing we credit each crew a nominal eight duty-hours a day, well below the enforced 14-hour cap, so over the $|D|$-day horizon one crew supplies $tau = 8"h" times |D| times u$ duty-minutes. This eight-hour figure is a sizing assumption only; the schedules themselves are bound by the 14-hour duty limit. The base size is then

$ s_b = max(ceil(1.8 thin "dem"_b \/ tau), thin ceil(1.8 thin "peak"_b), thin s_(m i n)) $

a $1.8$ slack over the larger load, floored at a small per-base minimum $s_(m i n)$, with a 10% Gaussian jitter on each count. The $1.8$ covers positioning (a crew often deadheads to where it is needed) and short demand peaks. It is ample: the pools supply two to nearly five times the crew-minutes flown (a coverage ratio of $2.8$ for G7, around four for the randomised ZW), so headcount is never binding. Coverage is limited by geography and legality, not numbers, so the slack could even be tightened to shrink the model.

Running this twice, on the senior demand and then the normal demand, gives two pools each matched to its own layer. Sizing one pool and splitting it by a fixed ratio was tried first, and it mismatched both layers.


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
  defrow($B_a subset.eq P$,[Crew home bases of airline $a in A$ (= every airport with flights to \& from)\ We would use $B$ from onwards as we look at one airline at a time]),
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
  defrow($m_f$, [Per-flight demand for the layer being solved: $1$ if $ell = 1$, else $r_f - 1$]),
  defrow($rho = 0.30$, [Home-return deadhead discount, applied when $"dest"_f = b$]),
))

== Network
Times are integer minutes from the horizon start; one day $= 1440$ min, and $"floor"_"day"(x) = 1440 floor(x \/ 1440)$ rounds a deadline down to a whole day.
#deftable((
  defrow($n = (p, t, e)$, [State node: airport $p in P$, minute $t$, break-expiry $e$ (latest minute the next $>= Delta_(h b)$ home break may finish)]),
  defrow($cal(N)_w$, [All nodes of window $w$'s time-expanded network]),
  defrow($cal(A)_w$, [All arcs, partitioned $cal(A)_w = cal(A)^"fl" union cal(A)^"dh" union cal(A)^"wt"$ (flight / deadhead / wait)]),
  defrow($delta^+(n), delta^-(n)$, [Arcs leaving / entering node $n$]),
  defrow($Delta t_a$, [Elapsed minutes of arc $a$]),
  defrow($c_a$, [Cost of arc $a$ (defined piecewise in the objective)]),
  defrow($cal(G)_w$, [Clock-groups: crew sharing an identical expanded graph; $g in cal(G)_w$]),
  defrow($K_g = |g|$, [Number of interchangeable crew in group $g$]),
  defrow($cal(N)_g, cal(A)_g$, [State nodes and expanded arcs available to group $g$]),
  defrow($cal(A)_(g,f)^"fl"$, [Flight arcs of leg $f$ within group $g$]),
  defrow($"src"_g, "snk"_g$, [Depot state and collapsed horizon sink of group $g$]),
))

== Variables
The model is solved once for each $ell in L$; the two instances share all structure —
same $cal(N)_w$, $cal(A)_w$, $cal(G)_w$, variables $x_(g,a)$ and $u_f$, and rows — and
differ only in their data: the pool ($S_b$ or $N_b$), the demand $m_f$, and the rates
$c_(f l,ell)$, $c_(w t,ell)$. Layer $0$ (normals) additionally runs only over legs that
received a senior in layer $1$.
#deftable((
  defrow($x_(g,a) in {0, ..., K_g}$, [Crew flow: number of group-$g$ crew traversing arc $a in cal(A)_g$]),
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

Each $w in W$ is solved as the single-window model below, in order. Only $F_w$ is committed (frozen and written out); legs after $t_w^"commit"$ are re-solved by later windows.

=== Seaming
Two boundary terms close the seam. $T_(t a i l) = d_(a w a y)$ leaves room to route every committed crew home within the away cap, and the soft set $F_(w')$ pulls crew into position for the next window's first bank.

The seam exists because of where coverage fails. In a trial run on ZW, $14$ of $20$ uncovered legs fell in the first $8$ h of a window: a leg leaving a spoke early on day $t_w^"start"$ can only be worked by a crew already rested there, but the deadhead that positions them must depart the evening before — inside the previous window, which no longer sees the leg as its responsibility and so never pre-positions. Marking these legs as $F_(w')$ gives that previous window a reason to commit the positioning deadhead in its own commit region, so the next window inherits a rested crew and covers the leg for real. The penalty is deliberately soft, $c_(u n c') = 10^6$: above the positioning cost (deadhead $+$ wait $+$ leg, $tilde.eq 1.5 times 10^4$) so the solver does pre-position, but far below $c_(u n c) = 10^8$ so it never sacrifices a committed cover to chase a seam one. Seam legs are re-solved by the next window, so they never count as uncovered here.

=== Window Carry-over
At each seam the next window starts from the committed state of the previous one — the only link between windows; there is no constraint coupling them, so window $w + 1$'s depot is pinned to where $w$'s frozen route left each crew, and the boundaries match by construction. Two things carry across: each crew's committed end position — the airport it occupies when the committed region closes — and its break-clock state, summarised by a single anchor, the minute the crew last left home after a completed home break, from which the away budget keeps counting. The subtlety is what counts as a completed break: only a 48-hour home stay whose full 48 hours elapse inside the committed region advances the anchor. A break the solver schedules in the uncommitted tail is deliberately not credited, because the tail is re-planned by the next window and that break may never actually be flown. A crew that is home at the seam but has served only part of its break carries the start of that home stay forward, so the next window finishes the remaining hours rather than restarting a fresh 48 — this neither forces an early return nor grants a free reset.


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
  a *flight* arc at $c_(f l,ell) delta_f$ (earns coverage) and a *deadhead* arc at $c_(d h,f)$ (does not; scaled by $1 - rho$ when $"dest"_f = b$).

+ *Reachability pruning.* For all crew $c$ with depot $o_c$ and home base $b_c$,
  $ cal(A)_c = { a in cal(A)_w : "both ends of" a in "Fwd"(o_c) inter "Bwd"(b_c, t_w^"hor") }, $
  the forward/backward Dijkstra reachable sets, dropping mid-sweep any arc that breaches the away cap $d_(a w a y)$. The sweep also cuts runs past $d_(w o r k)$ consecutive duty days — not a rule but a size bound (see Structural Constraints).

+ *Break-clock expansion.* Lift every reachable $(p, t)$ to states $(p, t, e)$, $e$ the latest minute the next $>= Delta_(h b)$ home break may finish. For all home stays $>= 60 Delta_(h b)$ a free reset arc re-anchors $e <- "floor"_"day"(t' + 1440 d_(a w a y) + 60 Delta_(h b))$; any non-reset arc is admitted iff its head time $<= e$. All states $(p, t_w^"hor", dot)$ collapse to one sink. Bucketing $e$ to whole days ($"floor"_"day"$) is what keeps the state space finite: states share an expiry whenever their deadlines fall on the same day, so $e$ takes only a handful of values rather than a distinct minute-resolution clock per crew, and rounding down never overstates a deadline, so no illegal route slips through.

+ *Clock-group aggregation.* Crew with an identical expanded graph form a group $g in cal(G)_w$, $K_g = |g|$; the model carries one flow $x_(g,a)$ for all $g in cal(G)_w, thick a in cal(A)_g$.

=== Structural Constraints
The model carries only flow-balance and coverage rows; every other rule is baked into $cal(N)_w$ and $cal(A)_w$, so any flow that conserves is already legal:

#deftable((
  defrow($Delta_(t a)$, [Turnaround — flight arcs land on the snapped node $"arr"_f + Delta_(t a)$]),
  defrow($Delta_(d u t y)$, [Duty cap — flight arcs whose cumulative duty exceeds $60 Delta_(d u t y)$ are dropped]),
  defrow($Delta_(r e s t)$, [Rest — a wait arc of $>= 60 Delta_(r e s t)$ zeroes the duty clock]),
  defrow($d_(a w a y), Delta_(h b)$, [Away cap and home break — carried by each state node's expiry $e$, reset only by a $>= Delta_(h b)$ home stay]),
))

The LP is thus small and integral — conservation plus unit-coefficient coverage, with all legality in the arcs. Conservation also hands each crew a single trajectory: it leaves every state on exactly one arc and time strictly advances, so no crew is booked on two legs — or a leg and a wait — at once. One thing at a time is structural, not a row.

$d_(w o r k) = 3$ is not enforced as a rule. The reachability sweep drops runs of more than three consecutive duty days purely to shrink the graph: once the overnight rests between duty days are counted, such a run almost always already breaches the $d_(a w a y)$ away cap, so cutting it early discards essentially no legal route. The only place it actually binds is the greedy senior-substitution gate, which re-checks it explicitly.

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

- (1) Conserves crew flow: $K_g$ crew leave each group's depot state, $K_g$ are absorbed at its horizon sink, and in- and out-flow balance at every other state.
- (2) Covers each leg: crew flow on its flight arcs plus shortfall slack meets the layer demand $m_f$.
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
  image("img/clock_group_aggregation.svg"),
  caption: [Crew with an identical carry-state signature collapse into one clock-group, decomposition reverses the arrows.],
) <fig-groups>

The split preserves everything the model enforced. Coverage is a constraint on the per-arc flow ($sum$ flight-arc flow $+ thin u_f >= m_f$), and decomposition keeps each arc's count exact, so every leg keeps its crew. Each path lies inside the expanded graph, so turnaround, duty, rest, the away cap, and the home break already hold — nothing is re-checked. Because a group's crew are interchangeable, the paths attach to crew-ids in any order (an optional tie-break balances duty hours or holds ids steady across windows); seam continuity comes from carry-over.

=== Senior Substitution in Idle Gaps

// After both passes, some surviving flights are still short a normal. A senior can fill a normal seat, but only opportunistically: inside an idle gap of its own layer-1 route, never displacing a senior duty. This is a greedy post-processing pass, not part of either MIP. It fills one seat per gap and writes each accepted fill back as a real flight leg on the senior's route, so the schedule, visualiser and validator all reflect it.

// An idle gap runs from a senior's arrival at an airport up to its next senior departure, or to the end of its route. A candidate fill flight $f$ must clear two gates.

// ==== Geometric Feasibility
// The senior must be parked at $f$'s origin $"orig"_f$, rested across the gap (the 8-hour minimum, so $"dep"_f - "arr"_"prev" >= 480$ minutes), and, when a senior duty follows the gap, able to connect to it: $f$ must land where that duty departs ($"dest"_f = "orig"_"next"$), leaving the 45-minute turnaround, $"arr"_f + 45 <= "dep"_"next"$. A gap with no following duty clears this trivially.

// ==== Route Legality
// This is what the gate geometry alone misses: a senior parked far from base could satisfy the geometry yet break the away cap by flying a fill hours or days later. So we insert $f$ into the senior's route, re-sort by departure, and re-check the whole route against the same rules the model enforces. No leg may depart more than 4 days after the senior last left home with no 48-hour break in between, and no run of consecutive duty days may exceed 3 days. The check is cumulative, so several fills on one senior stay jointly legal. A gap failing either gate yields nothing, which makes the pass best-effort: it recovers a handful of understaffed flights and leaves the rest short.

After both layers solve and decompose, some surviving flights are still short a normal. A senior can fill a normal seat, but only opportunistically: inside an idle gap of its own senior route ($ell = 1$), never displacing a senior duty. This is a greedy post-processing pass, not part of either MIP — it fills one seat per gap and writes each accepted fill back as a real leg on the senior's route, so the schedule, visualiser, and validator all reflect it.

An idle gap runs from a senior's arrival at an airport up to its next senior departure, or to the end of its route (@fig-sub). A candidate fill flight $f$ must clear two gates.

#figure(
  image("img/senior_substitution_idle_gap.svg"),
  caption: [A fill flight slotted into an idle gap],
) <fig-sub>

==== Geometric Feasibility
The senior must be parked at $f$'s origin $"orig"_f$, rested across the gap ($"dep"_f - "arr"_"prev" >= 60 Delta_(r e s t)$), and, when a senior duty follows the gap, able to connect to it: $f$ must land where that duty departs ($"dest"_f = "orig"_"next"$), leaving the turnaround $"arr"_f + Delta_(t a) <= "dep"_"next"$. A gap with no following duty clears this trivially.

==== Route Legality
Geometry alone misses a slower failure: a senior parked far from base could satisfy it yet break the away cap by flying a fill hours or days later. So we insert $f$ into the senior's route, re-sort by departure, and re-check the whole route against the rules the validator applies — no leg departing more than $d_(a w a y)$ days after the senior last left home without a $Delta_(h b)$ break, and no run of consecutive duty days past $d_(w o r k)$. The check is cumulative, so several fills on one senior stay jointly legal. A gap failing either gate yields nothing, which makes the pass best-effort: it recovers a handful of understaffed flights, and leaves the rest short.

=== Barrier vs Simplex
The per-window relaxation is large and sparse, so it is solved by barrier (interior-point) rather than simplex, which burned the whole limit pivoting at the root. For unit-demand models — the senior layer, or any single-crew airline — the group-flow relaxation is integral, so barrier lands straight on an integer optimum: no branching, and crossover to a simplex basis is pure overhead, so it is switched off. These windows solve in well under a minute.

Once a flight needs more than one crew, coverage couples flow across groups and breaks that integrality, so the relaxation is fractional and the model must branch (the consequences are in the results). Each solve stops at a 1% gap or 30 minutes; unit-demand windows close at a zero gap, so the limit binds only on these branching windows. For them an optional probe — off by default — runs crossover-free for a few seconds and, if still far from optimal, restarts with crossover on and the solver set to favour integer-feasible solutions, giving branch-and-bound a basis it can warm-start from.

=== Deterministic Model Construction

The expanded graph is built by exploring states held in hash sets, whose iteration order depends on the process hash seed. Identical inputs therefore produced models with their variables in different column orders from one run to the next, and because the crew-flow model is highly degenerate -- many interchangeable crew and equivalent paths -- the solver's anti-degeneracy effort swung sharply with that order, the same window taking anywhere from thirty seconds to over a hundred. Sorting arcs and nodes by value before they are handed to the solver makes the constructed model byte-identical across runs -- same fingerprint, same result, same time -- which removes the variance, makes timings comparable, and as a side effect presolves slightly smaller, since the regular ordering is easier to reduce.

Reproducibility rests on one more fixed point: the synthetic crew pool — its per-base counts and the 10% jitter — and the random load factors are all drawn from a single fixed seed, so a given airline and horizon regenerate the identical instance every run.



= Results

== G7

G7 is a small regional (@instance-table): $5141$ flights over the 30-day horizon across $51$ airports and $143$ routes, requirement two — one senior, one normal. It is chosen not for size but because it solves to optimality in every window while still exercising the full two-layer machinery.

=== Coverage breakdown
Of the $5141$ flights, $5069$ ($98.6%$) are fully crewed, $10$ fly one normal short, and $62$ ($1.2%$) are cancelled for want of a senior. The senior layer covered $5079$ of $5141$ ($98.8%$); the normal fill left $19$ seats short, of which substitution recovered $9$. The schedule is flat and saturated — about $171$ flights a day, fully crewed on almost every bar (@fig-schedule-G7). The residual is concentrated: cancellations show only at the day-$30$ tail ($16$), with spoke days $11$ and $18$ ($6$ each) the only earlier blips; the short bars on days $6$ and $19$ are light-traffic days, not failures.

#figure(
  image("img/whole_schedule_coverage_by_day_G7.svg", width: 100%),
  caption: [Flights per day, stacked by crewing outcome.],
) <fig-schedule-G7>

=== Cancelled flights
All $62$ cancellations come from layer 1 (no senior); they fall into three bands (@uncovered-table-G7).

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
    [Reachable inbound but no rested crew for the return: EWR→AVL is reachable by 151 crew, AVL→EWR by 4. The away cap strands any crew flown in; 10 have the reverse leg covered.],
    [Needs a relaxed away cap or a deadhead-home leg],

    [Rolling-horizon tail (hub origins, day $>= 28$)],
    [26],
    [Sit in the last window's tail: the return tail $T_(t a i l)$ runs past the flight data and no later window re-solves them. 16 depart on day 30.],
    [Needs a longer data horizon],
  ),
  caption: [The 62 uncovered G7 flights by cause.],
) <uncovered-table-G7>

None of this is an optimality-gap artifact, and the reason also fixes what _recoverable_ means. Every window solves to optimality over a totally-unimodular network (integral LP, no gap), and each cancellation costs $c_("unc") = 10^8$ — $62 times 10^8 = 6.20 times 10^9$, or $96%$ of G7's $6.48 times 10^9$ objective. At that price the solver positions crew wherever it legally can, positioning being a deadhead arc chosen inside the MIP and far cheaper than $10^8$; so an uncovered flight is never a missed deadhead but one the constraints forbid. The directional band is blocked by the away cap $Delta_("away") = 4$ days — the round-trip to a one-way spoke will not fit it — and the tail runs past the data. Recovering either is a macro change, not better routing: that is all _recoverable_ means here.

The directional band also clusters at window starts (@onedir-window-G7), which looks like a seam effect — but a longer seam barely touches it. Most of these flights already sit inside the current half-day look-ahead, so the previous window saw them and still found no return crew; more reach adds no crew when the bind is the away cap.

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
  caption: [The 18 one-directional flights by window position. _Frac_ is the fraction through the 3-day window; _In seam?_ marks those inside the half-day look-ahead.],
) <onedir-window-G7>

=== Understaffing and substitution
A flight that keeps its senior but loses its normal can be filled post-solve by an idle senior already at the origin — something the normal layer cannot do, as seniors are not in its pool (@sub-table-G7). Each fill is co-located, so no deadhead is needed, and they cluster early when the pool is freshest; the $10$ that remain had no co-located senior and no normal the solve could reach.

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

The $10$ still short all operate, one seat under (@understaffed-window-G7). The fixes that don't work are the informative part. The seat costs the same $10^8$ as a cancellation (layer 2's $19$ short seats are $1.9 times 10^9$, nearly its whole objective), so the solver would fill any reachable one — it is not under-priced. Nor is it scarcity, with $120$–$190$ normals idle system-wide each time, nor seam reach, since most already sit inside the look-ahead. The rested normals that show at the origin are a merged-timeline illusion: at $10^8$ a seat a truly free crew would have been used, so these are committed in an adjacent window or held by a home-break or away-cap rule — visible globally, absent from the per-window feasible set.

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

So the bind is the decomposition itself. Like the cancellations, understaffing is a $10^8$ penalty paid because the sub-problem has no assignable crew — but here the crew exists, in a neighbouring window. Recovering it needs a less decomposed solve, wider window overlap or a joint re-solve, plus the horizon extension for the day-$30$ flight: $10$ seats, $0.2%$, short because the crew that could fill them sit in another sub-problem.

From the crew side (@fig-crew-G7), both pools look much the same: only a thin slice is flying at any moment (blue), most crew are resting or on break, and the dominant grey band is crew simply available. Even at the daily peaks roughly half the pool is idle, and usually more. That deep reserve is what makes senior substitution cheap — an idle senior is often already where a normal is missing — and it is the same idle crew the decomposition cannot reach across window boundaries.

#figure(
  image("img/crew_states_two_layer_G7.svg", width: 100%),
  caption: [Crew states by layer],
) <fig-crew-G7>



== ZW with Random $r_f$

ZW is a random-demand instance: each flight's requirement $r_f$ is drawn across $1$ to $8$ rather than fixed at one. It has $3594$ flights over $30$ days, $182$ seniors and $511$ normals, and solves to optimality in every window. The heavy, variable demand makes understaffing, not cancellation, the dominant residual.

=== Coverage breakdown
Seat-demand totals $16{,}307$ across the $3594$ flights: one senior seat each and $12{,}713$ normal seats, a mean of $4.5$ crew per flight. The senior layer cancelled $24$ flights ($0.7%$); the normal layer left $510$ of the $12{,}713$ seats short ($96.0%$ filled), of which substitution recovered $33$. By flight, $3400$ ($94.6%$) are fully crewed, $170$ fly under $r_f$, and $24$ are cancelled. The shortfall is not spread evenly: it is negligible through the first half and climbs across the back half to a peak near day $26$ (@fig-schedule-ZW).

#figure(
  image("img/whole_schedule_coverage_by_day_ZW.svg", width: 100%),
  caption: [Flights per day, stacked by crewing outcome.],
) <fig-schedule-ZW>

=== Cancelled flights
All $24$ cancellations are layer-1 failures (no senior), and every one is an ORD–spoke connection where the senior pool runs too thin. Fifteen are inbound spoke→ORD legs — LIT recurs three times, each needing $8$, with MHK and MKE twice each — four form two complete day-$2$ round trips (ORD↔IND, ORD↔COU) whose routes never get a senior, and five are a day-$30$ tail of ORD→spoke departures whose return runs past the horizon. The normal fill never sees them.

=== Understaffing
Understaffing is ZW's real residual, and its cause is not the one the build-up suggests. The empty seat is priced at $10^8$, the same as a cancellation — layer 2's $510$ short seats are $5.1 times 10^10$, its entire objective — so the solver fills any seat it can reach. And the pool is far from spent: $350$–$400$ of the $511$ normals sit idle at midday on every day of the month, the worst understaffing days included. So it is neither under-pricing nor a headcount shortage; more crew would sit just as idle.

What concentrates the shortfall is random demand meeting thin geography. The worst-hit flights are high-$r_f$ departures into spokes that cannot cycle the crew back (@deadend-ZW). ORD→OMA needs seven, but the single daily OMA→ORD leg seats one, so six crew flown out would strand and the seats stay empty though dozens of normals idle at ORD; MCI, SPI and LIT repeat the pattern, their return capacity or its timing short of the outbound demand. The idle ORD reserve is real but unusable: positioning crew out to a one-way spoke breaks the away cap on the way home. Because $r_f$ can reach $8$, an unreachable spoke shows up not as one cancelled flight but as a six- or seven-seat shortfall on a flight that still operates.

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

The back-half build-up is not depletion. Demand is nearly flat — mean normal seat-demand is $413$ a day early and $433$ late — and the pool stays idle throughout, so the rise is the rolling horizon, not exhaustion: as windows hand off, the crew distribution drifts hub-ward, leaving fewer normals pre-positioned at the spokes where late high-demand legs need them, and the per-window partition then widens those spoke shortfalls. The levers follow from the cause — a relaxed away cap or a permitted deadhead-home to let crew reach one-way spokes, wider window overlap to share the idle reserve across boundaries, a longer horizon for the day-$30$ tail — and not more staff, whose only effect would be a larger idle reserve.

The reserve is plain on the crew side (@fig-crew-ZW): the normal pool spends most of the month available, a deep idle band the schedule never draws down — because the seats it cannot fill are unreachable, not unstaffed.

#figure(
  image("img/crew_states_two_layer_ZW.svg", width: 100%),
  caption: [Crew states by layer (top: seniors; bottom: normals).],
) <fig-crew-ZW>





==== Senior as the Binding Resource

==== Senior Substitution Outcomes

==== Two-Layer Tractability
===== Multi-Crew as Two Unit-Demand Layers
===== Effect of Barrier on Large Windows

Solving the multi-crew requirement directly -- one model with $r_f > 1$ -- shows why the two-layer split matters. With every G7 flight needing two crew, the coverage constraints make the root relaxation fractional, so the barrier point is no longer integer and the model must branch. Most windows still resolve at the root, but the windows late in the horizon, where the return tail runs past the end of the flight data and connectivity is sparse, stall: the relaxation bound is loose -- the LP "covers" flights with fractional flow that no whole crew can realise -- and, with crossover off, each branch-and-bound node re-solves its LP without a warm-start basis, so node throughput collapses. Three of the ten windows hit the thirty-minute limit at 24 to 83 per cent gap with badly degraded coverage. The two-layer decomposition avoids this entirely: each layer is unit-demand and therefore root-integral, so every window solves quickly -- the same airline that stalls as one model runs cleanly as two layers.

==== Schedule Validation
===== Rule-Compliance Checks
===== Window-Boundary Carry-over Fix

An earlier carry-over read each crew's break deadline straight off the last committed arc of the expanded graph. Because the cost-minimising solution within a window is free to park the mandatory 48-hour break in the uncommitted tail, that arc reported a deadline as if the break had already been served, so the anchor advanced every window even though no break was ever committed. The away clock therefore reset at each seam and drifted forward, letting crew accumulate five to seven days away and double-digit consecutive duty days across the merged schedule -- on the G7 instance the independent validator flagged 352 away-cap and 341 consecutive-duty violations, none of which were visible window-by-window. Tracking the anchor on the committed legs only, exactly as the validator reconstructs it, and crediting a partly-served break across the seam, removes the drift: the same instance then validates with zero violations of any rule while senior coverage is essentially unchanged at 5001 of 5141 flights, confirming the violations were a boundary-accounting artefact rather than illegal routes the solver actually wanted.

=== Model Scaling Limits












= Things Attempted but Left Out

=== Per-crew Binary

=== Direct Multi-Crew (min_crew > 1) Solve
==== Barrier with Crossover Disabled

Enabling crossover globally, to give branch-and-bound a warm-start basis on the fractional multi-crew windows, was a net loss. The easy windows -- which solve at the root and never branch -- paid a large crossover cost for nothing: forcing a basis on a million-variable degenerate model meant pushing hundreds of thousands of variables to a vertex (with a restart), and one window grew from about seven to twenty-two minutes for no benefit. A targeted variant was kept as an opt-in instead: probe with crossover off, and only if a window times out while still branching far from optimal, switch crossover on and re-solve with the full budget. This rescues the mid-size stalled windows without taxing the majority, but it does not help the largest window, which is stuck in root processing before branching even starts -- a model-size problem, not a missing-basis one.

==== Looser Time Limit

=== Mainline-Scale Airlines

=== DDD dead loop


=== A Separate counter for working days for each time away window
- It's basically the same thing as timeaway but blows up the model quite a bit
- merged into mandetory 48h home break every 4 days

=== Longer Away Window (7-day → 4-day)
- the return tail T_tail equals D_AWAY, so a 7-day away cap forces a 7-day tail
  → 14-day window horizon (vs 11-day at 4 days) → much larger graph/model per window
- dropped to a 4-day away cap (mandatory 48h home break), shrinking each window
- also let the separate consecutive-duty-days counter fold in (above)

=== Cutting Planes
vry slow, and it kinda didnt work.... got clean violations
======================================================================
RULE-VIOLATION SUMMARY
======================================================================
  continuity  :    0 violation(s)
  overlap     :    0 violation(s)
  turnaround  :    0 violation(s)
  d_away      :    0 violation(s)
  home_break  :    0 violation(s)

  TOTAL: 0 violations

but its cuz the d_away cut holding everyone under 4 days, the "48h break after a 4-day rotation" rule will essentially never fire — crew instead rest at home in shorter, more frequent stretches
301ed51

=== Warm Starts
`Warning: Completing partial solution with 773441 unfixed non-continuous variables out of 773446`

hint prior-window routes via VarHintVal instead of partial Var.Start, dr…
…opping Gurobi's ~80s completion sub-MIP

diff warm start attempts through three stages. F

the Gurobi log showed the original `_apply_warm_start` was setting `Var.Start=1` on only matched flight arcs and leaving every wait arc undefined, producing a near-empty partial start — `Completing partial solution with 773441 unfixed … out of 773446` (only 5 variables fixed). Gurobi reacted by running a completion sub-MIP that burned the whole time budget (`0 nodes explored in subMIP` for ~300s, first incumbent at 247s). So I rewrote it to reconstruct each crew's complete in-window flow by walking the wait-arc spine (depot → wait → leg → wait → leg …) and added instrumentation reporting match rate and coverage.

That instrumentation produced the key evidence: matching was *perfect* (`legs 11/11 matched`, `14/14 matched`) but only ~11–14 legs carried across each window boundary, hinting just 0.1–0.4% of variables (`2320/619830`, `939/788706`). The cause is structural, not a bug — each window only flies its committed 3-day region and leaves the 4-day overlap idle because tail flights aren't in `F_cov` and so carry no coverage reward, leaving almost nothing to seed. Worse, even that sparse start still triggered the completion sub-MIP (`Completing partial solution with 617795 unfixed`, then ~80s of `0 nodes` yielding a throwaway `2.02e8` incumbent), making the warm start net-negative: window 1 with it ran 129.6s versus window 0 without it at 106.1s. The fix was to switch from `Var.Start` to `VarHintVal` — hints feed the same guidance to Gurobi's heuristics without invoking the completion sub-MIP, removing the ~80s overhead.

=== Banning overcovering

== Future Work / Proposals

=== Two-Phase (Lexicographic) Objective
// minimise uncovered slots first, then cost — avoid the big-M poisoning of the multi-crew MIP

=== Joint Senior + Normal Two-Commodity Flow
// exact substitution in one model vs the sequential decomposition

=== Flow-Based Senior Substitution
// allow deadhead-back so substitution isn't limited to one-way idle-gap fills

=== Coordinated Two-Layer Positioning
// seed the normal layer with the senior layer's positions to cut understaffing

=== Scaling to Mainline-Size Airlines
==== Shorter Window Horizon
==== Coarser Time Bucketing
==== Column Generation

=== Deterministic Tie-Break for Trajectory Stability

=== Realistic min_crew Data

