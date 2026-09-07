"""Read-only ULX receiver battery queries and wired USB presence detection."""

from dataclasses import dataclass
import math
import threading
import time


VENDOR_ID = 13853
PRODUCT_ID = 256
USAGE_PAGE = 65280
USAGE = 1
INTERFACE_NUMBER = 0
WIRED_PRODUCT_ID = 258
WIRED_INTERFACE_NUMBER = 1
REPORT_BYTES = 64
OUTPUT_REPORT_ID = 4
INPUT_REPORT_ID = 5
QUERY_TIMEOUT_MS = 350
MAX_REPORT_SCANS = 64
_LINK_COMMAND = 36
_CHARGING_COMMAND = 37
_VOLTAGE_COMMAND = 5
_STATUS_PAYLOAD_BYTES = {_LINK_COMMAND: 1, _CHARGING_COMMAND: 1, _VOLTAGE_COMMAND: 2}


class BatteryReadError(RuntimeError):
    """A fresh, unambiguous receiver status could not be read."""


class DeviceUnavailable(BatteryReadError):
    """Neither verified ULX receiver nor wired interface is available."""


@dataclass(frozen=True, slots=True)
class BatteryReading:
    percent: int | None
    charging: bool | None
    connected: bool
    millivolts: int | None
    power_connected: bool = False


def _voltage_to_percent(millivolts):
    """Mirror Xpanel's volts-based interpolation and positive Math.round."""
    voltage = millivolts / 1000.0
    voltages = (3.0, 3.62, 3.66, 3.74, 3.88, 4.17, 4.38)
    percentages = (0.2, 5, 10, 25, 50, 75, 100)
    if voltage < voltages[0]:
        return 0
    if voltage > voltages[-1]:
        return 100
    percentage = 100.0
    for index in range(len(voltages) - 1):
        low, high = voltages[index:index + 2]
        if low <= voltage <= high:
            fraction = min(1.0, max(0.0, (voltage - low) / (high - low)))
            percentage = percentages[index] + (
                percentages[index + 1] - percentages[index]
            ) * fraction
            break
    integer = math.floor(percentage)
    return min(100, max(0, integer + int(percentage - integer >= 0.5)))


