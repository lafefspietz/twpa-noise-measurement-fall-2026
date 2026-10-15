"""Settings the web UI can change while the server runs (the settings tab).

config.yaml is the base and the server never writes it. A change made in the settings
tab is applied at once and saved in settings.local.yaml next to config.yaml (gitignored,
so a `git push` deploy keeps it); at start-up that file is applied over config.yaml.
Only what schema() lists can be changed this way: wiring (addresses, VISA names, switch
mapping), init commands, the server address and the data folder stay in config.yaml.

- Limits are [min, max]; either end may be None (no bound).
- Loosening a limit (a lower min, a higher max, an end removed) or turning a safety
  switch on (e.g. GS210 current mode) needs confirm=True and is logged as a warning.
- A value set back to what config.yaml says is dropped from settings.local.yaml.
- Experiment defaults saved from the experiments tab are kept in the same file
  (experiment_defaults: {experiment id: {setting: value}}).
"""
from __future__ import annotations

import copy
import logging
import math
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .drivers import DRIVERS
from .experiments.base import CAPS
from .live import LIVE_DEFAULTS, MAX_FPS_DEFAULT, has_live_view

log = logging.getLogger("labserver.settings")

SECTIONS = [
    {"id": "limits", "title": "Instrument limits",
     "help": "Checked before anything is sent to an instrument, by the instruments tab and by the "
             "experiments (their plan shows a value outside a limit as an error). Empty = no bound. "
             "Loosening a limit asks for a confirmation. Raw SCPI commands bypass the limits."},
    {"id": "live", "title": "Live view",
     "help": "The SA/VNA trace on the instruments tab. Applies from the next frame."},
    {"id": "instruments", "title": "Instrument I/O",
     "help": "Timeouts, retries and waits. Apply from the next command (a read already running keeps "
             "its timeout)."},
    {"id": "experiments", "title": "Experiments",
     "help": "Caps on what one run may ask for, checked when the settings are planned."},
    {"id": "safety", "title": "Outputs off", "help": ""},
]

HEADER = ("# Written by the settings tab of the web UI; applied over config.yaml at start-up.\n"
          "# Delete an entry (or this file) to go back to what config.yaml says.\n")

_MISSING = object()


class SettingsError(Exception):
    """A setting that does not exist or a value it cannot take."""


class ConfirmNeeded(Exception):
    """The change loosens a limit or turns a safety switch on; resend with confirm."""

    def __init__(self, loosened: list[str]):
        super().__init__("this loosens: " + "; ".join(loosened))
        self.loosened = loosened


@dataclass
class Setting:
    id: str                         # dotted path into the config, e.g. instruments.vna.limits.power_dbm
    section: str
    group: str                      # sub-heading: instrument name, ...
    label: str
    kind: str = "number"            # number | int | bool | limit ([min, max], None = no bound)
    unit: str = ""                  # unit the UI shows; value in the UI = value / scale
    scale: float = 1.0
    default: Any = None             # when neither config.yaml nor settings.local.yaml sets it
    min: float | None = None
    nullable: bool = False          # number: empty allowed (e.g. no ramp)
    safety: bool = False            # bool: on is the less safe side (needs a confirmation)
    help: str = ""

    @property
    def path(self) -> tuple[str, ...]:
        return tuple(self.id.split("."))

    def show(self, v) -> str:
        if v is None:
            return "none"
        if isinstance(v, bool):
            return "on" if v else "off"
        return f"{v / self.scale:g}{' ' + self.unit if self.unit else ''}"

    def text(self, v) -> str:
        if self.kind == "limit":
            lo, hi = v or (None, None)
            return f"[{self.show(lo)}, {self.show(hi)}]"
        return self.show(v)

    def parse(self, value):
        """The value in config form, or SettingsError."""
        what = f"{self.group} {self.label}"
        if self.kind == "limit":
            if value is None:
                return [None, None]
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise SettingsError(f"{what}: a limit is [min, max], got {value!r}")
            lo, hi = (None if v is None or v == "" else self._number(v, what) for v in value)
            if lo is not None and hi is not None and lo > hi:
                raise SettingsError(f"{what}: min {self.show(lo)} is above max {self.show(hi)}")
            return [lo, hi]
        if self.kind == "bool":
            if not isinstance(value, bool):
                raise SettingsError(f"{what}: expected true or false, got {value!r}")
            return value
        if value is None or value == "":
            if self.nullable:
                return None
            raise SettingsError(f"{what}: a value is needed")
        v = self._number(value, what)
        if self.kind == "int":
            if v != int(v):
                raise SettingsError(f"{what}: {value!r} is not a whole number")
            v = int(v)
        if self.min is not None and v < self.min:
            raise SettingsError(f"{what}: {self.show(v)} is below the minimum {self.show(self.min)}")
        return v

    @staticmethod
    def _number(value, what) -> float:
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise SettingsError(f"{what}: {value!r} is not a number") from None
        if not math.isfinite(v):
            raise SettingsError(f"{what}: {value!r} is not a finite number")
        return v

    def loosens(self, old, new) -> str | None:
        """'vna power: max 0 dBm -> none' if new is less safe than old, else None."""
        parts = []
        if self.kind == "limit":
            (olo, ohi), (nlo, nhi) = old or (None, None), new
            if olo is not None and (nlo is None or nlo < olo):
                parts.append(f"min {self.show(olo)} -> {self.show(nlo)}")
            if ohi is not None and (nhi is None or nhi > ohi):
                parts.append(f"max {self.show(ohi)} -> {self.show(nhi)}")
        elif self.safety and new and not old:
            parts.append("on")
        return f"{self.group} {self.label}: {', '.join(parts)}" if parts else None


