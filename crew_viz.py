"""
crew_viz.py  —  Crew scheduling network visualiser
Usage:  python crew_viz.py <input.json> [output.html]
        python crew_viz.py flights_mini_result.json          # → crew_viz.html
        python crew_viz.py flights_mini_result.json out.html # → out.html

Expected JSON schema
--------------------
{
  "meta": {
    "days": int,
    "horizon_end": int,          # minutes
    "solve_status": str,
    "total_cost": float,
    "flight_cost": float,
    "deadhead_cost": float,
    "wait_cost": float,
    "uncovered_slots": float,
    "num_flights": int,
    "covered_flights": int
  },
  "crew": [{"id": int, "base": str}, ...],
  "flights": [
    {"id": int, "flight_num": str, "origin": str, "dest": str,
     "dep_min": int, "arr_min": int, "duration": int, "min_crew": int},
    ...
  ],
  "routes": [
    {"crew_id": int, "base": str, "legs": [
      {"type": "flight"|"deadhead", "from": str, "to": str,
       "dep": int, "arr": int, "flight_id": int},
      ...
    ]},
    ...
  ],
  "uncovered_flights": [
    {"flight_num": str, "origin": str, "dest": str,
     "dep_min": int, "arr_min": int, "missing_slots": float},
    ...
  ]
}

Coverage logic
--------------
A flight appearing in uncovered_flights AND in routes  → "partial"  (amber)
A flight appearing in uncovered_flights but NOT routes → "uncovered" (red)
All other flights                                       → "covered"  (green)
"""

import json
import sys
import os

AIRPORT_POSITIONS = {
    # Roughly geographic layout for a 860×540 canvas.
    # Keys are IATA codes. Add/override here for other networks.
    "JFK": (690, 148),
    "LAX": (105, 228),
    "ORD": (418, 112),
    "MIA": (615, 398),
    "SFO": (80,  200),
    "DFW": (380, 310),
    "ATL": (560, 310),
    "BOS": (720, 120),
    "SEA": (90,  110),
    "DEN": (280, 210),
    "LAS": (155, 260),
    "PHX": (200, 300),
}

DEFAULT_POSITION_RADIUS = 240   # fallback circle radius for unknown airports
DEFAULT_CENTER = (430, 270)


def auto_position(codes):
    """Place unknown airports evenly around a circle."""
    import math
    known = {c: AIRPORT_POSITIONS[c] for c in codes if c in AIRPORT_POSITIONS}
    unknown = [c for c in codes if c not in AIRPORT_POSITIONS]
    n = len(unknown)
    for i, code in enumerate(unknown):
        angle = 2 * math.pi * i / max(n, 1) - math.pi / 2
        x = DEFAULT_CENTER[0] + DEFAULT_POSITION_RADIUS * math.cos(angle)
        y = DEFAULT_CENTER[1] + DEFAULT_POSITION_RADIUS * math.sin(angle)
        known[code] = (round(x), round(y))
    return known


def build_html(data: dict) -> str:
    meta = data["meta"]
    horizon = int(meta.get("horizon_end", 4320))

    # Gather all airport codes
    ap_codes = set()
    for f in data["flights"]:
        ap_codes.add(f["origin"])
        ap_codes.add(f["dest"])
    ap_positions = auto_position(sorted(ap_codes))

    # Serialise positions for JS
    ap_js = "{" + ",".join(
        f'"{k}":{{"x":{v[0]},"y":{v[1]}}}'
        for k, v in ap_positions.items()
    ) + "}"

    data_js = json.dumps(data, separators=(",", ":"))

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crew scheduling network</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:system-ui,sans-serif;background:#f8f8f7;color:#1a1a1a;padding:16px 20px}}
h1{{font-size:16px;font-weight:500;margin-bottom:12px;color:#1a1a1a}}
#controls{{display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap}}
#scrubber{{flex:1;min-width:200px;accent-color:#185FA5}}
#tdisp{{font-size:13px;font-weight:500;min-width:148px}}
#airborne{{font-size:12px;color:#666;white-space:nowrap}}
#filters{{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:10px;font-size:12px;color:#555}}
#filters label{{display:flex;align-items:center;gap:5px;cursor:pointer;user-select:none}}
.sw{{display:inline-block;width:20px;height:3px;border-radius:2px;vertical-align:middle}}
#svg-wrap{{position:relative;border:0.5px solid #ddd;border-radius:10px;background:#fff}}
#nsvg{{display:block;width:100%;max-height:560px}}
#tip{{position:absolute;background:#fff;border:0.5px solid #bbb;border-radius:8px;
  padding:9px 13px;font-size:12px;pointer-events:none;opacity:0;
  transition:opacity .12s;max-width:240px;line-height:1.7;z-index:20;
  box-shadow:0 2px 8px rgba(0,0,0,.08)}}
