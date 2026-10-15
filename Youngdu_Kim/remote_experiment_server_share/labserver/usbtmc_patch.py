"""Fixes for pyvisa-py's USBTMC reads with the GpibUsb USB-GPIB adapters on Linux.

The adapter answers a long query in transfers of at most 1012 data bytes (1024 bytes
with the USBTMC header), sets EOM only on the last one, and ends each full-size
transfer with a zero-length packet. Two problems in pyvisa-py 0.8.1, both seen on
2026-10-13 on Ubuntu (libusb); short queries are unaffected:

1. USBTMC.read treats a response that is a whole number of packets as "more of this
   transfer follows" and reads again instead of sending the next
   REQUEST_DEV_DEP_MSG_IN, so every response longer than 1000 bytes (any SPA or VNA
   trace) hangs until the VISA timeout.
2. USBSession.read always reports a complete message (StatusCode.success), even when
   it stopped at the requested size before EOM. RsInstrument (VNA) then never reads
   the newline after a binary block, and the next command fails with "Query
   interrupted"; pyvisa's own read_raw stops after one chunk (20 kB).

The replacements keep reading a transfer only until its transfer_size bytes are in,
keep requesting transfers until EOM or `size` bytes, and report
success_max_count_read when EOM has not been seen. A response shorter than the
USBTMC header (an empty packet after a timeout, 2026-10-14) aborts the transfer and
raises usb.core.USBError instead of struct.error.
"""
from __future__ import annotations

import errno
import logging
import math

log = logging.getLogger(__name__)

USB488_GO_TO_LOCAL = 161

_applied = False


def apply() -> None:
    """Patch pyvisa-py's USBTMC.read and USBSession.read (idempotent; no-op without pyvisa-py)."""
    global _applied
    if _applied:
        return
    try:
        import usb.core
        from pyvisa.constants import ResourceAttribute
        from pyvisa_py import usb as pv_usb
        from pyvisa_py.protocols import usbtmc
    except ImportError:
        return

    def tmc_read(self, size):
        header_size = 12
        packet = self.usb_recv_ep.wMaxPacketSize
        raw_read = super(usbtmc.USBTMC, self).read
        raw_write = super(usbtmc.USBTMC, self).write

        message = bytearray()
        eom = False
        while not eom and len(message) < size:
            want = size - len(message)
            self._btag = (self._btag % 255) + 1
            raw_write(usbtmc.BulkInMessage.build_array(self._btag, want, None))
            try:
                # one packet more than needed, so the device can always end with a short packet
                resp = raw_read((math.floor((want + header_size) / packet) + 1) * packet)
                if len(resp) < header_size:
                    # seen 2026-10-14 right after a timed-out *OPC?: the adapter answered with
                    # an empty packet. from_bytes would raise struct.error, which nothing
                    # above treats as an I/O error; abort the transfer and report it as one
                    raise usb.core.USBError(f"USBTMC bulk-in response of {len(resp)} bytes, "
                                            f"shorter than its {header_size}-byte header")
                response = usbtmc.BulkInMessage.from_bytes(resp)
                transfer = bytearray(response.data)
                while len(transfer) < response.transfer_size:
                    left = response.transfer_size - len(transfer)
                    transfer.extend(raw_read((math.floor(left / packet) + 1) * packet))
                message.extend(transfer[: response.transfer_size])
                eom = bool(response.transfer_attributes & 1)
            except (usb.core.USBError, ValueError):
                self._abort_bulk_in(self._btag)
                raise
        self.last_eom = eom
        return bytes(message)

    def session_read(self, count):
        def reader():
            try:
                return self.interface.read(count)
            except usb.USBError as exc:
                if exc.errno in (errno.ETIMEDOUT, -errno.ETIMEDOUT):
                    raise pv_usb.USBTimeoutException()
                raise

        suppress_end_en, _ = self.get_attribute(ResourceAttribute.suppress_end_enabled)
        term_char, _ = self.get_attribute(ResourceAttribute.termchar)
        term_char_en, _ = self.get_attribute(ResourceAttribute.termchar_enabled)
        return self._read(
            reader,
            count,
            # USBRaw interfaces have no EOM: every read is a complete message, as before
            lambda current: getattr(self.interface, "last_eom", True),
            suppress_end_en,
            term_char,
            term_char_en,
            pv_usb.USBTimeoutException,
        )

    usbtmc.USBTMC.read = tmc_read
    pv_usb.USBSession.read = session_read
    _applied = True
    log.debug("patched pyvisa_py USBTMC.read and USBSession.read")


def usb488_go_to_local(resource) -> bool:
    """Send the USB488 GO_TO_LOCAL request (GPIB GTL) to a pyvisa-py USB INSTR resource.

    pyvisa-py has no control_ren for USB, and RsInstrument without VXI falls back to
    writing "&GTL", which an instrument behind a USB-GPIB adapter rejects as
    -113 "Undefined header". REN stays asserted, so the next command takes the
    instrument back to remote. Returns False if the resource is not such a session or
    the adapter lacks REN control; the caller then uses its own fallback.
    """
    try:
        import usb.util
        interface = resource.visalib.sessions[resource.session].interface
        if not interface._capabilities.ren_control:
            return False
        request_type = usb.util.build_request_type(
            usb.util.CTRL_IN, usb.util.CTRL_TYPE_CLASS, usb.util.CTRL_RECIPIENT_INTERFACE)
    except (ImportError, AttributeError, KeyError, TypeError):
        return False
    status = interface.usb_dev.ctrl_transfer(
        request_type, USB488_GO_TO_LOCAL, 0, interface.usb_intf.index, 1, timeout=interface.timeout)
    # USBTMC_status 0x01 = STATUS_SUCCESS
    return len(status) >= 1 and status[0] == 1

