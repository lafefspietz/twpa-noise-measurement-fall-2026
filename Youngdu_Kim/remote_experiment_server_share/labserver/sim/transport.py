from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import numpy as np

from ..transport import Transport, TransportError
from .scpi import compile_pattern, matches, split_command
from .world import SimWorld

log = logging.getLogger("labserver.sim")


def scpi(pattern: str):
    """Register a SimTransport method as the handler for a SCPI header pattern.

    Handler signature: (self, is_query: bool, args: str) -> str | SimBlock | None
    """
    def deco(fn):
        fn._scpi_nodes = compile_pattern(pattern)
        return fn
    return deco


@dataclass
class SimBlock:
    """Trace data as the instrument would send it, in its current FORMat."""
    values: np.ndarray
    binary: bool


class SimTransport(Transport):
    """Base for simulated SCPI instruments.

    Subclasses define handlers with @scpi. Unknown headers go to the error queue like
    on real hardware; an unknown *query* also raises TransportError, because a real
    instrument would never answer and the read would time out.
    """
    IDN = "SIM,INSTRUMENT,0,0"
    io_latency_s = 0.002

    def __init__(self, world: SimWorld | None = None):
        self.world = world or SimWorld()
        self.resource_name = f"SIM::{type(self).__name__}"
        self.timeout_ms = 5000
        self._errors: list[str] = []
        self._busy_until = 0.0
        self._handlers = [
            getattr(self, n) for n in dir(type(self))
            if hasattr(getattr(type(self), n), "_scpi_nodes")
        ]

    # ---- helpers for subclasses ----
    def sleep(self, seconds: float) -> None:
        if seconds > 0 and self.world.time_scale > 0:
            time.sleep(seconds * self.world.time_scale)

    def start_operation(self, duration_s: float) -> None:
        """Mark the instrument busy (e.g. a sweep); *OPC? blocks until it finishes."""
        self._busy_until = time.monotonic() + duration_s * self.world.time_scale

    def push_error(self, code: int, msg: str) -> None:
        self._errors.append(f'{code:+d},"{msg}"')

    # ---- dispatch ----
    def _dispatch(self, cmd: str):
        self.sleep(self.io_latency_s)
        tokens, is_query, args = split_command(cmd)
        for h in self._handlers:
            if matches(h._scpi_nodes, tokens):
                try:
                    return h(is_query, args)
                except ValueError as e:
                    self.push_error(-224, f"Illegal parameter value; {e}")
                    if is_query:
                        raise TransportError(f"{cmd!r}: timeout (sim: {e})") from e
                    return None
        self.push_error(-113, f"Undefined header; {cmd}")
        log.warning("%s: undefined header %r", self.resource_name, cmd)
        if is_query:
            raise TransportError(f"{cmd!r}: timeout (sim: undefined header)")
        return None

    def write(self, cmd: str) -> None:
        self._dispatch(cmd)

    def query(self, cmd: str) -> str:
        resp = self._dispatch(cmd)
        if isinstance(resp, SimBlock):
            if resp.binary:
                raise TransportError(f"{cmd!r}: binary block received by a text query")
            return ",".join(f"{v:.6e}" for v in resp.values) + "\n"
        return f"{resp}\n"

    def query_ascii_values(self, cmd: str) -> np.ndarray:
        resp = self._dispatch(cmd)
        if isinstance(resp, SimBlock):
            if resp.binary:
                raise TransportError(f"{cmd!r}: binary block received by an ascii query")
            return np.asarray(resp.values, dtype=float)
        return np.array([float(v) for v in str(resp).split(",")])

    def query_binary_values(self, cmd: str, datatype: str = "f", is_big_endian: bool = True) -> np.ndarray:
        resp = self._dispatch(cmd)
        if not (isinstance(resp, SimBlock) and resp.binary):
            raise TransportError(f"{cmd!r}: expected a binary block (check FORMat)")
        return np.asarray(resp.values, dtype=np.float32).astype(float)

    def clear(self) -> None:
        self._busy_until = 0.0

    # ---- IEEE 488.2 common commands ----
    @scpi("*IDN")
    def _idn(self, q, args):
        return self.IDN

    @scpi("*OPC")
    def _opc(self, q, args):
        remaining = self._busy_until - time.monotonic()
        if remaining * 1000 > self.timeout_ms:
            time.sleep(self.timeout_ms / 1000)
            raise TransportError("*OPC?: timeout")
        if remaining > 0:
            time.sleep(remaining)
        return "1"

    @scpi("*CLS")
    def _cls(self, q, args):
        self._errors.clear()

    @scpi("SYSTem:ERRor[:NEXT]")
    def _err(self, q, args):
        return self._errors.pop(0) if self._errors else '+0,"No error"'
