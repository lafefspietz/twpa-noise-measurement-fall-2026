"""Owns every instrument connection for the server process.

- connect_all(): opens what it can; one dead instrument doesn't stop the others.
  Connecting never changes outputs (the state is read and taken over as is).
- use(name, owner): per-instrument lock, so a live view, a job and a UI click never
  talk to the same instrument at once. Who holds it is visible in status().
- outputs_off(): turns every output off; the server keeps running. If an instrument
  stays locked longer than `off_lock_timeout_s`, its safe_off() is sent anyway. A
  running job stops at its next check, without its "when done" step. `halt` stays set,
  and commands are refused, until end_halt(): the job runner calls it when that job
  has ended (the server right away if none is running).
- claim(names, owner): reserve instruments for a whole job. Others (UI, live view)
  get InstrumentBusy until the job ends, even between the job's own commands.
- shutdown(): close every connection; outputs stay as they are.
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time
from dataclasses import dataclass, field

from .drivers import DRIVERS, Instrument
from .sim import SimWorld
from .transport import TransportError

log = logging.getLogger("labserver.manager")


class InstrumentBusy(Exception):
    pass


class InstrumentUnavailable(Exception):
    pass


class OutputsOff(Exception):
    """Raised by check_halt() while the outputs are being turned off."""


@dataclass
class Slot:
    name: str
    cfg: dict
    inst: Instrument | None = None
    error: str | None = None          # why it is not connected / last I/O failure
    error_time: float | None = None
    idn: str | None = None
    owner: str | None = None          # who holds the lock right now
    claimed_by: str | None = None     # job that reserved this instrument
    waiters: int = 0                  # threads waiting in use(); live views back off when > 0
    lock: threading.RLock = field(default_factory=threading.RLock)


class InstrumentManager:
    def __init__(self, cfg: dict, *, sim: bool = False, world: SimWorld | None = None):
        self.cfg = cfg
        self.sim = sim
        self.world = world or (SimWorld.from_config(cfg.get("sim", {})) if sim else None)
        self.slots = {name: Slot(name, c) for name, c in cfg["instruments"].items()}
        self.halt = threading.Event()   # set by outputs_off() until end_halt(), and by shutdown()
        self.off_lock_timeout_s = cfg.get("off_lock_timeout_s", 3.0)
        self._rm = None
        self._counter_lock = threading.Lock()
        self._closed = False

    # ---- connection ----
    def _resource_manager(self):
        if self.sim:
            return None
        if self._rm is None:
            import pyvisa

            from . import usbtmc_patch
            usbtmc_patch.apply()    # pyvisa-py USB reads over 1 kB hang without it
            self._rm = pyvisa.ResourceManager(self.cfg.get("visa_library", ""))
        return self._rm

    def connect(self, name: str) -> bool:
        slot = self.slots[name]
        with slot.lock:
            if slot.inst is not None:
                self._close_quietly(slot)
            try:
                driver = DRIVERS[slot.cfg["driver"]]
                slot.inst = driver.open(name, slot.cfg, sim=self.sim, world=self.world,
                                        rm=self._resource_manager())
                slot.idn = slot.inst.idn()
                slot.error = slot.error_time = None
                log.info("%s connected: %s", name, slot.idn)
                return True
            except Exception as e:
                slot.inst = None
                slot.error, slot.error_time = f"{type(e).__name__}: {e}", time.time()
                log.error("%s: connect failed: %s", name, slot.error)
                return False

    def connect_all(self) -> dict[str, bool]:
        return {name: self.connect(name) for name in self.slots}

    def reconfigure(self, name: str) -> None:
        """Have a connected instrument read its settings from cfg again (after a change in
        the settings tab). Waits briefly for its lock, so a read in progress keeps the
        timeout it started with; if the lock stays busy it is done anyway (the values are
        plain attributes) and a raised read timeout may put the old I/O timeout back."""
        slot = self.slots[name]
        got_lock = slot.lock.acquire(timeout=2.0)
        try:
            if slot.inst is not None:
                slot.inst.reconfigure()
        finally:
            if got_lock:
                slot.lock.release()

    @staticmethod
    def _close_quietly(slot: Slot):
        try:
            slot.inst.close()
        except Exception as e:
            log.warning("%s: close failed: %s", slot.name, e)
        slot.inst = None

    # ---- access ----
    @contextlib.contextmanager
    def use(self, name: str, owner: str = "api", timeout: float = 10.0):
        """with mgr.use("spa", owner="live:spa") as spa: ..."""
        slot = self.slots.get(name)
        if slot is None:
            raise InstrumentUnavailable(f"unknown instrument {name!r}")
        if slot.claimed_by is not None and owner != slot.claimed_by:
            raise InstrumentBusy(f"{name} is reserved by {slot.claimed_by}")
        with self._counter_lock:
            slot.waiters += 1
        try:
            acquired = slot.lock.acquire(timeout=timeout)
        finally:
            with self._counter_lock:
                slot.waiters -= 1
        if not acquired:
            raise InstrumentBusy(f"{name} is busy ({slot.owner})")
        prev_owner = slot.owner
        try:
            if slot.inst is None:
                raise InstrumentUnavailable(f"{name} is not connected ({slot.error})")
            slot.owner = owner
            try:
                yield slot.inst
            except TransportError as e:
                slot.error, slot.error_time = f"TransportError: {e}", time.time()
                raise
        finally:
            slot.owner = prev_owner
            slot.lock.release()

    @contextlib.contextmanager
    def claim(self, names, owner: str):
        """with mgr.claim(["spa", "vna"], owner="job:vna_power_sweep"): ..."""
        names = list(names)
        with self._counter_lock:
            for n in names:
                if n not in self.slots:
                    raise InstrumentUnavailable(f"unknown instrument {n!r}")
                other = self.slots[n].claimed_by
                if other is not None and other != owner:
                    raise InstrumentBusy(f"{n} is reserved by {other}")
            for n in names:
                self.slots[n].claimed_by = owner
        try:
            yield
        finally:
            with self._counter_lock:
                for n in names:
                    if self.slots[n].claimed_by == owner:
                        self.slots[n].claimed_by = None

    def snapshot(self, owner: str = "snapshot", timeout: float = 2.0) -> dict:
        """State of every instrument, for saving next to a measurement. A busy
        instrument (e.g. the VNA in the middle of a 10 s live sweep) gets the settings
        it last reported, marked with "cached": {"age_s", "reason"}; one that never
        reported, or is disconnected, gets an {"error": ...} entry instead of failing."""
        out = {}
        for name, slot in self.slots.items():
            try:
                with self.use(name, owner=slot.claimed_by or owner, timeout=timeout) as inst:
                    out[name] = {"idn": slot.idn, **inst.get_state()}
            except InstrumentBusy as e:
                inst = slot.inst
                if inst is not None and inst.last_state is not None:
                    age = round(time.time() - inst.last_state_time, 1)
                    out[name] = {"idn": slot.idn, **inst.last_state,
                                 "cached": {"age_s": age, "reason": str(e)}}
                else:
                    out[name] = {"error": f"{type(e).__name__}: {e}"}
            except Exception as e:
                out[name] = {"error": f"{type(e).__name__}: {e}"}
        return out

    def get_state(self, name: str, owner: str = "api") -> dict:
        with self.use(name, owner) as inst:
            return inst.get_state()

    def apply(self, name: str, owner: str = "api", **params) -> dict:
        self.check_halt()
        with self.use(name, owner) as inst:
            return inst.apply(**params)

    def status(self) -> dict:
        return {
            name: {
                "connected": s.inst is not None,
                "driver": s.cfg["driver"],
                "kind": s.inst.kind if s.inst else None,
                "idn": s.idn,
                "busy": s.owner,
                "claimed_by": s.claimed_by,
                "error": s.error,
                "error_time": s.error_time,
            }
            for name, s in self.slots.items()
        }

    # ---- outputs off / shutdown ----
    def check_halt(self):
        """Call between steps of anything long-running."""
        if self.halt.is_set():
            raise OutputsOff("the outputs are being turned off; the running experiment is stopping")

    def outputs_off(self) -> dict[str, str]:
        """Turn every output off. Commands are refused until end_halt(), so a running
        job stops at its next check without turning anything back on."""
        log.warning("ALL OUTPUTS OFF")
        self.halt.set()
        return self.all_outputs_off()

    def end_halt(self):
        """Allow commands again after outputs_off() (not after shutdown())."""
        if not self._closed:
            self.halt.clear()

    def all_outputs_off(self, lock_timeout: float | None = None) -> dict[str, str]:
        """safe_off() on every connected instrument, all in parallel, so one busy
        instrument (e.g. the VNA in the middle of a long sweep) doesn't delay the
        others. Each waits up to lock_timeout for its lock, then sends anyway."""
        lock_timeout = self.off_lock_timeout_s if lock_timeout is None else lock_timeout
        results = {}

        def off(name, slot):
            got_lock = slot.lock.acquire(timeout=lock_timeout)
            try:
                slot.inst.safe_off()
                results[name] = "off" if got_lock else f"off (forced, was held by {slot.owner})"
            except Exception as e:
                results[name] = f"FAILED: {type(e).__name__}: {e}"
                log.error("%s: safe_off failed: %s", name, e)
            finally:
                if got_lock:
                    slot.lock.release()

        threads = []
        for name, slot in self.slots.items():
            if slot.inst is None:
                results[name] = "not connected"
                continue
            t = threading.Thread(target=off, args=(name, slot), name=f"safe_off-{name}")
            t.start()
            threads.append(t)
        for t in threads:
            t.join(lock_timeout + 30)
        return {name: results.get(name, "TIMED OUT") for name in self.slots}

    def shutdown(self) -> None:
        """Close every connection. Outputs stay as they are."""
        self._closed = True
        self.halt.set()
        for slot in self.slots.values():
            if slot.inst is None:
                continue
            got_lock = slot.lock.acquire(timeout=self.off_lock_timeout_s)
            try:
                self._close_quietly(slot)
            finally:
                if got_lock:
                    slot.lock.release()
