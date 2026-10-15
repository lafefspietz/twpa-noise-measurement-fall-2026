"""Real-hardware I/O failures must reach the drivers as TransportError, so their retries
handle them (2026-10-14: an empty USBTMC packet after a timeout ended a Y factor run
with struct.error)."""
import struct
from types import SimpleNamespace

import pytest

from labserver import usbtmc_patch
from labserver.transport import TransportError, VisaTransport

usb_core = pytest.importorskip("usb.core")
usbtmc = pytest.importorskip("pyvisa_py.protocols.usbtmc")


class FakeResource:
    resource_name = "USB0::FAKE::INSTR"
    timeout = 1000

    def __init__(self, exc):
        self.exc = exc

    def query(self, cmd):
        raise self.exc


@pytest.mark.parametrize("exc", [
    struct.error("unpack_from requires a buffer of at least 4 bytes"),
    usb_core.USBError("pipe error"),
])
def test_non_visa_errors_become_transport_errors(exc):
    t = VisaTransport(FakeResource(exc))
    with pytest.raises(TransportError, match=type(exc).__name__):
        t.query(":SYST:ERR?")


def bulk_in(btag, data, eom=True):
    """A USBTMC DEV_DEP_MSG_IN transfer: 12-byte header + data."""
    return bytes([2, btag, ~btag & 0xFF, 0]) + struct.pack("<LBxxx", len(data), int(eom)) + data


@pytest.fixture
def tmc(monkeypatch):
    usbtmc_patch.apply()
    replies = []
    monkeypatch.setattr(usbtmc.USBRaw, "write", lambda self, data: len(data))
    monkeypatch.setattr(usbtmc.USBRaw, "read", lambda self, size: replies.pop(0))
    inst = usbtmc.USBTMC.__new__(usbtmc.USBTMC)
    inst.usb_recv_ep = SimpleNamespace(wMaxPacketSize=64)
    inst._btag = 0
    inst.aborted = []
    inst._abort_bulk_in = inst.aborted.append
    return inst, replies


def test_patched_read_returns_the_message(tmc):
    inst, replies = tmc
    replies.append(bulk_in(1, b"+0,\"No error\"\n"))
    assert inst.read(1024) == b"+0,\"No error\"\n" and inst.last_eom is True


def test_patched_read_turns_an_empty_packet_into_a_usb_error(tmc):
    inst, replies = tmc
    replies.append(b"")
    with pytest.raises(usb_core.USBError, match="shorter than its 12-byte header"):
        inst.read(1024)
    assert inst.aborted == [1]          # the transfer was aborted, as for any USB error
