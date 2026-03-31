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
    subtitle: [with single-rooms entitlements],
    author: [Ella Wang],
    date: datetime.today(),
    institution: [],
  ),
)

// #set heading(numbering: numbly("{1}.", default: "1.1"))

#title-slide()

// == Outline <touying:hidden>


= Thank U :3
