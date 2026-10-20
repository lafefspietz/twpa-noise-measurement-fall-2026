"""Interactive HTML page of a saved experiment record: one file, no server needed.

The plots are the experiments tab's own (static/results.js, inlined), drawn from the rows
the experiment rebuilds from the record (Experiment.result_view), so they look and react
as they did during the run; each row also carries every per-point value of the record,
shown for the point under the cursor. Below the plots: the settings of the run (as the
form showed them), the state of every instrument at the start, the states read back
during the run and every other value in the record. The JSON stays the data file.

    GET /api/data/<day>/<epoch>-<label>.html        # next to the .json, built on request
    python -m labserver.report <record.json> ...    # writes <record>.html next to each
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from html import escape
from pathlib import Path

from .experiments import EXPERIMENTS

STATIC = Path(__file__).resolve().parent / "static"

# top-level record fields shown in the overview, not again under "other values"
OVERVIEW = ("experiment", "kind", "status", "message", "notes", "note", "sim", "timestamp", "date", "time",
            "timezone", "start_time", "finish_time", "sweep_time", "finish", "analysis_note")
STATE_TITLES = {"spa_setup": "SA as set up for the sweep (what it took)",
                "synth_state": "SynthHD once the pump was on",
                "probe_state": "probe (SMW200A) once it was on",
                "pump_state": "TWPA pump during the sweep",
                "attenuator_state": "attenuator during the sweep",
                "flux_bias_state": "qubit flux bias during the sweep",
                "reference": "pump-off reference"}
SHORT_LIST = 12         # lists up to this long are shown in full


class NotAnExperiment(ValueError):
    pass


# ---------------------------------------------------------------- values as text
def _scalar(v) -> bool:
    return v is None or isinstance(v, (bool, int, float, str))


def _num(v: float) -> str:
    return f"{v:.10g}"


def _hz(v: float) -> str:
    for scale, unit in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")):
        if abs(v) >= scale:
            return f"{v / scale:.10g} {unit}"
    return f"{v:g} Hz"


def _epoch(v: float) -> str:
    return datetime.fromtimestamp(v).astimezone().strftime("%Y-%m-%d %H:%M:%S UTC%z")


def value_text(key: str, v) -> str:
    """A record value as text; Hz, epoch times and W get a readable form next to them."""
    if v is None:
        return "none"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        out = _num(v)
        if key.endswith("_hz") and abs(v) >= 1e3:
            out += f"  ({_hz(v)})"
        elif (key.endswith("_time") or key == "timestamp") and 1e9 < v < 4e9:
            out += f"  ({_epoch(v)})"
        elif key.endswith("_w") and v > 0:
            out += f"  ({10 * math.log10(v / 1e-3):.3f} dBm)"
        return out
    if isinstance(v, list):
        if len(v) <= SHORT_LIST and all(_scalar(x) for x in v):
            return ", ".join(value_text(key, x) if not isinstance(x, str) else x for x in v) or "(empty)"
        return f"[{shape(v)} values: in the JSON file]"
    return str(v)


def shape(v) -> str:
    dims = []
    while isinstance(v, list):
        dims.append(len(v))
        v = v[0] if v else None
    return " x ".join(map(str, dims))


def flatten(d: dict, prefix: str = "") -> list[tuple[str, object]]:
    out = []
    for k, v in d.items():
        if isinstance(v, dict):
            out += flatten(v, f"{prefix}{k}.")
        else:
            out.append((prefix + k, v))
    return out


def kv_table(pairs) -> str:
    rows = "".join(f"<tr><th>{escape(k)}</th><td>{escape(value_text(k.rsplit('.', 1)[-1], v))}</td></tr>"
                   for k, v in pairs)
    return f'<table class="kv">{rows}</table>'


# ---------------------------------------------------------------- settings, as the form shows them
def param_text(p: dict, v) -> str:
    if p["kind"] == "bool":
        return "yes" if v else "no"
    if p["kind"] == "select":
        names = dict(p["options"])
        return f"{names.get(v, v)}  ({v})"
    if p["kind"] == "text":
        return v if v else "(empty)"
    if v is None:
        return "empty"
    return f"{v / p['scale']:.10g} {p['unit']}".strip()


def param_table(p: dict, rows: list[dict]) -> str:
    cols = p["columns"]
    head = "".join("<th>" + escape(c["label"] + (f" [{c['unit']}]" if c["unit"] else "")) + "</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{escape(_num(r[c['key']] / c['scale']))}</td>" for c in cols) + "</tr>"
                   for r in rows)
    return f'<table class="grid"><tr>{head}</tr>{body}</table>'


def settings_html(described: dict, params: dict) -> str:
    groups: dict[str, list[str]] = {}
    known = set()
    for p in described["params"]:
        known.add(p["key"])
        if p["key"] not in params:
            continue        # hidden in the form for these choices: the run did not use it
        v = params[p["key"]]
        value = param_table(p, v) if p["kind"] == "table" else escape(param_text(p, v))
        groups.setdefault(p["group"] or "settings", []).append(
            f'<tr title="{escape(p["help"])}"><th>{escape(p["label"])}</th><td>{value}</td>'
            f'<td class="key">{escape(p["key"])}</td></tr>')
    other = [(k, v) for k, v in params.items() if k not in known]
    if other:
        groups["not in this version of the form"] = [
            f'<tr><th>{escape(k)}</th><td>{escape(value_text(k, v))}</td><td class="key">{escape(k)}</td></tr>'
            for k, v in other]
    return "".join(f'<fieldset><legend>{escape(g)}</legend><table class="kv">{"".join(rows)}</table></fieldset>'
                   for g, rows in groups.items())


# ---------------------------------------------------------------- instruments
def instruments_html(snapshot: dict, used: list[str]) -> str:
    out = []
    for name in sorted(snapshot, key=lambda n: (n not in used, list(snapshot).index(n))):
        st = dict(snapshot[name])
        idn = st.pop("idn", None)
        legend = escape(name) + (" <b>(used by this experiment)</b>" if name in used else "")
        note = f'<div class="muted">{escape(idn)}</div>' if idn else ""
        if "cached" in st:
            c = st.pop("cached")
            note += (f'<div class="warn">busy at the start: the settings it last reported, {c.get("age_s")} s '
                     f'old ({escape(str(c.get("reason")))})</div>')
        if "error" in st:
            body = f'<div class="warn">could not read it: {escape(str(st["error"]))}</div>'
        else:
            body = kv_table(flatten(st))
        out.append(f"<fieldset><legend>{legend}</legend>{note}{body}</fieldset>")
    return '<div class="cols">' + "".join(out) + "</div>"


# ---------------------------------------------------------------- the page
def per_point_keys(rec: dict, n: int) -> list[str]:
    """Record fields with one scalar per point."""
    return [k for k, v in rec.items() if isinstance(v, list) and n and len(v) == n and all(_scalar(x) for x in v)]


def _clean(v):
    """For JSON.parse: NaN/inf -> null."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_clean(x) for x in v]
    return v


