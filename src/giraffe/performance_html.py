"""Bounded, script-free presentation of the shared performance contract.

This module formats already-derived values and decimates only SVG drawing data.
It never computes a metric from request records or pools targets/windows.
"""
from __future__ import annotations

from html import escape
import math
import re

from .performance import KPI_IDS


STYLE = """
:root{--bg:#09090a;--surface:#131315;--text:#f5f3ee;--text-2:#b5b2aa;--accent:#f5b544;--line:#333136;--mono:ui-monospace,monospace;color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui}main{max-width:1240px;margin:auto;padding:24px}h1{font-size:28px}h2{font-size:20px}h3{font-size:15px}a{color:var(--accent)}.muted,.html-note{color:var(--text-2)}.card,details,.html-kpi,.html-plot{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:14px;margin:12px 0}summary{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:11px/1.5 var(--mono)}code{overflow-wrap:anywhere}.badge{padding:2px 8px;border-radius:4px;background:#333136}.pass{color:#3dd68c}.fail,.blocked{color:#ff5f67}.inconclusive{color:#f5b544}.table{overflow:auto}table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:8px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line)}td:nth-child(2),.html-distributions td{font-family:var(--mono)}

.html-section{margin:32px 0;scroll-margin-top:90px}.html-section>header{display:flex;align-items:baseline;justify-content:space-between;gap:18px}.html-section h2{margin:0 0 16px}.html-section header p{font-size:12px;text-align:right}.html-cards{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px}.html-kpi{margin:0;min-width:0}.html-kpi h3{font-size:12px;margin:0 0 14px;display:flex;justify-content:space-between;gap:6px}.html-value{font:24px/1.25 var(--mono);overflow-wrap:anywhere}.html-value small,.html-kpi p,.html-note,.html-legend{font:11px/1.5 system-ui;color:var(--text-2)}.html-kpi p{margin:10px 0 0}.html-estimate{font:9px var(--mono);color:var(--accent)}.html-info{position:relative;margin:0;padding:0;border:0;display:inline-block}.html-info summary{font-size:11px;list-style:none}.html-info>span{position:absolute;right:0;z-index:3;width:240px;background:var(--surface);border:1px solid var(--line);padding:10px;font-weight:400}.html-two{display:grid;grid-template-columns:1fr 1fr;gap:14px}.html-throughput{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr);gap:18px;align-items:start}.html-capacity{grid-template-columns:repeat(2,minmax(0,1fr))}.html-load{grid-template-columns:repeat(3,minmax(0,1fr))}.html-support{grid-template-columns:1fr 1fr;margin:14px 0}.html-plot{min-width:0;margin:0}.html-plot svg{display:block;width:100%;height:auto}.html-legend{display:flex;flex-wrap:wrap;gap:8px 16px}.html-legend i{display:inline-block;width:14px;border-top:2px solid;margin-right:5px}.html-metrics details{margin:0;padding:0;border:0}.html-scope{padding:12px 0;border-bottom:1px solid var(--line)}
a:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px}nav{margin:24px 0}.answer-finding summary{overflow-wrap:anywhere}

.baseline-comparison{width:100%;min-width:0;overflow:visible}.baseline-table{width:100%;min-width:0;table-layout:fixed}.baseline-table .comparison-name-col{width:34%}.baseline-table .comparison-value-col{width:17%}.baseline-table .comparison-delta-col{width:12%}.baseline-table .comparison-verdict-col{width:20%}
.baseline-table th,.baseline-table td{min-width:0;padding:10px 8px;text-transform:none;white-space:normal;overflow-wrap:anywhere}.baseline-table .comparison-number{text-align:right;white-space:nowrap;font-family:var(--mono);font-variant-numeric:tabular-nums;overflow:hidden;text-overflow:ellipsis}.baseline-table .comparison-group th{padding-top:18px;font-size:13px}.baseline-table .comparison-reason{display:block;margin-top:6px;font-weight:400;color:var(--text-2);text-transform:none;white-space:normal}.baseline-table .comparison-samples{display:block;font-size:11px;color:var(--text-2)}
@media(max-width:599px){.baseline-table,.baseline-table tbody{display:block;width:100%}.baseline-table colgroup{display:none}.baseline-table thead{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}.baseline-table .comparison-group,.baseline-table .comparison-group th{display:block;width:100%;border:0}.baseline-table .comparison-metric{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);grid-template-areas:"metric metric" "baseline current" "change verdict";gap:8px;margin:8px 0;padding:12px;border:1px solid var(--line);border-radius:6px}.baseline-table .comparison-metric th,.baseline-table .comparison-metric td{display:block;padding:0;border:0}.baseline-table .comparison-metric th{grid-area:metric}.baseline-table [data-label="Baseline"]{grid-area:baseline}.baseline-table [data-label="Current"]{grid-area:current}.baseline-table [data-label="Δ"]{grid-area:change}.baseline-table [data-label="Verdict"]{grid-area:verdict}.baseline-table .comparison-metric .comparison-number{text-align:left}.baseline-table [data-label="Baseline"]::before{content:"Baseline ";color:var(--text-2);font:11px system-ui}.baseline-table [data-label="Current"]::before{content:"→ Current ";color:var(--text-2);font:11px system-ui}.baseline-table [data-label="Δ"]::before{content:"Δ ";color:var(--text-2)}}
@media(prefers-color-scheme:light){:root{--bg:#fff;--surface:#f7f7f8;--text:#18181b;--text-2:#52525b;--accent:#805500;--line:#d4d4d8;color-scheme:light}.badge{background:#ededf0}.pass{color:#087743}.fail,.blocked{color:#bf202a}.inconclusive{color:#805500}}
@media print{:root{--bg:#fff;--surface:#fff;--text:#000;--text-2:#333;--accent:#222;--line:#bbb;color-scheme:light}main{max-width:none;padding:0}nav,a[download]{display:none}section{break-inside:auto}h1,h2,h3{break-after:avoid}tr,.html-plot,.html-kpi{break-inside:avoid}.html-cards{grid-template-columns:repeat(3,minmax(0,1fr))}details{border:0;padding:4px 0}details>*:not(summary){display:block!important}details::details-content{content-visibility:visible!important;height:auto!important}a{color:inherit}.pass,.fail,.blocked,.inconclusive{color:#000}.table{overflow:visible}table{font-size:10px}th,td{padding:5px}body{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
@media(max-width:800px){main{padding:14px}.html-throughput{grid-template-columns:1fr}.html-two{grid-template-columns:1fr}.html-section>header{display:block}.html-section header p{text-align:left}.html-cards{grid-template-columns:repeat(2,minmax(0,1fr))}}
"""

