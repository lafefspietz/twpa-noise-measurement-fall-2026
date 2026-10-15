"""Continuous trace reading for the SA and VNA, like their front-panel continuous mode.

One LiveView per instrument that has read_trace(). It runs a background thread only
while someone is watching (subscribers > 0) and keeps just the latest frame, so slow
clients skip frames instead of falling behind.

The live view always gives way:
- an instrument claimed by a job      -> paused until the claim ends
- someone else waiting in mgr.use()   -> backs off for that call (UI clicks stay snappy)
- outputs off                         -> paused until a running job has stopped
- hold (Run/Hold button)              -> paused, last frame stays on screen
- expected sweep longer than max_sweep_s -> not started: a live read holds the
  instrument for the whole sweep, so a huge sweep (e.g. RBW 1 kHz over the full span,
  or a high average count) would block the very settings change that fixes it
Whenever it stops reading, the instrument's idle() hands the front panel back
(the SA goes back to continuous sweep).

Averaging (set_average) is a running mean of the last N traces, done here, not in the
instrument (see averaging.py): frames keep coming at the sweep rate and the noise
settles as they add up. Saved traces are the averaged ones, with meta["averages"].

A failed live read is retried only max_retries times (not the driver's count, 50 for
the SA): the loop retries anyway, and in between the instrument is free for UI clicks
and for outputs off. Cached settings are re-read every state_refresh_s, so changes
made on the front panel show up on the x axis. The last few traces are kept by seq,
so "save" stores the frame the viewer is looking at, not a newer one.

max_fps, max_sweep_s, max_retries, state_refresh_s and max_average come from the `live`
section of the config; LiveHub.configure() sets them again after a change in the
settings tab, and the running loops pick them up at their next frame.
"""
from __future__ import annotations

import collections
import logging
import threading
import time

import numpy as np

from .averaging import TraceAverager
from .drivers import DRIVERS
from .manager import InstrumentBusy, InstrumentManager

log = logging.getLogger("labserver.live")

IDLE_GRACE_S = 2.0      # keep the thread around briefly after the last viewer leaves
RECENT_TRACES = 64      # traces kept for save-by-seq (a few seconds at the frame-rate caps)
# live config key -> default (live.<instrument>.max_fps is per view)
LIVE_DEFAULTS = {"max_sweep_s": 5.0, "max_retries": 2, "state_refresh_s": 5.0, "max_average": 10000}
MAX_FPS_DEFAULT = 10.0


def frame_from_trace(name: str, tr, seq: int, read_ms: float) -> dict:
    frame = {"type": "frame", "instrument": name, "seq": seq, "timestamp": tr.timestamp,
             "read_ms": round(read_ms, 1), "x": tr.x.tolist(), "x_unit": tr.x_unit,
             "y_unit": tr.y_unit, "meta": tr.meta}
    if np.iscomplexobj(tr.y):
        mag = np.abs(tr.y)
        frame["y_db"] = (20 * np.log10(np.maximum(mag, 1e-30))).tolist()
        frame["y_deg"] = np.degrees(np.angle(tr.y)).tolist()
    else:
        frame["y"] = tr.y.tolist()
    return frame


