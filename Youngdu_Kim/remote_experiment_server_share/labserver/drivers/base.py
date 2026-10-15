from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import numpy as np

from ..transport import Transport, TransportError, VisaTransport, find_resource

log = logging.getLogger("labserver.drivers")


class InstrumentError(Exception):
    """The instrument reported an error, or a request was rejected (e.g. a limit)."""


class MeasurementError(Exception):
    """A measurement could not be completed after retries."""


@dataclass
class Trace:
    x: np.ndarray
    y: np.ndarray
    x_unit: str
    y_unit: str
    timestamp: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        if np.iscomplexobj(self.y):
            y = {"y_real": self.y.real.tolist(), "y_imag": self.y.imag.tolist()}
        else:
            y = {"y": self.y.tolist()}
        return {"x": self.x.tolist(), **y, "x_unit": self.x_unit, "y_unit": self.y_unit,
                "timestamp": self.timestamp, **self.meta}


def fmt(value: float) -> str:
    """Number formatting for SCPI arguments."""
    return f"{float(value):.12g}"


def within(lim, value) -> bool:
    """value inside [lo, hi]; no limit, or a None end, is no bound."""
    if not lim:
        return True
    lo, hi = lim
    return (lo is None or lo <= value) and (hi is None or value <= hi)


def limit_text(lim) -> str:
    lo, hi = lim
    return f"[{'-inf' if lo is None else f'{lo:g}'}, {'inf' if hi is None else f'{hi:g}'}]"