CHART_STYLE = """.html-chart-frame{position:relative;margin:16px 0}.html-chart-frame svg{height:240px;width:100%}.html-axis-label,.html-stage-label{position:absolute;color:var(--text-2);font:11px/1.3 var(--mono);pointer-events:none}.html-axis-label.x-min{left:5.33%;bottom:8%}.html-axis-label.x-max{right:4%;bottom:8%}.html-axis-label.y-max{left:0;top:10%}.html-axis-label.y-min{left:0;bottom:20%}.html-axis-label.x-title{left:5.33%;right:4%;bottom:0;text-align:center}.html-axis-label.y-title{left:5.33%;top:0}.html-stage-label{top:8%;max-width:18%;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}.html-transition{stroke:var(--surface);stroke-width:1;vector-effect:non-scaling-stroke}@media(max-width:640px){.html-chart-frame svg{height:220px}}"""
COLORS = ("#b5b2aa", "#9aa4ff", "#f5b544", "#3dd68c", "#63c8d2")
COMMON_GUIDANCE = {"No eligible observations in this scope.", "Collect eligible observations for this scope."}

STATES = {"not_measured": "Not measured", "not_observed": "Not observed", "not_collected": "Not collected",
          "not_applicable": "Not applicable", "insufficient_evidence": "Insufficient evidence",
          "not_supported": "Not supported", "no_baseline": "No baseline"}


def _e(value):
    return escape(str(value if value is not None else ""), quote=True)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _fmt(value):
    if not _number(value):
        return "—" if value is None else _e(value)
    if value == int(value):
        return f"{int(value):,}"
    magnitude = abs(value)
    precision = 2 if magnitude >= 1 else max(2, 1 - math.floor(math.log10(magnitude)))
    if precision > 8:
        return f"{value:.2g}"
    return f"{value:,.{precision}f}".rstrip("0").rstrip(".")


