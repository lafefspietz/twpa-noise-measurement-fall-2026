"""Predefined experiments: what can be set, and what a run would do with those settings.

An Experiment describes its settings as a list of Params. The web UI builds the settings
form from describe(), so a new experiment needs no UI code. plan(params) checks the
settings and lists every sweep point, with a time estimate, before anything runs; it
does not change any instrument. run(params, job) does the measurement in a job thread
(jobs.py): it reserves its instruments, checks job.check() between steps, reports with
job.progress()/job.add_row(), and fills job.record, which the runner saves however the
run ends (done, stopped, outputs off, error).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..drivers.base import limit_text, within

# caps on what one run may ask for; changed in the settings tab (cfg["experiments"])
CAPS = {"max_points": 2000, "max_total_points": 10000, "big_file_mb": 200}


def outside(values, lim) -> str | None:
    """'a, b, c ...' for the values outside lim ([lo, hi], None ends unbounded), or None."""
    bad = sorted({v for v in values if not within(lim, v)})
    if not bad:
        return None
    return ", ".join(f"{v:g}" for v in bad[:5]) + (" ..." if len(bad) > 5 else "")


class ExperimentError(Exception):
    """Settings that cannot be used (wrong type, unknown key, unknown option)."""


@dataclass
class Param:
    key: str
    label: str
    kind: str = "number"            # number | int | select | bool | text | table
    default: Any = None
    unit: str = ""                  # unit the UI shows; value in the UI = value / scale
    scale: float = 1.0
    options: list[list[str]] = field(default_factory=list)    # select: [value, label] pairs
    group: str = ""
    help: str = ""
    show_if: dict[str, list] = field(default_factory=dict)    # shown only when other params match
    from_state: list[str] | None = None                       # [instrument, state key] for "load current values"
    columns: list["Param"] = field(default_factory=list)      # table: one Param per column; value = list of rows

    def _parse_table(self, value) -> list[dict]:
        if not isinstance(value, list) or not all(isinstance(r, dict) for r in value):
            raise ExperimentError(f"{self.key}: expected a list of rows")
        rows = []
        for i, row in enumerate(value, 1):
            unknown = set(row) - {c.key for c in self.columns}
            if unknown:
                raise ExperimentError(f"{self.key} row {i}: unknown column(s) {sorted(unknown)}")
            parsed = {c.key: c.parse(row.get(c.key)) for c in self.columns}
            missing = [c.label for c in self.columns if parsed[c.key] is None]
            if missing:
                raise ExperimentError(f"{self.key} row {i}: missing {', '.join(missing)}")
            rows.append(parsed)
        return rows

    def parse(self, value):
        if value is None or value == "":
            return self.default
        if self.kind == "table":
            return self._parse_table(value)
        try:
            if self.kind == "number":
                return float(value)
            if self.kind == "int":
                if float(value) != int(float(value)):
                    raise ValueError
                return int(float(value))
            if self.kind == "bool":
                if isinstance(value, str):
                    return value.lower() in ("1", "true", "on", "yes")
                return bool(value)
            if self.kind == "select":
                if value not in [v for v, _ in self.options]:
                    raise ValueError
                return value
            return str(value)
        except (TypeError, ValueError):
            raise ExperimentError(f"{self.key}: {value!r} is not a valid {self.kind}") from None

    def shown(self, params: dict) -> bool:
        return all(params.get(k) in vals for k, vals in self.show_if.items())


class Experiment:
    id = ""
    title = ""
    summary = ""
    instruments: list[str] = []     # what a run reserves
    turns_on = ""                   # outputs a run turns on, for the start confirmation
    params: list[Param] = []

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def limits(self, inst: str) -> dict:
        return self.cfg["instruments"].get(inst, {}).get("limits", {})

    def cap(self, name: str):
        return self.cfg.get("experiments", {}).get(name, CAPS[name])

    def check_limit(self, errors: list[str], inst: str, key: str, values, what: str, unit: str = ""):
        """Adds an error if any of values is outside the instrument limit `key` (the
        apply() would refuse it mid-run otherwise)."""
        lim = self.limits(inst).get(key)
        values = values if isinstance(values, (list, tuple)) else [values]
        bad = lim and outside(values, lim)
        if bad:
            errors.append(f"{what} outside the limit {limit_text(lim)}{' ' + unit if unit else ''}: {bad}")

    def recorded_params(self, p: dict) -> dict:
        """p without the params the form hides for these choices (e.g. the reference level in W
        when it was typed in dBm, the pump settings when the pump is left off), so a record
        only has values the run used."""
        hidden = {q.key for q in self.params if not q.shown(p)}
        return {k: v for k, v in p.items() if k not in hidden}

    def saved_defaults(self) -> dict:
        """Defaults saved from the experiments tab (settings.local.yaml), by key."""
        known = {p.key for p in self.params}
        saved = self.cfg.get("experiment_defaults", {}).get(self.id) or {}
        return {k: v for k, v in saved.items() if k in known}

    def describe(self) -> dict:
        saved = self.saved_defaults()
        params = [{**asdict(p), "default": saved.get(p.key, p.default), "builtin_default": p.default}
                  for p in self.params]
        return {"id": self.id, "title": self.title, "summary": self.summary,
                "instruments": self.instruments, "turns_on": self.turns_on,
                "params": params, "saved_defaults": sorted(saved)}

    def parse(self, raw: dict) -> dict:
        """Defaults filled in, values converted to their types. Unknown keys are refused
        so a typo never silently falls back to a default."""
        known = {p.key: p for p in self.params}
        unknown = set(raw) - set(known)
        if unknown:
            raise ExperimentError(f"unknown setting(s) {sorted(unknown)}")
        saved = self.saved_defaults()
        return {k: saved[k] if k in saved and raw.get(k) in (None, "") else p.parse(raw.get(k))
                for k, p in known.items()}

    def plan(self, params: dict, mgr=None) -> dict:
        """{"points": [...], "columns": [...], "estimate_s": float, "errors": [...],
        "warnings": [...], "notes": [...]}, optionally "derived": {key: value} for settings
        left empty that get a value from the others (the UI shows it in gray in the empty
        field). Must not change any instrument."""
        raise NotImplementedError

    def run(self, params: dict, job) -> None:
        raise NotImplementedError