def view_data(exp, rec: dict) -> dict:
    view = exp.result_view(rec) or {"axis": {"plot": "none"}, "rows": []}
    rows = view["rows"]
    keys = per_point_keys(rec, len(rows))
    for i, row in enumerate(rows):
        row["point_index"] = i
        row["point"] = {k: rec[k][i] for k in keys}
    return _clean(view)


def overview_html(described: dict, rec: dict, file_name: str) -> str:
    pairs = [("file", file_name), ("experiment", f'{described["title"]} ({rec.get("experiment")})'),
             ("status", rec.get("status")), ("message", rec.get("message") or "")]
    if rec.get("sim"):
        pairs.append(("simulated", "yes: simulators, not the instruments"))
    for k in ("start_time", "finish_time"):
        if k in rec:
            pairs.append((k.replace("_", " "), _epoch(rec[k])))
    if "sweep_time" in rec:
        pairs.append(("duration", f"{rec['sweep_time']:g} s"))
    if "timestamp" in rec:
        pairs.append(("saved", _epoch(rec["timestamp"])))
    pairs.append(("note", rec.get("note") or ""))
    rows = "".join(f"<tr><th>{escape(k)}</th><td>{escape(str(v))}</td></tr>" for k, v in pairs)
    lists = ""
    for key, title in (("notes", "notes from the run"), ("finish", "when done")):
        if rec.get(key):
            lists += f"<tr><th>{title}</th><td><ul>" + "".join(f"<li>{escape(str(x))}</li>" for x in rec[key]) \
                     + "</ul></td></tr>"
    if rec.get("analysis_note"):
        lists += f'<tr><th>analysis</th><td>{escape(rec["analysis_note"])}</td></tr>'
    return f'<table class="kv">{rows}{lists}</table>'