def _evidence_label(key):
    return {"first_output_ms": "First useful output limit", "first_token_ms": "Client TTFT limit",
            "elapsed_ms": "Elapsed time", "local_drops": "Local drops", "dropped_local": "Local drops",
            "late_drops": "Late", "dropped_late": "Late", "not_offered": "Not offered",
            "timing_violations": "Timing violations", "missing_timing": "Missing timing"}.get(
                key, str(key).replace("_", " ").capitalize())


def _structured_value(row):
    """Readable bounded evidence, without deriving new performance decisions."""
    rows = []
    truncated = False

    def text(value, key=""):
        if key == "acceptance_ratio" and _number(value):
            return _fmt(value * 100) + "%"
        nonlocal truncated
        result = ("Unknown" if value is None else ("Yes" if value else "No") if isinstance(value, bool)
                  else _fmt(value) + (" ms" if key.endswith("_ms") else "") if _number(value)
                  else _evidence_label(value) if value in ("first_output_ms", "first_token_ms")
                  else str(value).replace("_", " ").capitalize() if re.fullmatch(r"[a-z]+(?:_[a-z0-9]+)+", str(value))
                  else str(value))
        if len(result) > 240:
            result = result[:240] + "…"
            truncated = True
        return result

    def visit(value, path="", key="", depth=0):
        nonlocal truncated
        if len(rows) >= 40:
            truncated = True
            return
        if depth > 6:
            rows.append((path, "Nested values (preview depth limit)"))
            truncated = True
        elif isinstance(value, list):
            if all(not isinstance(item, (dict, list)) for item in value):
                rows.append((path or "Values", ", ".join(text(item) for item in value[:12]) if value else "None"))
                truncated |= len(value) > 12
            else:
                for index, item in enumerate(value[:41]):
                    visit(item, f"{path} · {index+1}" if path else f"Observation {index+1}", depth=depth+1)
                truncated |= len(value) > 41
        elif isinstance(value, dict):
            if not value:
                rows.append((path or "Values", "None"))
            for index, (field, item) in enumerate(value.items()):
                if index > 40:
                    truncated = True
                    break
                label = _evidence_label(field)
                visit(item, f"{path} · {label}" if path else label, field, depth+1)
        else:
            rows.append((path or "Value", text(value, key)))

    visit(row["value"])
    summary = " · ".join(f"{key}: {value}" for key, value in rows[:3])
    if row.get("id") == "drops":
        values = row["value"] if isinstance(row["value"], list) else [row["value"]]
        summaries = []
        for index, item in enumerate(values[:3]):
            counts = [("Local drops", item.get("local_drops") if item.get("local_drops") is not None else item.get("dropped_local")),
                      ("Late", item.get("late_drops") if item.get("late_drops") is not None else item.get("dropped_late")),
                      ("Not offered", item.get("not_offered"))]
            summaries.append((f"Observation {index+1}: " if len(values) > 1 else "")
                             + " · ".join(f"{label} {text(value)}" for label, value in counts))
        summary = "; ".join(summaries) + (f" · {len(values)} entries" if len(values) > 3 else "")
    empty_fields = [key for key, value in rows if value == "None"]
    rows = [(key, value) for key, value in rows if value != "None"]
    if empty_fields:
        rows.append(("Fields with no entries", "; ".join(empty_fields)))
    evidence = ''.join('<tr><th>' + _e(key[:160]) + '<td>' + _e(value) for key, value in rows)
    return (_e(summary)
            + '<details><summary>Underlying values</summary><table><tr><th>Recorded field<th>Value'
            + evidence + '</table>'
            + ('<p>Preview limited to 40 fields, 12 items per list and 240 characters per value. Complete values are included in the JSON export.</p>' if truncated else '')
            + '</details>')


def _value(row):
    value = row.get("value")
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        return _structured_value(row)
    unit = row.get("unit", "")
    if row.get("id", "").startswith("spec_decode_") and unit in {"ratio", "fraction"} and _number(value):
        value, unit = value * 100, "%"
    return _fmt(value) + (f' <small>{_e(unit)}</small>' if unit else '')