def schema(cfg: dict) -> list[Setting]:
    """Every setting the settings tab offers for this config."""
    out = []
    for name, icfg in cfg.get("instruments", {}).items():
        drv = DRIVERS.get(icfg.get("driver"))
        if drv is None:
            continue
        specs = list(drv.limit_specs(icfg))
        offered = {key for key, *_ in specs}
        # limits config.yaml sets that the driver doesn't list (checked all the same)
        specs += [(key, key, "", 1) for key in icfg.get("limits") or {} if key not in offered]
        for key, label, unit, scale in specs:
            out.append(Setting(f"instruments.{name}.limits.{key}", "limits", name, label, "limit", unit, scale,
                               default=[None, None]))
        for s in drv.SAFETY:
            out.append(Setting(f"instruments.{name}.{s['key']}", "limits", name, s["label"], "bool",
                               default=s.get("default", False), safety=True, help=s.get("help", "")))
        for t in drv.tunable_specs(icfg):
            default = t.get("default", drv.TIMEOUT_MS if t["key"] == "timeout_ms" else None)
            out.append(Setting(f"instruments.{name}.{t['key']}", "instruments", name, t["label"],
                               t.get("kind", "number"), t.get("unit", ""), t.get("scale", 1.0), default,
                               t.get("min"), t.get("nullable", False), help=t.get("help", "")))

    live = [("max_sweep_s", "longest sweep shown live", "number", "s", 0.001,
             "A live read holds the instrument for the whole sweep (x averages / sweep count), so longer "
             "sweeps are not started: they would block the settings change that shortens them."),
            ("max_retries", "read attempts per frame", "int", "", 1,
             "The loop tries again anyway, and in between the instrument is free for clicks."),
            ("state_refresh_s", "re-read settings every", "number", "s", 0.1,
             "So front-panel changes reach the x axis."),
            ("max_average", "largest average count", "int", "", 1, "The avg box on the instruments tab.")]
    for key, label, kind, unit, lo, help_ in live:
        out.append(Setting(f"live.{key}", "live", "all live views", label, kind, unit,
                           default=LIVE_DEFAULTS[key], min=lo, help=help_))
    for name, icfg in cfg.get("instruments", {}).items():
        if has_live_view(icfg):
            out.append(Setting(f"live.{name}.max_fps", "live", name, "frame rate cap", unit="fps",
                               default=MAX_FPS_DEFAULT, min=0.1, help="The instrument may be slower."))

    exp = [("max_points", "points per sweep", 2, "Also the most VNA points per trace."),
           ("max_total_points", "points per run (all passes)", 1, ""),
           ("big_file_mb", "warn above data file size", 1, "")]
    for key, label, lo, help_ in exp:
        out.append(Setting(f"experiments.{key}", "experiments", "all experiments", label,
                           "number" if key == "big_file_mb" else "int", "MB" if key == "big_file_mb" else "",
                           default=CAPS[key], min=lo, help=help_))
    out.append(Setting("off_lock_timeout_s", "safety", "outputs off", "wait for a busy instrument",
                       unit="s", default=3.0, min=0,
                       help="Then its outputs are turned off anyway, without its lock."))
    return out


# ---- nested dict helpers ----
def get_path(d: dict, path, default=_MISSING):
    for key in path:
        if not isinstance(d, dict) or key not in d:
            return default
        d = d[key]
    return d


def set_path(d: dict, path, value):
    for key in path[:-1]:
        if not isinstance(d.get(key), dict):
            d[key] = {}
        d = d[key]
    d[path[-1]] = value


def pop_path(d: dict, path, prune: bool = True):
    """Remove path, and (prune) the dicts it leaves empty."""
    parents = []
    for key in path[:-1]:
        if not isinstance(d.get(key), dict):
            return
        parents.append((d, key))
        d = d[key]
    d.pop(path[-1], None)
    for parent, key in reversed(parents if prune else []):
        if parent[key]:
            break
        del parent[key]