class LiveView:
    def __init__(self, mgr: InstrumentManager, name: str, max_fps: float = 10.0, max_sweep_s: float = 5.0,
                 max_retries: int = 2, state_refresh_s: float = 5.0, max_average: int = 10000):
        self.mgr = mgr
        self.name = name
        self.max_fps = max_fps
        self.max_sweep_s = max_sweep_s
        self.max_retries = max_retries
        self.state_refresh_s = state_refresh_s
        self.max_average = max_average
        self.trace = None           # latest Trace
        self._recent = collections.deque(maxlen=RECENT_TRACES)   # (seq, Trace), for save
        self.frame = None           # latest frame dict (sent to clients)
        self.seq = 0
        self.status = "idle"
        self.fps = 0.0
        self.subscribers = 0
        self.hold = False
        self.averager = TraceAverager(1)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ---- control ----
    def subscribe(self):
        with self._lock:
            self.subscribers += 1
            if self._thread is None:
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name=f"live-{self.name}", daemon=True)
                self._thread.start()

    def unsubscribe(self):
        with self._lock:
            self.subscribers = max(0, self.subscribers - 1)

    def set_hold(self, hold: bool):
        self.hold = bool(hold)

    def set_average(self, count: int):
        with self._lock:
            self.averager.set_count(count, self.max_average)

    def restart_average(self):
        with self._lock:
            self.averager.restart()

    def trace_for(self, seq: int):
        """The trace sent as frame `seq`, or None if it is no longer kept."""
        with self._lock:
            for s, tr in self._recent:
                if s == seq:
                    return tr
        return None

    def stop(self, timeout: float = 5.0):
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout)

    def info(self) -> dict:
        return {"status": self.status, "fps": round(self.fps, 2), "subscribers": self.subscribers,
                "hold": self.hold, "seq": self.seq, "max_fps": self.max_fps, "max_average": self.max_average,
                "average_count": self.averager.count, "averaged": self.averager.n}

    # ---- loop ----
    def _pause_reason(self) -> str | None:
        slot = self.mgr.slots[self.name]
        if self.subscribers == 0:
            return "idle"
        if self.mgr.halt.is_set():
            return "paused: turning outputs off"
        if self.hold:
            return "hold"
        if slot.inst is None:
            return "paused: not connected"
        if slot.claimed_by is not None:
            return f"paused: in use by {slot.claimed_by}"
        if slot.waiters > 0:
            return "yielding"
        return None

    def _release(self):
        """Hand the front panel back after we stop reading."""
        try:
            with self.mgr.use(self.name, owner=f"live:{self.name}", timeout=1.0) as inst:
                inst.idle()
        except Exception as e:
            log.debug("%s: idle() skipped: %s", self.name, e)

    def _run(self):
        reading = False
        idle_since = None
        state_t = None              # when cached settings were last dropped; None = at the next read
        while not self._stop.is_set():
            reason = self._pause_reason()
            if reason:
                self.status = reason
                self.fps = 0.0
                if reason != "yielding":
                    state_t = None      # settings may change while paused: re-read on resume
                    if reading:
                        self._release()
                        reading = False
                if reason == "idle":
                    idle_since = idle_since or time.monotonic()
                    with self._lock:   # a new subscriber either sees us running or starts a new thread
                        if self.subscribers == 0 and time.monotonic() - idle_since > IDLE_GRACE_S:
                            self._thread = None
                            break
                time.sleep(0.02 if reason == "yielding" else 0.1)
                continue
            idle_since = None

            t0 = time.monotonic()
            try:
                with self.mgr.use(self.name, owner=f"live:{self.name}", timeout=0.2) as inst:
                    if state_t is None or t0 - state_t > self.state_refresh_s:
                        inst.forget_cached_state()      # pick up front-panel changes
                        state_t = t0
                    sweep_s = inst.expected_sweep_s() if hasattr(inst, "expected_sweep_s") else 0.0
                    tr = None if sweep_s > self.max_sweep_s else inst.read_trace(max_retries=self.max_retries)
            except InstrumentBusy:
                time.sleep(0.05)
                continue
            except Exception as e:
                self.status = f"error: {type(e).__name__}: {e}"
                log.warning("%s: live read failed: %s", self.name, e)
                time.sleep(1.0)
                continue
            if tr is None:
                self.status = (f"paused: sweep time {sweep_s:.3g} s > live limit {self.max_sweep_s:g} s "
                               f"(reduce span/points/averages or raise RBW/IFBW)")
                self.fps = 0.0
                time.sleep(0.3)
                continue
            reading = True
            dt = time.monotonic() - t0
            seq = self.seq + 1
            with self._lock:
                tr = self.averager.add(tr)
                self._recent.append((seq, tr))
            frame = frame_from_trace(self.name, tr, seq, dt * 1000)
            self.seq, self.trace, self.frame = seq, tr, frame   # frame last: it is what clients poll
            self.status = "running"

            period = 1.0 / self.max_fps if self.max_fps else 0.0
            if dt < period:
                time.sleep(period - dt)
            total = time.monotonic() - t0
            self.fps = 0.8 * self.fps + 0.2 * (1.0 / total) if self.fps else 1.0 / total
        if reading:
            self._release()
        self.status = "idle"
        self.fps = 0.0


def has_live_view(inst_cfg: dict) -> bool:
    driver = DRIVERS.get(inst_cfg.get("driver"))
    return driver is not None and hasattr(driver, "read_trace")


class LiveHub:
    """One LiveView per instrument that can produce traces."""

    def __init__(self, mgr: InstrumentManager, live_cfg: dict | None = None):
        self.views = {name: LiveView(mgr, name) for name, slot in mgr.slots.items()
                      if has_live_view(slot.cfg)}
        self.configure(live_cfg)

    def configure(self, live_cfg: dict | None):
        """(Re)read the live settings; running views use them from their next frame."""
        live_cfg = live_cfg or {}
        for name, v in self.views.items():
            for key, default in LIVE_DEFAULTS.items():
                setattr(v, key, live_cfg.get(key, default))
            v.max_fps = (live_cfg.get(name) or {}).get("max_fps", MAX_FPS_DEFAULT)
            if v.averager.count > v.max_average:
                v.set_average(v.max_average)

    def info(self) -> dict:
        return {name: v.info() for name, v in self.views.items()}

    def stop_all(self):
        for v in self.views.values():
            v.stop()