def _availability(row):
    if row.get("value") is not None:
        return ""
    state = STATES.get(row.get("state"), str(row.get("state", "Not measured")).replace("_", " ").capitalize())
    return f'<strong>{_e(state)}</strong>. ' + _e(" ".join(str(row.get(k) or "") for k in ("reason", "action") if row.get(k) not in COMMON_GUIDANCE).strip())


# Older performance snapshots keep their original labels; display current names.
METRIC_LABELS = {"Per-turn latency / first output": "Per-turn latency / time to first answer"}


def _metric_label(row):
    label = row.get("label", "")
    return METRIC_LABELS.get(label, label)


def _card(row):
    info = " · ".join(str(v) for v in (row.get("source"), (row.get("action") if row.get("action") not in COMMON_GUIDANCE else None)) if v)
    note = (f'Median · P95 {_fmt(row["p95"])} {_e(row.get("unit", ""))}'
            if row.get("distribution") and row.get("p95") is not None
            else f'{row.get("sample_count", 0)} / {row.get("eligible_count", 0)} eligible {_e(row.get("sample_unit", "samples"))}')
    note = _e(row['display_note']) if row.get('display_note') else note
    estimate = ' <span class="html-estimate">ESTIMATE</span>' if row.get("state") == "estimated" else ''
    missing = ('<strong>' + _e(STATES.get(row.get("state"), "Not measured")) + '</strong>. '
               + _e(row.get("reason", "") if row.get("reason") not in COMMON_GUIDANCE else "")) if row.get("value") is None else ''
    return ('<article class="html-kpi"><h3>' + _e(_metric_label(row))
            + '<details class="html-info"><summary aria-label="Definition">i</summary><span>' + _e(info)
            + '</span></details></h3><div class="html-value">' + _value(row) + '</div>' + estimate
            + '<p>' + (missing + (' ' + note if row.get("display_note") else '') if missing else note)
            + (' · Partial usage coverage' if row.get("partial") else '') + '</p></article>')


def _cards(rows, kind=""):
    return f'<div class="html-cards {_e(kind)}">' + ''.join(_card(row) for row in rows) + '</div>'


def _table(rows):
    # Adjacent identical definitions share a cell rather than repeating verbose
    # missing-collector guidance for every server metric.
    notes = [(_availability(row) or _e(row.get("source", "")))
             + (' · ' + _e(row["display_note"]) if row.get("display_note") else '')
             + (' · ESTIMATE' if row.get("state") == "estimated" else '')
             + (' · Partial coverage' if row.get("partial") else '')
             + (' · Server-wide; separate telemetry collection interval' if row.get("scope") == "server-wide" else '')
             for row in rows]
    parts = ['<div class="table"><table class="html-metrics"><tr><th>Metric<th>Value<th>Definition / availability']
    remaining = 0
    for index, row in enumerate(rows):
        parts.append('<tr><th>' + _e(_metric_label(row)) + '<td>' + _value(row))
        if not remaining:
            count = 1
            while index + count < len(rows) and notes[index + count] == notes[index]:
                count += 1
            remaining = count
            parts.append(('<td rowspan="' + str(count) + '">' if count > 1 else '<td>') + notes[index])
        remaining -= 1
    parts.append('</table></div>')
    return ''.join(parts)


def _distributions(rows, convention):
    parts = ['<div class="table"><table class="html-distributions"><tr><th>Metric / unit<th>n<th>Mean<th>Min<th>Median<th>P90<th>P95<th>P99<th>Max']
    for row in rows:
        parts.append('<tr><th scope="row">' + _e(_metric_label(row)) + ' · ' + _e(row.get('unit', '')))
        if row.get('value') is None:
            parts.append('<td>' + str(row.get('sample_count', 0)) + '<td colspan="7">' + _e(STATES.get(row.get('state'), 'Not measured')))
        else:
            parts.extend('<td>' + _fmt(row['value'].get(key)) for key in ('n', 'mean', 'min', 'median', 'p90', 'p95', 'p99', 'max'))
    parts.append('</table></div><p class="html-note">' + _e(convention) + '</p>')
    return ''.join(parts)