class Settings:
    def __init__(self, cfg: dict, path: str | Path | None = None):
        """Applies settings.local.yaml to cfg (in place), so build this before anything
        that reads cfg."""
        self.cfg = cfg
        path = path or cfg.get("settings_file")
        self.path = Path(path) if path else None
        self.base = copy.deepcopy(cfg)          # config.yaml, for "back to config"
        self.settings = {s.id: s for s in schema(cfg)}
        self._lock = threading.Lock()
        self._listeners = []
        self.overrides = self._read()
        for sid, s in self.settings.items():
            v = get_path(self.overrides, s.path)
            if v is _MISSING:
                continue
            try:
                set_path(cfg, s.path, s.parse(v))
            except SettingsError as e:
                log.error("%s: %s: %s (ignored)", self.path, sid, e)
        cfg["experiment_defaults"] = copy.deepcopy(self.overrides.get("experiment_defaults") or {})

    def on_change(self, fn):
        """fn(ids) after every change, to push the new values to what is running."""
        self._listeners.append(fn)

    # ---- reading ----
    def value(self, sid: str):
        s = self.settings[sid]
        return get_path(self.cfg, s.path, s.default)

    def config_value(self, sid: str):
        s = self.settings[sid]
        return get_path(self.base, s.path, s.default)

    def describe(self) -> dict:
        items = [{**asdict(s), "value": self.value(sid), "config": self.config_value(sid),
                  "overridden": get_path(self.overrides, s.path) is not _MISSING}
                 for sid, s in self.settings.items()]
        return {"file": str(self.path) if self.path else None, "sections": SECTIONS, "items": items,
                "experiment_defaults": {k: sorted(v) for k, v in (self.overrides.get("experiment_defaults") or {}).items()}}

    # ---- changing ----
    def update(self, changes: dict | None = None, reset=(), confirm: bool = False) -> list[str]:
        """Apply {id: value} and put the ids in `reset` back to config.yaml. All or
        nothing: any invalid value, or a loosening without confirm, changes nothing.
        Returns the ids whose value changed."""
        changes = changes or {}
        with self._lock:
            new = {}
            for sid, value in changes.items():
                new[sid] = self._setting(sid).parse(value)
            for sid in reset:
                self._setting(sid)
                new[sid] = self.config_value(sid)
            loosened = [t for sid, v in new.items() if (t := self.settings[sid].loosens(self.value(sid), v))]
            if loosened and not confirm:
                raise ConfirmNeeded(loosened)
            changed = []
            for sid, v in new.items():
                s, old = self.settings[sid], self.value(sid)
                if get_path(self.base, s.path) is _MISSING and v == s.default:
                    pop_path(self.cfg, s.path, prune=False)     # e.g. a limit config.yaml doesn't have
                else:
                    set_path(self.cfg, s.path, v)
                if v == self.config_value(sid):
                    pop_path(self.overrides, s.path)
                else:
                    set_path(self.overrides, s.path, v)
                if v != old:
                    changed.append(sid)
                    loose = s.loosens(old, v)
                    (log.warning if loose else log.info)("setting %s: %s -> %s%s", sid, s.text(old), s.text(v),
                                                         " (loosened, confirmed)" if loose else "")
            self._write()
        if changed:
            for fn in self._listeners:
                fn(changed)
        return changed

    def set_experiment_defaults(self, exp_id: str, values: dict | None):
        """Saved defaults for one experiment (None: back to the built-in ones)."""
        with self._lock:
            saved = self.overrides.setdefault("experiment_defaults", {})
            if values:
                saved[exp_id] = values
            else:
                saved.pop(exp_id, None)
            if not saved:
                del self.overrides["experiment_defaults"]
            self.cfg["experiment_defaults"] = copy.deepcopy(self.overrides.get("experiment_defaults") or {})
            self._write()
        log.info("experiment defaults for %s %s", exp_id, "saved" if values else "reset")

    def _setting(self, sid: str) -> Setting:
        if sid not in self.settings:
            raise SettingsError(f"unknown setting {sid!r}")
        return self.settings[sid]

    # ---- file ----
    def _read(self) -> dict:
        if self.path is None or not self.path.exists():
            return {}
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                raise ValueError("not a mapping")
            return data
        except Exception as e:
            # keep the broken file for a look, start from config.yaml alone
            aside = self.path.with_name(f"{self.path.name}.broken-{datetime.now():%Y%m%d-%H%M%S}")
            os.replace(self.path, aside)
            log.error("%s could not be read (%s); moved to %s, using config.yaml alone", self.path, e, aside)
            return {}

    def _write(self):
        if self.path is None:
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        body = yaml.safe_dump(self.overrides, sort_keys=False, default_flow_style=None,
                              allow_unicode=True) if self.overrides else ""
        tmp.write_text(HEADER + body, encoding="utf-8")
        os.replace(tmp, self.path)
