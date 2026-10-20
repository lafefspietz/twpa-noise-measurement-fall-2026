"""Byte-level link to an instrument.

Drivers only talk to a Transport, so the same driver code runs against real VISA
hardware (VisaTransport) or a simulator (labserver.sim.SimTransport).
"""
from __future__ import annotations

import contextlib
import logging
import time

import numpy as np

scpi_log = logging.getLogger("labserver.scpi")


class TransportError(Exception):
    """I/O failure talking to an instrument (timeout, bus error, ...)."""


class Transport:
    resource_name: str = ""
    timeout_ms: float = 5000

    def write(self, cmd: str) -> None:
        raise NotImplementedError

    def query(self, cmd: str) -> str:
        raise NotImplementedError

    def query_ascii_values(self, cmd: str) -> np.ndarray:
        raise NotImplementedError

    def query_binary_values(self, cmd: str, datatype: str = "f", is_big_endian: bool = True) -> np.ndarray:
        raise NotImplementedError

    def clear(self) -> None:
        pass

    def go_to_local(self) -> None:
        """Hand the front panel back to the user (no-op where unsupported)."""

    def close(self) -> None:
        pass

    @contextlib.contextmanager
    def timeout_at_least(self, ms: float):
        """Temporarily raise the I/O timeout, e.g. for a long sweep."""
        old = self.timeout_ms
        self.timeout_ms = max(old, ms)
        try:
            yield
        finally:
            self.timeout_ms = old


def find_resource(resources, match: str) -> str:
    """Pick the single VISA resource whose name contains `match`."""
    hits = [r for r in resources if match in r]
    if len(hits) != 1:
        raise TransportError(
            f"expected exactly one resource matching {match!r}, found {len(hits)}: {list(resources)}"
        )
    return hits[0]


class HttpTransport(Transport):
    """Mini-Circuits Ethernet control: GET http://<ip>/:<CMD> (switches, attenuator).

    Same retry behaviour as send_url_command() in twpa_calibration.ipynb.
    """
    def __init__(self, ip: str, timeout_ms: float = 3000, retries: int = 3):
        self.ip = ip
        self.resource_name = f"http://{ip}"
        self.timeout_ms = timeout_ms
        self.retries = retries

    def get(self, path: str) -> str:
        from urllib.request import urlopen

        url = f"http://{self.ip}/{path}"
        for attempt in range(1, self.retries + 1):
            scpi_log.debug("%s << %s", self.resource_name, path)
            try:
                with urlopen(url, timeout=self.timeout_ms / 1000) as r:
                    resp = r.read().decode("utf-8", errors="replace").strip()
                scpi_log.debug("%s >> %s", self.resource_name, resp[:200])
                return resp
            except OSError as e:
                if attempt == self.retries:
                    raise TransportError(f"{url}: no response ({e})") from e
                time.sleep(0.05)

    def query(self, cmd: str) -> str:
        resp = self.get(f":{cmd}")
        if len(resp) > 100:   # the device answers unknown commands with an HTML page
            raise TransportError(f"{cmd!r}: command not recognised")
        return resp

    def write(self, cmd: str) -> None:
        self.query(cmd)