def decimate(points, buckets=120):
    """Min/max in each x-coordinate bucket, plus endpoints, in original order.

    Drawing retains extrema and endpoints in <= 2*buckets+2 points. Metrics and
    their populations remain unchanged; no synthetic interpolated samples appear.
    """
    if len(points) <= 2 * buckets + 2:
        return points
    left, right = points[0][0], points[-1][0]
    span = right - left
    groups = {}
    for index, (x, y) in enumerate(points):
        bucket = min(buckets - 1, int((x - left) / span * buckets)) if span else 0
        previous = groups.setdefault(bucket, [index, index])
        if y < points[previous[0]][1]:
            previous[0] = index
        if y > points[previous[1]][1]:
            previous[1] = index
    indices = sorted({0, len(points)-1, *(index for pair in groups.values() for index in pair)})
    return [points[index] for index in indices]


def _chart_frame(svg, xmin, xmax, ymin, ymax, xlabel, ylabel, stage_labels=''):
    # Text is HTML in CSS pixels, outside the scaled SVG coordinate system.
    # The portable report needs neither a script nor a fixed minimum width.
    return ('<div class="html-chart-frame">' + svg + stage_labels
            + ''.join('<span class="html-axis-label ' + kind + '">' + _e(label) + '</span>'
                      for kind, label in (('x-min', _fmt(xmin)), ('x-max', _fmt(xmax)),
                                          ('y-min', _fmt(ymin)), ('y-max', _fmt(ymax)),
                                          ('x-title', xlabel), ('y-title', ylabel))) + '</div>')


def _nice_max(start, end, *, integer=False):
    """Drawing domain only: nearest 1/2/2.5/5 decade step, rounded outward."""
    raw = (end - start or abs(end) or 1) / 4
    power = 10 ** math.floor(math.log10(raw))
    choices = ([1] if integer and raw < 1 else
               [value * power for value in (1, 2, 2.5, 5, 10)
                if not integer or float(value * power).is_integer()])
    step = min(choices, key=lambda value: abs(value - raw))
    return float(f'{math.ceil(end / step - 1e-10) * step:.12g}')


def _chart(title, series, *, xlabel="Request number · dispatch order", ylabel="seconds", step=False, bands=()):
    series = [(name, [(x, y) for x, y in points if _number(x) and _number(y)]) for name, points in series]
    series = [(name, points) for name, points in series if len(points) > 1]
    if not series:
        return ''
    all_points = [point for _, points in series for point in points]
    xmin, xmax = min(p[0] for p in all_points), max(p[0] for p in all_points)
    ymin, ymax = min(0, min(p[1] for p in all_points)), max(p[1] for p in all_points)
    if xmax <= xmin:
        return ''
    ymax = ymax if ymax > ymin else ymin + 1
    if not xlabel.startswith('Request number'):
        xmax = _nice_max(xmin, xmax)
    ymax = _nice_max(ymin, ymax, integer='tokens' in ylabel.lower() or 'requests' in ylabel.lower())
    def x(value):
        return 48 + (value - xmin) / (xmax - xmin) * 816

    def y(value):
        return 220 - (value - ymin) / (ymax - ymin) * 192
    desc = f'{title}. {xlabel}: {_fmt(xmin)} to {_fmt(xmax)}. {ylabel}: {_fmt(ymin)} to {_fmt(ymax)}. '
    desc += '; '.join(f'{name}: {len(points)} observations' for name, points in series)
    body, stage_labels = [], []
    for band in bands[:40]:
        left, right = max(xmin, band.get('elapsed_s', xmin)), min(xmax, band.get('end_elapsed_s', xmax))
        if right <= left:
            continue
        body.append(f'<rect x="{x(left):.2f}" y="22" width="{x(right)-x(left):.2f}" height="198" fill="#f5b544" opacity=".04"/>')
        stage_labels.append(f'<span class="html-stage-label" style="left:{x(left)/9:.2f}%">{_e(band.get("label", ""))}</span>')
    for index, (name, points) in enumerate(series):
        drawn = decimate(points)
        path = f'M{x(drawn[0][0]):.2f},{y(drawn[0][1]):.2f}'
        path += ''.join((f'H{x(a):.2f}V{y(b):.2f}' if step else f'L{x(a):.2f},{y(b):.2f}') for a, b in drawn[1:])
        body.append(f'<path d="{path}" fill="none" stroke="{COLORS[index % len(COLORS)]}" stroke-width="1.6"'
                    + (' stroke-dasharray="6 4"' if step and index else '') + f'><title>{_e(name)}</title></path>')
        if step and name == 'Intended':
            transitions = [point for i, point in enumerate(points) if not i or point[1] != points[i-1][1]]
            body.extend(f'<circle class="html-transition" cx="{x(a):.2f}" cy="{y(b):.2f}" r="3" fill="{COLORS[index % len(COLORS)]}"><title>Intended: {_fmt(b)} at {_fmt(a)} s</title></circle>'
                        for a, b in transitions[:40])
    svg = ('<svg viewBox="0 0 900 280" preserveAspectRatio="none" role="img" tabindex="0" aria-label="' + _e(desc) + '"><desc>' + _e(desc) + '</desc>'
            + '<path d="M48,24V220H864" fill="none" stroke="#555159"/>' + ''.join(body)
            + '</svg>')
    legend = '<div class="html-legend">' + ''.join(f'<span><i style="color:{COLORS[index % len(COLORS)]}"></i>{_e(name)}</span>' for index, (name, _) in enumerate(series)) + '</div>'
    return (legend + _chart_frame(svg, xmin, xmax, ymin, ymax, xlabel, ylabel, ''.join(stage_labels))
            + '<p class="html-note">Min/max decimation for drawing; every request included in metrics.</p>')