#tip.on{{opacity:1}}
#tip b{{font-weight:500;display:block;margin-bottom:2px}}
#tip s2{{color:#666;display:block}}
#legend{{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:#666;
  margin:10px 0;align-items:center}}
.li{{display:flex;align-items:center;gap:5px}}
.sw-dashed{{height:2px;background:repeating-linear-gradient(
  90deg,#7F77DD 0,#7F77DD 4px,transparent 4px,transparent 8px)}}
.sw-partial{{background:repeating-linear-gradient(
  90deg,#EF9F27 0,#EF9F27 4px,#E24B4A 4px,#E24B4A 8px);height:2px}}
#stats{{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}}
.pill{{background:#f1efe8;border-radius:8px;padding:5px 11px;font-size:12px;color:#555}}
.pill strong{{color:#1a1a1a;font-weight:500}}
</style>
</head>
<body>
<h1>Crew scheduling network</h1>
<div id="filters">
  <label><input type="checkbox" id="fc" checked>
    <span class="sw" style="background:#1D9E75"></span>Fully covered</label>
  <label><input type="checkbox" id="fp" checked>
    <span class="sw" style="background:#EF9F27"></span>Partially covered</label>
  <label><input type="checkbox" id="fu" checked>
    <span class="sw" style="background:#E24B4A"></span>Uncovered</label>
  <label><input type="checkbox" id="fd" checked>
    <span class="sw sw-dashed"></span>Deadhead</label>
  <label><input type="checkbox" id="fa" checked>Active flights only</label>
</div>
<div id="controls">
  <span style="font-size:12px;color:#888">Timeline</span>
  <input type="range" id="scrubber" min="0" max="{horizon}" step="10" value="0">
  <span id="tdisp">Day 1  00:00</span>
  <span id="airborne"></span>
</div>
<div id="svg-wrap">
  <svg id="nsvg" viewBox="0 0 860 540" xmlns="http://www.w3.org/2000/svg"></svg>
  <div id="tip"></div>
</div>
<div id="legend">
  <span class="li"><span class="sw" style="background:#1D9E75"></span>Fully covered</span>
  <span class="li"><span class="sw" style="background:#EF9F27"></span>Partially covered</span>
  <span class="li"><span class="sw" style="background:#E24B4A"></span>Uncovered (zero crew)</span>
  <span class="li"><span class="sw sw-dashed"></span>Deadhead</span>
  <span class="li">
    <span style="width:13px;height:13px;border-radius:50%;background:#185FA5;
      display:inline-block;opacity:.88"></span>Total crew based here (top-left)</span>
  <span class="li">
    <span style="width:13px;height:13px;border-radius:50%;background:#BA7517;
      display:inline-block;opacity:.9"></span>Crew on ground now (top-right)</span>
  <span class="li">
    <span style="width:13px;height:13px;border-radius:50%;background:#1D9E75;
      display:inline-block;opacity:.88"></span>Available for work (bottom-right)</span>
  <span class="li">
    <span style="width:13px;height:13px;border-radius:50%;background:#BA7517;
      display:inline-block;opacity:.60"></span>Visiting (away from home base, bottom-left)</span>
</div>
<div id="stats"></div>

<script>
const RAW = {data_js};
const AP  = {ap_js};
const HORIZON = {horizon};
const NS = 'http://www.w3.org/2000/svg';

// ── helpers ──────────────────────────────────────────────────────────────────

function fmtMin(m) {{
  const d  = Math.floor(m / 1440) + 1;
  const hh = String(Math.floor((m % 1440) / 60)).padStart(2, '0');
  const mm = String(m % 60).padStart(2, '0');
  return 'Day ' + d + '  ' + hh + ':' + mm;
}}

function el(tag, attrs) {{
  const e = document.createElementNS(NS, tag);
  if (attrs) Object.entries(attrs).forEach(([k, v]) => e.setAttribute(k, v));
  return e;
}}

function arcPath(ax, ay, bx, by, bend) {{
  const mx = (ax+bx)/2, my = (ay+by)/2;
  const dx = bx-ax, dy = by-ay, len = Math.sqrt(dx*dx+dy*dy);
  const nx = -dy/len, ny = dx/len;
  const cx = mx + nx*bend, cy = my + ny*bend;
  return {{ cx, cy, d: 'M'+ax+','+ay+' Q'+cx+','+cy+' '+bx+','+by }};
}}

function ptOnQ(ax, ay, bx, by, cx, cy, t) {{
  return {{
    x:  (1-t)*(1-t)*ax + 2*(1-t)*t*cx + t*t*bx,
    y:  (1-t)*(1-t)*ay + 2*(1-t)*t*cy + t*t*by,
    dx: 2*(1-t)*(cx-ax) + 2*t*(bx-cx),
    dy: 2*(1-t)*(cy-ay) + 2*t*(by-cy),
  }};
}}

// ── pre-compute coverage ─────────────────────────────────────────────────────

const uncovMap = {{}};
RAW.uncovered_flights.forEach(u => uncovMap[u.flight_num] = u.missing_slots);

const coveredByRoute = new Set();
RAW.routes.forEach(r => r.legs.forEach(l => {{
  if (l.type === 'flight') coveredByRoute.add(l.flight_id);
}}));

function flightStatus(f) {{
  const inUnc   = uncovMap[f.flight_num] !== undefined;
  const inRoute = coveredByRoute.has(f.id);
  if (inUnc && inRoute)  return 'partial';
  if (inUnc && !inRoute) return 'uncovered';
  return 'covered';
}}

const flightCrewCount = {{}};
RAW.routes.forEach(r => r.legs.forEach(l => {{
  if (l.type === 'flight')
    flightCrewCount[l.flight_id] = (flightCrewCount[l.flight_id] || 0) + 1;
}}));

const crewBase = {{}};
RAW.crew.forEach(c => crewBase[c.id] = c.base);

// ── constants mirrored from crew_ddd.py ──────────────────────────────────────
const MIN_TURNAROUND  = 45;    // min gap between arriving and departing
const MIN_REST        = 480;   // 8 hr rest resets duty clock
const MAX_DUTY        = 840;   // 14 hr FAA duty limit

// ── per-crew state at time t ─────────────────────────────────────────────────
// Returns object per crew_id: {{ loc, lastArrival, dutyMins, nextDep, nextFlight }}
// loc=null means in the air.  crew with no route stay at base indefinitely.

function crewStateAt(t) {{
  const routeMap = {{}};
  RAW.routes.forEach(r => {{ routeMap[r.crew_id] = r; }});

  const states = {{}};
  RAW.crew.forEach(c => {{
    const route = routeMap[c.id];
    if (!route) {{
      // no route: sitting at base entire horizon
      states[c.id] = {{ loc: c.base, lastArrival: null, dutyMins: 0, nextDep: null, nextFlight: null }};
      return;
    }}

    const legs = route.legs.slice().sort((a, b) => a.dep - b.dep);
    let loc         = c.base;
    let lastArrival = null;   // time of most recent landing (flight or dh)
    let lastRestEnd = 0;      // time duty clock last reset
    let dutyMins    = 0;      // accumulated duty since last rest
    let nextDep     = null;   // next scheduled departure from current loc
    let nextFlight  = null;   // the leg object for next dep

    for (let i = 0; i < legs.length; i++) {{
      const l = legs[i];

      if (t < l.dep) {{
        // this leg hasn't started — check if it's the next one
        if (nextDep === null) {{ nextDep = l.dep; nextFlight = l; }}
        break;
      }}

      if (t >= l.dep && t < l.arr) {{
        // currently on this leg
        loc = null;
        lastArrival = null;  // in the air
        // duty so far in this leg
        const dutyBeforeLeg = dutyMins;
        if (l.type === 'flight') dutyMins = dutyBeforeLeg + (t - l.dep);
        break;
      }}

      // leg completed before t
      if (t >= l.arr) {{
        loc = l.to;
        lastArrival = l.arr;

        // compute gap to previous arrival for rest check
        const prevEnd = i === 0 ? 0 : legs[i-1].arr;
        const gap = l.dep - prevEnd;
        if (gap >= MIN_REST) {{
          // rest period before this leg — reset duty
          lastRestEnd = l.dep;
          dutyMins = 0;
        }}
        if (l.type === 'flight') dutyMins += l.arr - l.dep;
        // deadheads don't accumulate duty minutes (repositioning, not working)
      }}
    }}

    states[c.id] = {{ loc, lastArrival, dutyMins: Math.round(dutyMins), nextDep, nextFlight }};
  }});
  return states;
}}

// ── classify a single crew member on the ground ──────────────────────────────
// Returns one of: 'available' | 'turnaround' | 'rest' | 'duty_limit' | 'committed'
// plus a human-readable reason string.

function classifyCrew(state, t) {{
  const {{ lastArrival, dutyMins, nextDep }} = state;

  // Committed: next leg departs within turnaround window (already locked in)
  if (nextDep !== null && nextDep - t <= MIN_TURNAROUND) {{
    const mins = nextDep - t;
    return {{ status: 'committed', reason: 'Next leg in ' + mins + ' min' }};
  }}

  // In turnaround: just landed, not enough recovery time yet
  if (lastArrival !== null && (t - lastArrival) < MIN_TURNAROUND) {{
    const remaining = MIN_TURNAROUND - (t - lastArrival);
    return {{ status: 'turnaround', reason: 'Turnaround — ' + remaining + ' min left (need ' + MIN_TURNAROUND + ')' }};
  }}

  // In rest: landed, but rest period not yet complete (duty clock hasn't reset)
  if (lastArrival !== null) {{
    const restSoFar = t - lastArrival;
    if (restSoFar < MIN_REST && dutyMins > 0) {{
      const remaining = MIN_REST - restSoFar;
      const hh = Math.floor(remaining / 60), mm = remaining % 60;
      return {{ status: 'rest', reason: 'Mandatory rest — ' + hh + 'h ' + mm + 'm left (8 hr rule)' }};
    }}
  }}

  // Duty limit: accumulated too many duty minutes since last rest
  if (dutyMins >= MAX_DUTY) {{
    return {{ status: 'duty_limit', reason: 'Duty limit reached (' + dutyMins + '/' + MAX_DUTY + ' min)' }};
  }}

  // Approaching duty limit: can't take a typical flight
  if (dutyMins >= MAX_DUTY - 60) {{
    return {{ status: 'duty_limit', reason: 'Near duty limit (' + dutyMins + '/' + MAX_DUTY + ' min, <1 hr left)' }};
  }}

  return {{ status: 'available', reason: 'Available (' + (MAX_DUTY - dutyMins) + ' duty min remaining)' }};
}}

// ── aggregate availability per airport ───────────────────────────────────────

function crewAvailabilityAt(t) {{
  const allStates = crewStateAt(t);
  const byAirport = {{}};

  RAW.crew.forEach(c => {{
    const state = allStates[c.id];
    if (!state.loc) return;  // in the air

    const airport = state.loc;
    if (!byAirport[airport]) {{
      byAirport[airport] = {{
        available:0, turnaround:0, rest:0, duty_limit:0, committed:0,
        fromBase:0, visiting:0, details:[]
      }};
    }}
    const cls = classifyCrew(state, t);
    byAirport[airport][cls.status]++;
    // track whether this crew member is at their home base or away
    if (c.base === airport) byAirport[airport].fromBase++;
    else                    byAirport[airport].visiting++;
    byAirport[airport].details.push({{ id: c.id, base: c.base, atHome: c.base === airport, ...cls, dutyMins: state.dutyMins }});
  }});

  return byAirport;
}}

// ── crew ground counts (kept for arc rendering) ───────────────────────────────

function crewAtAirportsAt(t) {{
  const counts = {{}};
  Object.keys(AP).forEach(k => counts[k] = 0);
  RAW.routes.forEach(route => {{
    const base = crewBase[route.crew_id];
    const legs = route.legs.slice().sort((a, b) => a.dep - b.dep);
    let loc = base;
    for (let i = 0; i < legs.length; i++) {{
      const l = legs[i];
      if (t < l.dep) break;
      if (t >= l.dep && t < l.arr) {{ loc = null; break; }}
      if (t >= l.arr) loc = l.to;
    }}
    if (loc && counts[loc] !== undefined) counts[loc]++;
  }});
  // also count crew with no routes (sitting at base)
  const routedIds = new Set(RAW.routes.map(r => r.crew_id));
  RAW.crew.forEach(c => {{
    if (!routedIds.has(c.id) && counts[c.base] !== undefined) counts[c.base]++;
  }});
  return counts;
}}

// ── arc bend assignment ───────────────────────────────────────────────────────

function assignBends(arcs) {{
  const pairCount = {{}}, pairIdx = {{}};
  arcs.forEach(a => {{
    const k = [a.from, a.to].sort().join('-');
    pairCount[k] = (pairCount[k] || 0) + 1;
  }});
  return arcs.map(a => {{
    const k   = [a.from, a.to].sort().join('-');
    pairIdx[k] = (pairIdx[k] || 0);
    const idx  = pairIdx[k]++;
    const n    = pairCount[k];
    const spread = n === 1 ? 0 : (idx - (n - 1) / 2) * 28;
    const fwd  = a.from < a.to;
    const base = 54;
    const bend = n === 1 ? base : (base + Math.abs(spread)) * (fwd ? 1 : -1) * (spread < 0 ? -1 : 1);
    return {{ ...a, bend }};
  }});
}}

// ── SVG defs (arrow markers) ─────────────────────────────────────────────────

const svg = document.getElementById('nsvg');
const tip = document.getElementById('tip');

function makeDefs() {{
  const defs = el('defs');
  [['ac','#1D9E75'],['ap','#EF9F27'],['au','#E24B4A'],['adh','#7F77DD']].forEach(([id,col]) => {{
    const m = el('marker', {{ id, viewBox:'0 0 10 10', refX:'8', refY:'5',
                              markerWidth:'5', markerHeight:'5', orient:'auto-start-reverse' }});
    const p = el('path', {{ d:'M2,1 L8,5 L2,9', fill:'none', stroke:col,
                            'stroke-width':'1.5', 'stroke-linecap':'round', 'stroke-linejoin':'round' }});
    m.appendChild(p); defs.appendChild(m);
  }});
  svg.appendChild(defs);
}}

// ── tooltip helpers ───────────────────────────────────────────────────────────

const wrap = document.getElementById('svg-wrap');

function moveTip(e) {{
  const r = wrap.getBoundingClientRect();
  let x = e.clientX - r.left + 14;
  let y = e.clientY - r.top  - 80;
  if (x + 245 > r.width) x = e.clientX - r.left - 255;
  tip.style.left = Math.max(x, 4) + 'px';
  tip.style.top  = Math.max(y, 4) + 'px';
}}

function buildFlightTip(arc, isActive, t) {{
  const f  = arc.flight;
  const uf = RAW.uncovered_flights.find(u => u.flight_num === f.flight_num);
  const prog = isActive ? Math.round((t - f.dep_min) / (f.arr_min - f.dep_min) * 100) : null;
  const assigned = flightCrewCount[f.id] || 0;
  const colMap = {{ covered:'#1D9E75', partial:'#EF9F27', uncovered:'#E24B4A' }};
  const lblMap = {{ covered:'Fully covered', partial:'Partially covered', uncovered:'Uncovered' }};
  const col    = colMap[arc.status];
  const lbl    = lblMap[arc.status];
  return '<b>FL' + f.flight_num + ' <span style="color:' + col + '">' + lbl + '</span></b>'
    + '<s2>' + f.origin + ' → ' + f.dest + '</s2>'
    + '<s2>Dep: ' + fmtMin(f.dep_min) + ' · Arr: ' + fmtMin(f.arr_min) + '</s2>'
    + '<s2>Duration: ' + f.duration + ' min</s2>'
    + '<s2>Min crew: ' + f.min_crew + ' · Assigned: ' + assigned
    + (uf ? ' · Missing: ' + uf.missing_slots : '') + '</s2>'
    + (prog !== null ? '<s2 style="color:' + col + '">Airborne — ' + prog + '% complete</s2>' : '');
}}

function buildDhTip(arc, isActive, t) {{
  const l    = arc.leg;
  const prog = isActive ? Math.round((t - l.dep) / (l.arr - l.dep) * 100) : null;
  return '<b>Deadhead leg</b>'
    + '<s2>' + l.from + ' → ' + l.to + '</s2>'
    + '<s2>Dep: ' + fmtMin(l.dep) + ' · Arr: ' + fmtMin(l.arr) + '</s2>'
    + '<s2>Crew: ' + arc.crewIds.join(', ') + '</s2>'
    + (prog !== null ? '<s2 style="color:#7F77DD">En route — ' + prog + '% complete</s2>' : '');
}}

function buildApTip(code, apData, total, t) {{
  const cnt      = apData ? (apData.available + apData.turnaround + apData.rest + apData.duty_limit + apData.committed) : 0;
  const fromBase = apData ? apData.fromBase   : 0;
  const visiting = apData ? apData.visiting   : 0;
  const availCnt = apData ? apData.available  : 0;
  const onBreak  = cnt - availCnt;
  const dep = RAW.flights.filter(f => f.origin === code && t >= f.dep_min && t < f.arr_min).length;
  const arr = RAW.flights.filter(f => f.dest   === code && t >= f.dep_min && t < f.arr_min).length;
  const nxt = RAW.flights.filter(f => f.origin === code && f.dep_min > t)
                          .sort((a, b) => a.dep_min - b.dep_min)[0];

  // build breakdown rows for each crew on ground
  let detailRows = '';
  if (apData && apData.details.length) {{
    const statusIcon = {{ available:'✓', turnaround:'↻', rest:'💤', duty_limit:'⛔', committed:'→' }};
    const statusCol  = {{ available:'#1D9E75', turnaround:'#EF9F27', rest:'#7F77DD', duty_limit:'#E24B4A', committed:'#185FA5' }};
    apData.details.forEach(d => {{
      const homeTag = d.atHome ? '' : ' <span style="color:#BA7517">[away]</span>';
      detailRows += '<s2 style="color:' + statusCol[d.status] + '">'
        + statusIcon[d.status] + ' Crew&nbsp;' + d.id + homeTag + ' — ' + d.reason + '</s2>';
    }});
  }}

  return '<b>' + code + '</b>'
    + '<s2>Based here (total): ' + total + '</s2>'
    + '<s2>On ground now: ' + cnt
      + ' <span style="color:#185FA5">(' + fromBase + ' home)</span>'
      + (visiting ? ' <span style="color:#BA7517">(+' + visiting + ' visiting)</span>' : '')
      + '</s2>'
    + '<s2>Away / airborne: ' + (total - fromBase) + '</s2>'
    + '<s2 style="color:#1D9E75">✓ Available now: ' + availCnt + '</s2>'
    + (onBreak ? '<s2 style="color:#EF9F27">⏳ On break / unavailable: ' + onBreak + '</s2>' : '')
    + '<s2>Departing now: '  + dep + '</s2>'
    + '<s2>Arriving now: '   + arr + '</s2>'
    + (nxt ? '<s2>Next dep: FL' + nxt.flight_num + ' at ' + fmtMin(nxt.dep_min) + '</s2>' : '')
    + (detailRows ? '<s2 style="margin-top:4px;display:block;border-top:0.5px solid #ddd;padding-top:4px">Ground crew status:</s2>' + detailRows : '');
}}

// ── main render ───────────────────────────────────────────────────────────────

function render() {{
  while (svg.children.length > 1) svg.removeChild(svg.lastChild);

  const t          = +document.getElementById('scrubber').value;
  const showC      = document.getElementById('fc').checked;
  const showP      = document.getElementById('fp').checked;
  const showU      = document.getElementById('fu').checked;
  const showDH     = document.getElementById('fd').checked;
  const activeOnly = document.getElementById('fa').checked;

  // use the richer availability data (superset of crewAtAirportsAt)
  const avail  = crewAvailabilityAt(t);
  const crewNow = {{}};
  Object.keys(AP).forEach(k => crewNow[k] = 0);
  Object.entries(avail).forEach(([ap, d]) => {{
    if (crewNow[ap] !== undefined)
      crewNow[ap] = d.available + d.turnaround + d.rest + d.duty_limit + d.committed;
  }});

  // build arc list
  const allArcs = [];
  RAW.flights.forEach(f => {{
    allArcs.push({{ kind:'flight', status:flightStatus(f), from:f.origin, to:f.dest, flight:f }});
  }});

  const dhMap = {{}};
  RAW.routes.forEach(r => r.legs.filter(l => l.type === 'deadhead').forEach(l => {{
    const k = l.from + '-' + l.to + '-' + l.flight_id;
    if (!dhMap[k]) {{ dhMap[k] = {{ kind:'deadhead', from:l.from, to:l.to, leg:l, crewIds:[] }}; allArcs.push(dhMap[k]); }}
    if (!dhMap[k].crewIds.includes(r.crew_id)) dhMap[k].crewIds.push(r.crew_id);
  }}));

  const bent = assignBends(allArcs);
  const arcLayer = el('g');

  bent.forEach(arc => {{
    if (arc.kind === 'flight') {{
      if (arc.status === 'covered'   && !showC)  return;
      if (arc.status === 'partial'   && !showP)  return;
      if (arc.status === 'uncovered' && !showU)  return;
    }} else {{
      if (!showDH) return;
    }}

    const isActive = arc.kind === 'deadhead'
      ? (t >= arc.leg.dep && t < arc.leg.arr)
      : (t >= arc.flight.dep_min && t < arc.flight.arr_min);
    if (activeOnly && !isActive) return;

    const A = AP[arc.from], B = AP[arc.to];
    if (!A || !B) return;

    const colMap  = {{ covered:'#1D9E75', partial:'#EF9F27', uncovered:'#E24B4A' }};
    const markMap = {{ covered:'ac',       partial:'ap',       uncovered:'au' }};
    const col     = arc.kind === 'deadhead' ? '#7F77DD' : colMap[arc.status];
    const markId  = arc.kind === 'deadhead' ? 'adh'     : markMap[arc.status];
    const dashArr = arc.kind === 'deadhead' ? '5 4'     : '';

    const {{ cx, cy, d }} = arcPath(A.x, A.y, B.x, B.y, arc.bend || 54);
    const sw = isActive ? 2.5 : 1.3;
    const op = isActive ? 0.95 : 0.22;

    const g = el('g'); g.style.cursor = 'pointer';

    const path = el('path', {{ d, fill:'none', stroke:col,
      'stroke-width':sw, 'stroke-opacity':op, 'marker-end':'url(#'+markId+')' }});
    if (dashArr) path.setAttribute('stroke-dasharray', dashArr);
    g.appendChild(path);

    const mid = ptOnQ(A.x, A.y, B.x, B.y, cx, cy, 0.5);
    const ang = Math.atan2(mid.dy, mid.dx) * 180 / Math.PI;
    g.appendChild(el('polygon', {{ points:'-6,-4 5,0 -6,4', fill:col, opacity: isActive?0.9:0.2,
      transform:'translate('+mid.x+','+mid.y+') rotate('+ang+')' }}));

    const lp  = ptOnQ(A.x, A.y, B.x, B.y, cx, cy, 0.35);
    const lbl = el('text', {{ x:lp.x, y:lp.y-8, 'text-anchor':'middle', 'font-size':'11',
      fill:col, opacity: isActive?0.95:0.28, 'pointer-events':'none',
      'font-weight': isActive?'500':'400' }});
    lbl.textContent = arc.kind === 'deadhead' ? 'DH' : 'FL' + arc.flight.flight_num;
    g.appendChild(lbl);

    const tipHtml = arc.kind === 'deadhead' ? buildDhTip(arc, isActive, t)
                                            : buildFlightTip(arc, isActive, t);
    g.addEventListener('mouseenter', e => {{ tip.innerHTML = tipHtml; tip.classList.add('on'); moveTip(e); }});
    g.addEventListener('mousemove',  moveTip);
    g.addEventListener('mouseleave', () => tip.classList.remove('on'));
    arcLayer.appendChild(g);
  }});
  svg.appendChild(arcLayer);

  // pre-compute total crew assigned to each base (static, from RAW.crew)
  const baseTotal = {{}};
  RAW.crew.forEach(c => {{ baseTotal[c.base] = (baseTotal[c.base] || 0) + 1; }});

  // airport nodes
  const nodeLayer = el('g');
  Object.entries(AP).forEach(([code, pos]) => {{
    const apData   = avail[code] || null;
    const cnt      = crewNow[code] || 0;
    const total    = baseTotal[code] || 0;
    const fromBase = apData ? apData.fromBase  : 0;
    const visiting = apData ? apData.visiting  : 0;
    const availCnt = apData ? apData.available : 0;
    const onBreak  = cnt - availCnt;
    const g        = el('g'); g.style.cursor = 'pointer';

    g.appendChild(el('circle', {{ cx:pos.x, cy:pos.y, r:28, fill:'#185FA5',
      'fill-opacity':'0.12', stroke:'#185FA5', 'stroke-width':'1.5', 'stroke-opacity':'0.55' }}));

    const lt = el('text', {{ x:pos.x, y:pos.y+1, 'text-anchor':'middle',
      'dominant-baseline':'central', 'font-size':'13', 'font-weight':'500', fill:'#185FA5' }});
    lt.textContent = code; g.appendChild(lt);

    // amber badge top-right: crew currently on ground
    const bx = pos.x + 19, by = pos.y - 19;
    g.appendChild(el('circle', {{ cx:bx, cy:by, r:12, fill:'#BA7517', 'fill-opacity':'0.92' }}));
    const bt = el('text', {{ x:bx, y:by+1, 'text-anchor':'middle', 'dominant-baseline':'central',
      'font-size':'11', 'font-weight':'500', fill:'#fff' }});
    bt.textContent = cnt; g.appendChild(bt);

    // blue badge top-left: total crew based here (static)
    const tx = pos.x - 19, ty = pos.y - 19;
    g.appendChild(el('circle', {{ cx:tx, cy:ty, r:12, fill:'#185FA5', 'fill-opacity':'0.88' }}));
    const tt = el('text', {{ x:tx, y:ty+1, 'text-anchor':'middle', 'dominant-baseline':'central',
      'font-size':'11', 'font-weight':'500', fill:'#fff' }});
    tt.textContent = total; g.appendChild(tt);

    // green badge bottom-right: available crew count
    if (cnt > 0) {{
      const ax2 = pos.x + 19, ay2 = pos.y + 19;
      const availCol = availCnt > 0 ? '#1D9E75' : '#aaa';
      g.appendChild(el('circle', {{ cx:ax2, cy:ay2, r:12, fill:availCol, 'fill-opacity':'0.88' }}));
      const at2 = el('text', {{ x:ax2, y:ay2+1, 'text-anchor':'middle', 'dominant-baseline':'central',
        'font-size':'11', 'font-weight':'500', fill:'#fff' }});
      at2.textContent = availCnt; g.appendChild(at2);
    }}

    // visiting badge bottom-left: visiting (away-from-home) crew
    if (visiting > 0) {{
      const vx = pos.x - 19, vy = pos.y + 19;
      g.appendChild(el('circle', {{ cx:vx, cy:vy, r:12, fill:'#BA7517', 'fill-opacity':'0.60' }}));
      const vt = el('text', {{ x:vx, y:vy+1, 'text-anchor':'middle', 'dominant-baseline':'central',
        'font-size':'11', 'font-weight':'500', fill:'#fff' }});
      vt.textContent = '+' + visiting; g.appendChild(vt);
    }}

    // labels below node
    const cl1 = el('text', {{ x:pos.x, y:pos.y+34, 'text-anchor':'middle',
      'dominant-baseline':'central', 'font-size':'10', fill:'#555' }});
    cl1.textContent = cnt + ' ground / ' + total + ' based'; g.appendChild(cl1);

    if (cnt > 0) {{
      const cl2 = el('text', {{ x:pos.x, y:pos.y+45, 'text-anchor':'middle',
        'dominant-baseline':'central', 'font-size':'9.5', fill:'#777' }});
      const homePart    = fromBase + ' home' + (visiting ? ' · ' + visiting + ' visit' : '');
      const breakPart   = onBreak  ? ' · ' + onBreak + ' on break' : '';
      cl2.textContent = homePart + breakPart; g.appendChild(cl2);
    }}

    g.addEventListener('mouseenter', e => {{ tip.innerHTML = buildApTip(code, apData, total, t); tip.classList.add('on'); moveTip(e); }});
    g.addEventListener('mousemove',  moveTip);
    g.addEventListener('mouseleave', () => tip.classList.remove('on'));
    nodeLayer.appendChild(g);
  }});
  svg.appendChild(nodeLayer);

  // status bar
  document.getElementById('tdisp').textContent = fmtMin(t);
  const nb = RAW.flights.filter(f => t >= f.dep_min && t < f.arr_min).length;
  document.getElementById('airborne').textContent = nb + ' flight' + (nb !== 1 ? 's' : '') + ' airborne';

  const total = Object.values(crewNow).reduce((a, b) => a + b, 0);
  const uncovActive = RAW.uncovered_flights.filter(u => t >= u.dep_min && t < u.arr_min).length;

  // aggregate global from-base / visiting / available across all airports
  let globalFromBase = 0, globalVisiting = 0, globalAvail = 0, globalOnBreak = 0;
  Object.values(avail).forEach(d => {{
    globalFromBase += d.fromBase;
    globalVisiting += d.visiting;
    globalAvail    += d.available;
    globalOnBreak  += d.turnaround + d.rest + d.duty_limit + d.committed;
  }});

  document.getElementById('stats').innerHTML =
    '<div class="pill">Ground crew: <strong>' + total + '</strong></div>' +
    '<div class="pill">Airborne: <strong>' + nb + '</strong></div>' +
    '<div class="pill" title="Crew on ground who are at their home base">At home base: <strong style="color:#185FA5">' + globalFromBase + '</strong></div>' +
    '<div class="pill" title="Crew on ground away from their home base">Visiting: <strong style="color:#BA7517">' + globalVisiting + '</strong></div>' +
    '<div class="pill" title="Crew on ground who have finished their break and can work">Available now: <strong style="color:#1D9E75">' + globalAvail + '</strong></div>' +
    (globalOnBreak ? '<div class="pill" title="Crew on ground still on mandatory rest, turnaround, or duty limit">On break: <strong style="color:#EF9F27">' + globalOnBreak + '</strong></div>' : '') +
    '<div class="pill">Uncovered active: <strong style="color:#E24B4A">' + uncovActive + '</strong></div>' +
    '<div class="pill">Missing slots total: <strong style="color:#EF9F27">' + RAW.meta.uncovered_slots + '</strong></div>' +
    '<div class="pill">Solve: <strong>' + RAW.meta.solve_status + '</strong></div>';
}}

makeDefs();
render();
document.getElementById('scrubber').addEventListener('input', render);
['fc','fp','fu','fd','fa'].forEach(id =>
  document.getElementById(id).addEventListener('change', render));
</script>
</body>
</html>
"""
    return html


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    input_path = sys.argv[1]
    if not os.path.exists(input_path):
        print(f"Error: file not found — {input_path}")
        sys.exit(1)

    with open(input_path, encoding="utf-8") as f:
        data = json.load(f)

    # default output name next to the input file
    if len(sys.argv) >= 3:
        output_path = sys.argv[2]
    else:
        base = os.path.splitext(os.path.basename(input_path))[0]
        output_path = os.path.join(os.path.dirname(input_path) or ".", base + "_viz.html")

    html = build_html(data)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Saved → {output_path}")


if __name__ == "__main__":
    main()
