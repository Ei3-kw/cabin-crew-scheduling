// EJOR (European Journal of Operational Research) manuscript starter.
//
// EJOR is an Elsevier journal; there is no official Typst template. This uses
// the community `elsearticle` package, which mimics Elsevier's elsarticle.cls.
// Package + field reference: https://typst.app/universe/package/elsearticle/
//
// For initial submission EJOR wants a single-column, line-numbered manuscript,
// which is the `review` format below. Switch to a final layout (1p/3p/5p) only
// for camera-ready, and note that Editorial Manager expects LaTeX source — you
// will likely re-typeset the accepted paper in elsarticle LaTeX.

#import "@preview/elsearticle:3.1.0": *

#show: elsearticle.with(
  title: "A Working Title for Your Operational Research Paper",
  authors: (
    (
      name: "Ada First Author",
      // `affiliation` is a key (or comma-joined keys, e.g. "1,2") into the
      // `affiliations` dictionary below.
      affiliation: "1",
      corresponding: true,
      email: "ada.author@university.edu",
    ),
    (
      name: "Ben Second Author",
      affiliation: "2",
      corresponding: false,
      email: "ben.author@institute.edu",
    ),
  ),
  // `affiliations` is a dictionary: key -> affiliation text.
  affiliations: (
    "1": [Department of Operations Research, University of Somewhere, City 1000, Country],
    "2": [School of Management, Institute of Elsewhere, Other City 2000, Country],
  ),
  abstract: [
    Replace this with a concise abstract. State the problem, the operational
    research approach, the main methodological contribution, and the key result.
    Keep it self-contained and free of citations and undefined abbreviations.
  ],
  journal: "European Journal of Operational Research",
  keywords: ("OR in practice", "Optimization", "Heuristics"),
  format: "review",
  numcol: 1,
  line-numbering: true,
)

= Introduction

Motivate the problem and position it against recent OR literature. State the
contribution explicitly near the end of this section. Cite sources with the
`@key` syntax, for example @example2024.

= Problem Formulation

Define notation, decision variables, and constraints. Numbered equations look
like this:

$
min_(x in X) quad sum_(i=1)^n c_i x_i
$ <eq:objective>

Reference equations by label, e.g. @eq:objective.

For an unnumbered equation, tag it with the template's special label:

$
  g(x) = A x - b
$ <nonum-eq>

= Method

Describe the model, algorithm, or solution approach. Split distinct ideas into
their own subsections rather than one long block.

== Solution Approach

Outline the procedure here.

== Complexity

Comment on computational complexity or convergence here.

= Computational Experiments

Summarize instances, parameters, and hardware, then report results. A figure
placeholder:

#figure(
  rect(width: 60%, height: 4cm, stroke: 0.5pt),
  caption: [Replace with your result plot.],
) <fig:results>

Reference it as @fig:results.

= Conclusions

Summarize findings, limitations, and directions for future work.

// Appendix: everything after this show rule is treated as appendix material.
#show: appendix

= Supplementary Derivations

Move long proofs or extended tables here.

#bibliography("refs.bib")