def _plot(title, content, reason):
    return '<article class="html-plot"><h3>' + _e(title) + '</h3>' + (content or '<p class="html-note">' + _e(reason) + '</p>') + '</article>'


def _buckets(data):
    rows = (data or {}).get('buckets', [])
    if not rows:
        return ''
    duration = rows[-1]['end_s']
    maximum = max(row['count'] for row in rows)
    if duration <= 0 or not maximum:
        return ''
    xmax, ymax = _nice_max(0, duration), _nice_max(0, maximum, integer=True)
    # Backend bounds completion buckets to approximately twenty per window.
    bars = ''.join(f'<rect x="{48+816*r["start_s"]/xmax+3:.2f}" y="{220-180*r["count"]/ymax:.2f}"'
                   f' width="{max(1,816*r["duration_s"]/xmax-6):.2f}" height="{180*r["count"]/ymax:.2f}" fill="#f5b544">'
                   f'<title>{_fmt(r["start_s"])}–{_fmt(r["end_s"])} s · {r["count"]} completed requests'
                   + (' · partial bucket' if r['partial'] else '') + '</title></rect>' for r in rows)
    desc = f'Completed-request counts in {len(rows)} elapsed-time buckets, nominal width {data["width_s"]} seconds. Includes failed terminal requests; counts are not a rate.'
    svg = f'<svg viewBox="0 0 900 280" preserveAspectRatio="none" role="img" tabindex="0" aria-label="{_e(desc)}"><desc>{_e(desc)}</desc>' + bars + '</svg>'
    caption = (_fmt(data['width_s']) + '-second buckets; '
               + ('the final bucket uses its actual duration (' + _fmt(rows[-1]['duration_s']) + ' s).'
                  if data['final_partial_bucket'] else 'all buckets use the full duration.'))
    return (_chart_frame(svg, 0, xmax, 0, ymax, 'Elapsed seconds', 'Completed requests')
            + '<p class="html-note">' + caption + '</p><details><summary>Bucket definitions</summary><p>'
            + _e(data['rule']) + ' ' + _e(data.get('boundary_rule', '')) + ' '
            + _e(data['partial_bucket_disclosure']) + '</p></details>')


def _section(prefix, name, title, note, body):
    return f'<section class="html-section" id="{prefix}-{name}"><header><h2>{_e(title)}</h2><p>{_e(note)}</p></header>{body}</section>'