class RsTransport(Transport):
    """Rohde & Schwarz instruments through the RsInstrument package (ZVA).

    RsInstrument checks the instrument error queue after every command by default, so
    instrument errors also surface here as TransportError.
    """
    def __init__(self, inst):
        from RsInstrument import BinFloatFormat, RsInstrException

        self._inst = inst
        self._err = RsInstrException
        inst.bin_float_numbers_format = BinFloatFormat.Single_4bytes
        self.resource_name = inst.resource_name

    @classmethod
    def open(cls, resource_name: str, timeout_ms: float = 5000, options: str = "") -> "RsTransport":
        from RsInstrument import RsInstrException, RsInstrument

        try:
            inst = RsInstrument(resource_name, id_query=True, reset=False, options=options)
        except RsInstrException as e:
            raise TransportError(f"{resource_name}: {e}") from e
        inst.visa_timeout = timeout_ms
        return cls(inst)

    @property
    def timeout_ms(self) -> float:
        return self._inst.visa_timeout

    @timeout_ms.setter
    def timeout_ms(self, ms: float) -> None:
        self._inst.visa_timeout = int(ms)

    def _call(self, fn, cmd):
        scpi_log.debug("%s << %s", self.resource_name, cmd)
        try:
            result = fn(cmd)
        except (self._err, OSError) as e:   # usb.core.USBError (pyvisa-py) is an OSError
            scpi_log.debug("%s !! %s", self.resource_name, e)
            raise TransportError(f"{cmd!r}: {e}") from e
        if isinstance(result, str):
            scpi_log.debug("%s >> %s", self.resource_name, result.strip()[:200])
        return result

    def write(self, cmd: str) -> None:
        self._call(self._inst.write_str, cmd)

    def query(self, cmd: str) -> str:
        return self._call(self._inst.query_str, cmd)

    def query_ascii_values(self, cmd: str) -> np.ndarray:
        return np.asarray(self._call(self._inst.query_bin_or_ascii_float_list, cmd), dtype=float)

    def query_binary_values(self, cmd: str, datatype: str = "f", is_big_endian: bool = False) -> np.ndarray:
        # RsInstrument decodes the block itself (bin_float_numbers_format, byte order)
        return np.asarray(self._call(self._inst.query_bin_or_ascii_float_list, cmd), dtype=float)

    def clear(self) -> None:
        try:
            self._inst.clear_status()
        except self._err as e:
            raise TransportError(f"clear: {e}") from e

    def go_to_local(self) -> None:
        from . import usbtmc_patch

        try:
            if usbtmc_patch.usb488_go_to_local(self._inst.get_session_handle()):
                return
            self._inst.go_to_local()
        except (self._err, OSError) as e:   # usb.core.USBError is an OSError
            raise TransportError(f"go_to_local: {e}") from e

    def close(self) -> None:
        self._inst.close()


class VisaTransport(Transport):
    def __init__(self, resource):
        import pyvisa

        self._res = resource
        self._visa_error = pyvisa.errors.VisaIOError
        self.resource_name = resource.resource_name

    @classmethod
    def open(cls, rm, resource_name: str, timeout_ms: float = 5000) -> "VisaTransport":
        import pyvisa

        try:
            res = rm.open_resource(resource_name)
        except pyvisa.errors.VisaIOError as e:
            raise TransportError(f"{resource_name}: {e}") from e
        res.timeout = timeout_ms
        return cls(res)

    @property
    def timeout_ms(self) -> float:
        return self._res.timeout

    @timeout_ms.setter
    def timeout_ms(self, ms: float) -> None:
        self._res.timeout = ms

    def _call(self, fn, cmd, *args, **kwargs):
        scpi_log.debug("%s << %s", self.resource_name, cmd)
        try:
            result = fn(cmd, *args, **kwargs)
        except self._visa_error as e:
            scpi_log.debug("%s !! %s", self.resource_name, e)
            raise TransportError(f"{cmd!r}: {e}") from e
        except Exception as e:
            # pyvisa-py turns only timeouts into VisaIOError; other USB failures come through
            # as they are (usb.core.USBError, or struct.error from a garbled USBTMC header,
            # 2026-10-14). They are I/O failures too, so the drivers' retries must see them.
            scpi_log.debug("%s !! %s: %s", self.resource_name, type(e).__name__, e)
            raise TransportError(f"{cmd!r}: {type(e).__name__}: {e}") from e
        if isinstance(result, str):
            scpi_log.debug("%s >> %s", self.resource_name, result.strip()[:200])
        return result

    def write(self, cmd: str) -> None:
        self._call(self._res.write, cmd)

    def query(self, cmd: str) -> str:
        return self._call(self._res.query, cmd)

    def query_ascii_values(self, cmd: str) -> np.ndarray:
        return np.asarray(self._call(self._res.query_ascii_values, cmd), dtype=float)

    def clear(self) -> None:
        # pyvisa-py has no viClear for USB INSTR (VI_ERROR_NSUP_OPER). Don't replace it with
        # USBTMC INITIATE_CLEAR: on 2026-10-13 that request crashed a GpibUsb adapter's
        # firmware (it dropped off the bus until replugged).
        try:
            self._res.clear()
        except self._visa_error as e:
            raise TransportError(f"clear: {e}") from e

    def go_to_local(self) -> None:
        from . import usbtmc_patch

        try:
            usbtmc_patch.usb488_go_to_local(self._res)      # no-op (False) if not a pyvisa-py USB session
        except OSError as e:   # usb.core.USBError
            raise TransportError(f"go_to_local: {e}") from e

    def close(self) -> None:
        self._res.close()