class Instrument:
    """Base class for all drivers.

    SETTERS maps public parameter names (also the keys of get_state()) to setter
    method names. apply(**params) is what the API and UI call; it checks limits
    from config and reads the instrument error queue afterwards.

    LIMITS lists the limits the settings tab offers (key, label, unit, UI scale); a limit
    is [min, max] in cfg['limits'], either end None for no bound. SAFETY lists on/off
    safety switches shown next to them (on = less safe). TUNABLES lists the
    other settings it offers (see settings.py); reconfigure() reads them from cfg again,
    so a change in the settings tab applies without reconnecting.
    """
    kind = "instrument"
    SETTERS: dict[str, str] = {}
    SIM: type | None = None          # simulated transport class
    TIMEOUT_MS = 5000                # I/O timeout when cfg has no timeout_ms
    LIMITS: list[tuple[str, str, str, float]] = []
    SAFETY: list[dict] = []          # switches shown with the limits; turning one on needs a confirmation
    TUNABLES: list[dict] = [
        {"key": "timeout_ms", "label": "I/O timeout", "kind": "int", "unit": "ms", "min": 100,
         "help": "How long one command may take before it counts as failed."},
    ]

    def __init__(self, name: str, transport: Transport, cfg: dict | None = None):
        self.name = name
        self.t = transport
        self.cfg = cfg if cfg is not None else {}
        self._unsupported: set[str] = set()   # optional queries this instrument didn't answer
        self._pending: dict = {}              # the params of the apply() being checked
        self.last_state: dict | None = None   # see remember_state()
        self.last_state_time: float | None = None
        self.reconfigure()

    @classmethod
    def limit_specs(cls, cfg: dict) -> list[tuple[str, str, str, float]]:
        """LIMITS for this config (e.g. one set per SynthHD channel)."""
        return cls.LIMITS

    @classmethod
    def tunable_specs(cls, cfg: dict) -> list[dict]:
        return cls.TUNABLES

    def reconfigure(self) -> None:
        """Read the settings-tab values from cfg (also called after each change there)."""
        self.limits = self.cfg.setdefault("limits", {})
        if "timeout_ms" in self.cfg and isinstance(self.t, Transport):
            self.t.timeout_ms = self.cfg["timeout_ms"]

    # ---- construction ----
    @classmethod
    def open(cls, name: str, cfg: dict, *, sim: bool, world=None, rm=None) -> "Instrument":
        if sim:
            if cls.SIM is None:
                raise InstrumentError(f"{cls.__name__} has no simulator")
            transport = cls.SIM(world)
        else:
            transport = cls.open_transport(cfg, rm)
        inst = cls(name, transport, cfg)
        inst.initialize()
        return inst

    @classmethod
    def open_transport(cls, cfg: dict, rm) -> Transport:
        """Default: VISA resource whose name contains cfg['match']."""
        resource = find_resource(rm.list_resources(), cfg["match"])
        return VisaTransport.open(rm, resource, timeout_ms=cfg.get("timeout_ms", cls.TIMEOUT_MS))

    def initialize(self) -> None:
        """Called once after connecting. Must not change any output state.
        Runs cfg['init_commands'] (raw writes) first. Errors left in the queue from before
        (an earlier session, a cut-off query) are logged and dropped."""
        self.discard_old_errors()
        for cmd in self.cfg.get("init_commands", []):
            self.t.write(cmd)

    # ---- generic interface ----
    def idn(self) -> str:
        return self.t.query("*IDN?").strip()

    def get_state(self) -> dict:
        raise NotImplementedError

    def remember_state(self, state: dict) -> dict:
        """Keep the last full get_state() result, so a snapshot taken while the
        instrument is busy (a live sweep longer than the snapshot wait) can still save
        its settings. Drivers with a live view call this at the end of get_state()."""
        self.last_state, self.last_state_time = dict(state), time.time()
        return state

    def settable(self) -> list[str]:
        return list(self.SETTERS)

    def _set_param(self, key: str, value) -> None:
        getattr(self, self.SETTERS[key])(value)

    def apply(self, **params) -> dict:
        unknown = set(params) - set(self.settable())
        if unknown:
            raise InstrumentError(f"{self.name}: unknown parameter(s) {sorted(unknown)}; "
                                  f"settable: {sorted(self.settable())}")
        self._pending = params          # check_limit() may depend on another param (e.g. the unit)
        try:
            for key, value in params.items():
                self.check_limit(key, value)
            self.discard_old_errors()       # so the check below reports only this apply()'s errors
            for key, value in params.items():
                self._set_param(key, value)
        finally:
            self._pending = {}
        errors = self.errors()
        if errors:
            raise InstrumentError(f"{self.name}: instrument reported {errors}")
        return self.get_state()

    def check_limit(self, key: str, value) -> None:
        if key in self.limits and value is not None:
            self.check_range(key, float(value))

    def check_range(self, key: str, value: float, what: str | None = None) -> None:
        """value against limits[key], if there is one."""
        lim = self.limits.get(key)
        if not within(lim, value):
            raise InstrumentError(f"{self.name}.{what or f'{key}={value:g}'} outside limit {limit_text(lim)}")

    def errors(self, max_reads: int = 100) -> list[str]:
        """Drain the SCPI error queue."""
        out = []
        for _ in range(max_reads):
            resp = self.t.query(":SYST:ERR?").strip()
            if resp.startswith(("+0", "0")):
                break
            out.append(resp)
        return out

    def discard_old_errors(self) -> None:
        """Drain the error queue, logging what was in it (e.g. -410 "Query interrupted"
        left by a timed-out read)."""
        old = self.errors()
        if old:
            log.warning("%s: discarded old errors from the queue: %s", self.name, old)

    def query_optional(self, cmd: str, default: str) -> str:
        """Query a command the instrument may not support. If it doesn't answer, clear
        it, drain the error queue (so the next apply() doesn't fail on it) and return
        default; the command is not sent again, so an unsupported query costs one
        timeout, not one per get_state()."""
        if cmd in self._unsupported:
            return default
        try:
            return self.t.query(cmd).strip()
        except TransportError as e:
            log.warning("%s: %s not answered, using %r from now on: %s", self.name, cmd, default, e)
            self._unsupported.add(cmd)
            self._recover()
            return default

    def forget_cached_state(self) -> None:
        """Drop cached settings so the next read asks the instrument again. Called after
        raw commands and periodically by the live view (front-panel changes)."""

    def idle(self) -> None:
        """Called when a live view stops reading: hand the front panel back."""

    def safe_off(self) -> None:
        """Turn off every output this instrument drives. Called on server start/stop."""

    def close(self) -> None:
        self.t.close()

    def retry(self, fn, *, what: str, max_retries: int, delay_s: float):
        """Run fn(), clearing the device and retrying on TransportError/MeasurementError."""
        for attempt in range(1, max_retries + 1):
            try:
                return fn()
            except (TransportError, MeasurementError) as e:
                log.warning("%s: %s failed (attempt %d/%d): %s", self.name, what, attempt, max_retries, e)
                self._recover()
                if delay_s:
                    time.sleep(delay_s)
        raise MeasurementError(f"{self.name}: {what} failed after {max_retries} attempts")

    def _recover(self) -> None:
        """After a failed exchange: device clear, then drain the error queue. The two
        are tried separately: pyvisa-py USB has no clear() (VI_ERROR_NSUP_OPER), and
        skipping the drain with it let errors from failed reads pile up until the next
        apply() blamed them on its own commands."""
        try:
            self.t.clear()
        except TransportError:
            pass
        try:
            self.discard_old_errors()
        except TransportError:
            pass