def render(exp, rec: dict, file_name: str) -> str:
    """The HTML page of a saved experiment record (a dict as loaded from its JSON)."""
    if rec.get("kind") != "experiment" or rec.get("experiment") != exp.id:
        raise NotAnExperiment(f"{file_name} is not a {exp.id} record")
    described = exp.describe()
    view = view_data(exp, rec)
    n = len(view["rows"])
    states = [(k, v) for k, v in rec.items() if isinstance(v, dict) and k not in ("instruments", "params")]
    states_html = "".join(
        f"<fieldset><legend>{escape(STATE_TITLES.get(k, k))} <span class=key>{escape(k)}</span></legend>"
        f"{kv_table(flatten(v))}</fieldset>" for k, v in states)
    point_keys = set(per_point_keys(rec, n))
    skip = set(OVERVIEW) | {"instruments", "params"} | {k for k, _ in states}
    other = [(k, v) for k, v in rec.items() if k not in skip and k not in point_keys]
    small = [(k, v) for k, v in other if _scalar(v) or (isinstance(v, list) and len(v) <= SHORT_LIST
                                                         and all(_scalar(x) for x in v))]
    big = [(k, v) for k, v in other if (k, v) not in small]
    arrays = ", ".join(f"{escape(k)} [{shape(v)}]" for k, v in big + [(k, rec[k]) for k in sorted(point_keys)])
    data = json.dumps(view, separators=(",", ":"), allow_nan=False).replace("</", "<\\/")
    name, title = escape(described["title"]), escape(f'{described["title"]}: {file_name}')
    used = escape(", ".join(described["instruments"]))
    during = f'<h2>Read back during the run</h2><div class="cols">{states_html}</div>' if states_html else ""
    css = (STATIC / "results.css").read_text(encoding="utf-8")
    js = (STATIC / "results.js").read_text(encoding="utf-8")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ margin: 12px; background: #fff; color: #000; font: 14px/1.4 "Courier New", monospace; }}
  h1 {{ font: bold 18px Arial, sans-serif; margin: 0 0 4px; }}
  h2 {{ font: bold 16px Arial, sans-serif; margin: 22px 0 6px; border-bottom: 1px solid #000; }}
  input, select {{ font: inherit; border: 1px solid #000; padding: 1px 3px; }}
  input[type=number] {{ width: 110px; }}
  .row {{ margin: 6px 0; }}
  .muted, .key {{ color: #666; }}
  .key {{ font-size: 12px; font-weight: normal; }}
  .warn {{ font-weight: bold; }}
  fieldset {{ border: 1px solid #000; margin: 10px 0; padding: 4px 10px 8px; }}
  legend {{ font-weight: bold; padding: 0 4px; }}
  table.kv {{ border-collapse: collapse; }}
  table.kv th {{ font-weight: normal; text-align: left; vertical-align: top; padding: 1px 16px 1px 0; color: #444; }}
  table.kv td {{ padding: 1px 16px 1px 0; vertical-align: top; overflow-wrap: anywhere; }}
  table.kv ul {{ margin: 0; padding-left: 18px; }}
  fieldset table.kv {{ width: 100%; }}
  fieldset table.kv th {{ width: 32%; }}
  td.key {{ width: 1%; white-space: nowrap; }}
  .cols {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 560px), 1fr)); column-gap: 16px;
           align-items: start; }}
  .cols fieldset {{ margin: 6px 0; }}
  table.grid {{ border-collapse: collapse; }}
  table.grid th, table.grid td {{ border: 1px solid #000; padding: 1px 8px; text-align: right; }}
  table.grid th {{ font-weight: normal; }}
  tr[title]:hover th {{ text-decoration: underline dotted; }}
{css}</style>
</head>
<body>
<h1>{name}</h1>
<div class="muted">{escape(described['summary'])}</div>
<h2>Run</h2>
{overview_html(described, rec, file_name)}
<h2>Result</h2>
<div class="muted">{n} point(s). Hover the plots for values; the table under them has every value saved for that point.</div>
<div id="resultView"></div>
<h2>Settings</h2>
<div class="muted">As set in the form (hover a setting for its help). Settings the form hid for these choices were not used and are not listed.</div>
{settings_html(described, rec.get('params', {}))}
<h2>Instruments at the start of the run</h2>
<div class="muted">Read just before the run changed anything; this experiment uses {used}.</div>
{instruments_html(rec.get('instruments', {}), described['instruments'])}
{during}
<h2>Other values in the record</h2>
{kv_table(small)}
<div class="row muted">Arrays (in the JSON file, {escape(file_name)}): {arrays or 'none'}</div>
<script type="application/json" id="resultData">{data}</script>
<script>
{js}</script>
<script>
resultInit(document.getElementById("resultView"));
showResult(JSON.parse(document.getElementById("resultData").textContent));
window.addEventListener("resize", drawResult);
</script>
</body>
</html>
"""


def experiment_for(rec: dict, cfg: dict | None = None):
    """The Experiment that wrote this record (None if it is not an experiment record)."""
    cls = next((c for c in EXPERIMENTS if c.id == rec.get("experiment")), None)
    if rec.get("kind") != "experiment" or cls is None:
        return None
    return cls(cfg or {"instruments": {}})


def main():
    parser = argparse.ArgumentParser(description="Write the interactive HTML page of saved experiment records.")
    parser.add_argument("records", nargs="+", type=Path, help="record JSON files")
    args = parser.parse_args()
    for path in args.records:
        rec = json.loads(path.read_text(encoding="utf-8"))
        exp = experiment_for(rec)
        if exp is None:
            print(f"{path}: not an experiment record, skipped")
            continue
        out = path.with_suffix(".html")
        out.write_text(render(exp, rec, path.name), encoding="utf-8")
        print(f"{path} -> {out}")


if __name__ == "__main__":
    main()