class NativeBatteryReader:
    """Keep one HID handle, with no polling thread, helper or cached readings.

    Each query shares a 350 ms deadline and 64-read cap between queue draining
    and response scanning. HID enumeration/open/write/close remain synchronous
    operating-system calls. A failed read closes the handle; the next read opens
    it again. Call close() when the owning tray application stops.
    """

    def __init__(self, *, hid_module=None, clock=time.monotonic):
        self._hid = hid_module
        self._clock = clock
        self._lock = threading.Lock()
        self._device = None
        self._path = None

    def close(self):
        """Release the handle; a later read may open a fresh one."""
        with self._lock:
            self._close_device()

    def _close_device(self):
        device, self._device = self._device, None
        self._path = None
        if device is not None:
            try:
                device.close()
            except Exception:
                pass

    def _ensure_device(self):
        if self._hid is None:
            try:
                import hid
            except ImportError:
                raise BatteryReadError("HID backend is unavailable") from None
            self._hid = hid

        selected = {"receiver": None, "wired": None}
        for descriptor in self._hid.enumerate(VENDOR_ID, 0):
            if not isinstance(descriptor, dict):
                raise BatteryReadError("Device enumeration returned invalid data")
            if any(descriptor.get(key) != value for key, value in (
                ("vendor_id", VENDOR_ID),
                ("usage_page", USAGE_PAGE), ("usage", USAGE),
            )):
                continue
            identity = (descriptor.get("product_id"), descriptor.get("interface_number"))
            if identity == (PRODUCT_ID, INTERFACE_NUMBER):
                mode = "receiver"
            elif identity == (WIRED_PRODUCT_ID, WIRED_INTERFACE_NUMBER):
                mode = "wired"
            else:
                continue
            candidate = descriptor.get("path")
            if not isinstance(candidate, bytes) or not candidate:
                raise BatteryReadError("Device interface is incomplete")
            if selected[mode] is not None:
                raise BatteryReadError("More than one matching device interface is available")
            selected[mode] = candidate
        if selected["wired"] is not None:
            self._close_device()
            return "wired"
        receiver_path = selected["receiver"]
        if receiver_path is None:
            raise DeviceUnavailable("ULX receiver and wired mouse are unavailable")
        if self._device is not None and self._path == receiver_path:
            return "receiver"
        self._close_device()
        self._device = self._hid.device()
        self._device.open_path(receiver_path)
        self._path = receiver_path
        return "receiver"

    def _drain_reports(self, deadline):
        # There is no transaction ID. Discard already queued reports before
        # sending a query so an old reply cannot satisfy the new request.
        self._device.set_nonblocking(1)
        try:
            for scanned in range(1, MAX_REPORT_SCANS + 1):
                if self._clock() >= deadline:
                    raise BatteryReadError("Receiver status query timed out")
                if not self._device.read(REPORT_BYTES):
                    return scanned
            raise BatteryReadError("Receiver report queue did not settle")
        finally:
            self._device.set_nonblocking(0)

    def _query(self, command):
        if command not in _STATUS_PAYLOAD_BYTES:
            raise BatteryReadError("Unsupported receiver status query")
        deadline = self._clock() + QUERY_TIMEOUT_MS / 1000.0
        drained = self._drain_reports(deadline)
        if drained >= MAX_REPORT_SCANS:
            raise BatteryReadError("Receiver report scan limit reached")
        if self._clock() >= deadline:
            raise BatteryReadError("Receiver status query timed out")
        request = bytes((OUTPUT_REPORT_ID, 2, 0x80 | command, 0)) + bytes(60)
        if self._device.write(request) != REPORT_BYTES:
            raise BatteryReadError("Receiver status request was incomplete")
        for _ in range(MAX_REPORT_SCANS - drained):
            remaining_ms = math.ceil((deadline - self._clock()) * 1000)
            if remaining_ms <= 0:
                raise BatteryReadError("Receiver status query timed out")
            report = self._device.read(REPORT_BYTES, min(remaining_ms, QUERY_TIMEOUT_MS))
            if not report:
                continue
            if not isinstance(report, (bytes, bytearray, list, tuple)):
                raise BatteryReadError("Receiver returned a malformed report")
            if not 4 <= len(report) <= REPORT_BYTES or any(
                not isinstance(value, int) or not 0 <= value <= 255 for value in report
            ):
                raise BatteryReadError("Receiver returned a malformed report")
            if report[0] != INPUT_REPORT_ID or report[2] & 0x7F != command:
                continue
            size = report[3]
            if size > 60 or len(report) < 4 + size or size < _STATUS_PAYLOAD_BYTES[command]:
                raise BatteryReadError("Receiver returned an incomplete status response")
            return report[4:4 + size]
        raise BatteryReadError("Receiver report scan limit reached")

    def read(self):
        """Return only freshly queried status, or raise without retaining values."""
        with self._lock:
            try:
                if self._ensure_device() == "wired":
                    # USB presence is confirmed; charging current is unobserved.
                    return BatteryReading(None, None, True, None, power_connected=True)
                linked = self._query(_LINK_COMMAND)[0]
                if linked not in (0, 1):
                    raise BatteryReadError("Receiver returned an unknown link state")
                charging = self._query(_CHARGING_COMMAND)[0]
                if charging not in (0, 1):
                    raise BatteryReadError("Receiver returned an unknown charging state")
                if charging == 1:
                    return BatteryReading(None, True, linked == 1, None)
                if linked == 0:
                    return BatteryReading(None, charging == 1, False, None)
                voltage = self._query(_VOLTAGE_COMMAND)
                millivolts = voltage[0] | voltage[1] << 8
                return BatteryReading(
                    _voltage_to_percent(millivolts), charging == 1, True, millivolts,
                )
            except BatteryReadError:
                self._close_device()
                raise
            except Exception:
                self._close_device()
                # HID exceptions can contain receiver paths or serial numbers.
                raise BatteryReadError("Receiver status read failed") from None
