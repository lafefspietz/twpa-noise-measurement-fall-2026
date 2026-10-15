"""Running average of live traces, like a front panel's trace averaging.

The mean is taken over the last `count` traces (fewer right after a restart), in
linear units:
- complex (VNA S21): vector mean, so noise averages down and the trace stays a trace
- dB units (SA dBm, dBmV, ...): mean power, 10**(y/10), then back to dB
  (the SA's own :AVER:TYPE POW does the same)
- W: plain mean; V: rms
It restarts by itself when the x axis, the unit or the trace meta (RBW, VNA power,
IFBW, ...) changes, so a setting change never gets mixed into the old average.
"""
from __future__ import annotations

import collections
import dataclasses

import numpy as np

from .drivers import Trace

MAX_COUNT = 10000      # default cap; the live view's comes from the settings (live.max_average)


def _to_linear(y: np.ndarray, unit: str) -> np.ndarray:
    if np.iscomplexobj(y):
        return y
    u = unit.upper()
    if u.startswith("DB"):
        return 10 ** (y / 10)
    if u == "V":
        return y ** 2
    return y


def _from_linear(y: np.ndarray, unit: str) -> np.ndarray:
    if np.iscomplexobj(y):
        return y
    u = unit.upper()
    if u.startswith("DB"):
        return 10 * np.log10(np.maximum(y, 1e-300))
    if u == "V":
        return np.sqrt(y)
    return y


class TraceAverager:
    def __init__(self, count: int = 1):
        self.count = 1
        self._traces: collections.deque = collections.deque()
        self._sum = None
        self._key = None
        self.set_count(count)

    @property
    def n(self) -> int:
        """Traces in the current average."""
        return len(self._traces)

    def set_count(self, count: int, max_count: int = MAX_COUNT):
        count = int(count)
        if not 1 <= count <= max_count:
            raise ValueError(f"average count must be 1..{max_count}, got {count}")
        self.count = count
        while len(self._traces) > count:
            self._sum -= self._traces.popleft()

    def restart(self):
        self._traces.clear()
        self._sum = None
        self._key = None

    @staticmethod
    def _key_of(tr: Trace):
        meta = tuple(sorted((k, repr(v)) for k, v in tr.meta.items() if k != "averages"))
        return (len(tr.x), float(tr.x[0]), float(tr.x[-1]), tr.x_unit, tr.y_unit, np.iscomplexobj(tr.y), meta)

    def add(self, tr: Trace) -> Trace:
        """Add a trace; returns the averaged trace (meta["averages"] = traces in it)."""
        if self.count == 1:
            self.restart()
            return dataclasses.replace(tr, meta={**tr.meta, "averages": 1})
        key = self._key_of(tr)
        if key != self._key:
            self.restart()
            self._key = key
        lin = _to_linear(np.asarray(tr.y), tr.y_unit)
        if self._sum is None:
            self._sum = np.zeros_like(lin, dtype=complex if np.iscomplexobj(lin) else float)
        self._traces.append(lin)
        self._sum += lin
        if len(self._traces) > self.count:
            self._sum -= self._traces.popleft()
        y = _from_linear(self._sum / len(self._traces), tr.y_unit)
        return dataclasses.replace(tr, y=y, meta={**tr.meta, "averages": len(self._traces)})