def render_performance(performance, *, prefix='performance'):
    """Render one selected target/window; full request series remains local once."""
    if not performance.get('targets'):
        return '<p class="card">No target or request measurements were recorded. Performance is unavailable; inspect Test results for coverage.</p>'
    parts = []
    for target in performance.get('targets', []):
        summary = target['summaries'][target.get('selected_window', target['default_window'])]
        metrics, sections = summary['metrics'], summary['sections']
        window = summary['window']
        rows = [target['request_series'][index] for index in summary['request_row_indices']]
        pfx = prefix if len(performance['targets']) == 1 else prefix + '-' + str(len(parts))
        parts.append('<div class="html-scope"><strong>' + _e(target['target']) + '</strong> · '
            + _e(window['label']) + ' · ' + _fmt(window.get('duration_s')) + ' s · '
            + _fmt(window['request_count']) + ' requests<p class="html-note">'
            + _e(' · '.join(filter(None, (window.get('workload_label'), window.get('stages_description')))))
            + ' ' + _e(window.get('population', '')) + ' ' + _e(target.get('scope_note', '')) + '</p>'
            + '<p class="html-note">Absent evidence stays unavailable. Collect eligible observations for this scope. Specific exclusions and collection instructions appear below.</p></div>')
        if len(target.get('windows', [])) > 1:
            parts.append('<details><summary>Measurement windows · static report</summary>'
                '<p>This document shows the default measurement for this target. Other windows are selectable in the app; populations are never pooled.</p>'
                '<div class="table"><table><tr><th>Window<th>Bounds (ms)<th>Requests<th>Population'
                + ''.join('<tr><th scope="row">' + _e(item.get('selection_label') or item['label']) + '<td>'
                    + _fmt(item.get('start_ms')) + '–' + _fmt(item.get('end_ms')) + '<td>'
                    + _fmt(item['request_count']) + '<td>' + _e(item.get('population', ''))
                    for item in target['windows'][:50]) + '</table></div>'
                + (f'<p>Showing 50 of {len(target["windows"])} windows; complete catalog in report.json.</p>'
                   if len(target['windows']) > 50 else '') + '</details>')
        timing = [r for key, r in summary['distributions'].items() if not key.endswith('_length')]
        distributions = _distributions(timing, performance['percentile_convention'])
        headlines = [dict(metrics[key]) for key in KPI_IDS]
        for row in headlines:
            if row['id'] == 'output_throughput':
                row['display_note'] = 'Answer tokens; completion accounting below'
            elif row['id'] == 'generation_pace':
                row['display_note'] = 'Median · answer streaming only'
        parts.append(_section(pfx, 'overview', 'Performance overview', 'One target and measurement population.',
            _cards(headlines) + '<details id="' + pfx + '-distributions"><summary>All timing percentiles &amp; definitions</summary>' + distributions + '</details>'))
        def points(field, divisor=1):
            return [(row['number'], row['values'].get(field)/divisor) for row in rows if _number(row['values'].get(field))]
        timing_series = [(name, points(field, 1000)) for name, field in (
            ('Response latency', 'response_latency_ms'), ('Before first useful output', 'before_first_useful_output_ms'),
            ('Answer streaming time', 'answer_streaming_ms'), ('Completion time (tail)', 'completion_tail_ms'), ('TTFT (client)', 'client_ttft_ms'))]
        chart = _chart('Timing across requests', timing_series)
        streaming_max = (summary['distributions']['answer_streaming'].get('value') or {}).get('max')
        support = [dict(metrics['answer_streaming'], display_note='Median · Max ' + _fmt(streaming_max) + ' ms'),
                   dict(metrics['longest_stream_pause'], display_note='Maximum answer-read gap; not token ITL')]
        parts.append(_section(pfx, 'timing', '02 Request timing', 'Each observation is a request, ordered by dispatch.',
            _plot('Timing across requests', chart, 'No request timing observations to connect; at least two observations are needed.')
            + '<p class="html-note">Lines connect observations. Phases are not all additive; read gaps are not token ITL.</p>'
            + _cards(support, 'html-support')
            + '<details><summary>Timing distributions &amp; definitions</summary><p><a href="#' + pfx + '-distributions">All timing percentiles &amp; definitions above</a></p></details>'))
        capacity = [row for row in sections['throughput_capacity'] if not (row['id'] == 'completion_throughput' and row.get('semantically_identical_to_answer'))]
        bucket_data = summary.get('completion_buckets')
        bucket_reason = ('No completed requests in this selected window; recorded bucket counts are zero.'
                         if (bucket_data or {}).get('buckets') else
                         'No completed-request bucket observations; request lifetimes were not collected.')
        parts.append(_section(pfx, 'throughput', '03 Throughput & capacity', 'Input and normalized rates are distinct from headline output.',
            '<div class="html-throughput">' + _plot('Completed requests over time', _buckets(bucket_data), bucket_reason)
            + _cards(capacity, 'html-capacity') + '</div>'))
        occupancy = summary.get('actual_concurrency', {})
        actual = [(p['elapsed_s'], p['in_flight']) for p in occupancy.get('points', [])] if occupancy.get('sample_count') else []
        intended = [(x, p['intended_concurrency']) for p in summary.get('intended_concurrency', []) for x in (p['elapsed_s'], p['end_elapsed_s'])]
        concurrency = _chart('Actual and intended concurrency', [('Actual', actual), ('Intended', intended)], xlabel='Elapsed seconds', ylabel='Requests in flight', step=True, bands=summary.get('schedule_intervals', []))
        pause_rows = summary.get('client_pauses', [])
        pauses = ('<ul>' + ''.join('<li>' + _fmt(p['elapsed_s']) + ' s · ' + _fmt(p['lag_ms']) + ' ms scheduling delay</li>' for p in pause_rows[:50]) + '</ul>'
            + (f'<p>{len(pause_rows)} pauses; showing first 50. Complete evidence in report.json.</p>' if len(pause_rows)>50 else '') if pause_rows
            else '<p class="html-note">' + ('No scheduling delays above the recording threshold were observed in this window.' if summary.get('client_pause_threshold_ms') is not None else 'Client scheduling-delay recording was not collected.') + '</p>')
        parts.append(_section(pfx, 'delivery', '04 Load delivery', 'Actual concurrency uses request lifetimes; intended uses recorded transitions.',
            _cards([metrics[key] for key in ('peak_concurrency', 'average_concurrency', 'configured_ceiling')], 'html-load')
            + _plot('Actual and intended concurrency', concurrency, 'No request lifetime observations or recorded intended schedule.')
            + ('<p class="html-note">' + _e(summary['intended_reason']) + '</p>' if summary.get('intended_reason') else '')
            + '<h3>When did the client pause?</h3>' + pauses + _table(sections['delivery'])))
        parts.append(_section(pfx, 'outcomes', '05 Outcomes & quality', 'Completion does not establish answer correctness.', _table(sections['outcomes'])))
        token_charts = '<div class="html-two">' + _plot('Input and answer size by request', _chart('Input and answer size by request', [('Input', points('input_tokens')), ('Answer', points('answer_tokens'))], ylabel='tokens'), 'Insufficient input/answer token observations; unknown usage remains unknown.')
        token_charts += _plot('Cached prompt tokens by request', _chart('Cached prompt tokens by request', [('Cached prompt', points('cached_prompt_tokens'))], ylabel='cached prompt tokens'), 'Insufficient cached-prompt usage observations; no cache reuse inferred.') + '</div>'
        parts.append(_section(pfx, 'tokens', '06 Workload & tokens', 'Cached input is a subset, never extra tokens or a measured speedup.', token_charts
            + '<h3>Token accounting · selected window</h3>' + _table(sections['token_accounting'])
            + '<details><summary>Token-length distributions</summary>' + _distributions([r for key, r in summary['distributions'].items() if key.endswith('_length')], performance['percentile_convention']) + '</details>'))
        for name, title, key, note in (
            ('effects', '07 Conversations & workload effects', 'conversations', 'Workload-specific evidence remains separate.'),
            ('telemetry', '08 Server observations', 'server', 'Server-wide counters may include other traffic; separate collection window.')):
            parts.append(_section(pfx, name, title, note, _table(sections[key])))
    content = ''.join(parts)
    return ('<style>' + CHART_STYLE + '</style>' if '<svg' in content else '') + content
