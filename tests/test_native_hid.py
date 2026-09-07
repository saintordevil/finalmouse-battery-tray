"""Offline protocol checks: every HID object here is a pure Python fake."""

from collections import deque
from dataclasses import FrozenInstanceError
import unittest

from native_hid import (
    BatteryReadError, BatteryReading, DeviceUnavailable, NativeBatteryReader,
    MAX_REPORT_SCANS, QUERY_TIMEOUT_MS, _voltage_to_percent,
)


def descriptor(**changes):
    return {
        "vendor_id": 13853, "product_id": 256, "usage_page": 65280,
        "usage": 1, "interface_number": 0, "path": b"fixture-receiver",
        **changes,
    }


def wired_descriptor(**changes):
    return descriptor(**{
        "product_id": 258, "interface_number": 1, "path": b"fixture-wired-mouse",
        **changes,
    })


def report(command, payload, *, high_bit=True, report_id=5):
    return [report_id, 2 + len(payload), command | (128 if high_bit else 0),
            len(payload), *payload]


def status(link=1, charging=0, millivolts=3940):
    return {
        36: [report(36, [link])],
        37: [report(37, [charging])],
        5: [report(5, [millivolts & 255, millivolts >> 8])],
    }


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakeDevice:
    def __init__(self, clock, responses=None, stale=()):
        self.clock = clock
        self.responses = status() if responses is None else responses
        self.queue = deque(stale)
        self.nonblocking = False
        self.modes = []
        self.reads = []
        self.writes = []
        self.opened = []
        self.close_count = 0
        self.write_result = 64
        self.read_error = None
        self.open_error = None
        self.drain_delay = 0.0
        self.guard = lambda: None

    def open_path(self, path):
        self.guard()
        self.opened.append(path)
        if self.open_error:
            raise self.open_error

    def set_nonblocking(self, value):
        self.guard()
        self.nonblocking = bool(value)
        self.modes.append(value)

    def write(self, request):
        self.guard()
        self.writes.append(request)
        command = request[2] & 127
        self.queue.extend(self.responses.get(command, []))
        return self.write_result

    def read(self, length, timeout_ms=0):
        self.guard()
        self.reads.append((length, timeout_ms, self.nonblocking))
        if self.read_error:
            raise self.read_error
        if self.nonblocking:
            self.clock.now += self.drain_delay
        if self.queue:
            return self.queue.popleft()
        if not self.nonblocking:
            self.clock.now += timeout_ms / 1000.0
        return []

    def close(self):
        self.guard()
        self.close_count += 1


class FakeHid:
    def __init__(self, devices, descriptors=None):
        self.devices = deque(devices)
        self.descriptors = [descriptor()] if descriptors is None else descriptors
        self.enumerations = []
        self.device_count = 0
        self.guard = lambda: None

    def enumerate(self, vendor, product):
        self.guard()
        self.enumerations.append((vendor, product))
        return self.descriptors

    def device(self):
        self.guard()
        self.device_count += 1
        return self.devices.popleft()


class NativeBatteryReaderTests(unittest.TestCase):
    def make_reader(self, responses=None, stale=()):
        clock = FakeClock()
        device = FakeDevice(clock, responses, stale)
        backend = FakeHid([device])
        reader = NativeBatteryReader(hid_module=backend, clock=clock)
        self.addCleanup(reader.close)
        return reader, backend, device, clock

    def test_exact_status_queries_and_observed_voltage(self):
        reader, backend, device, _ = self.make_reader()
        self.assertEqual(reader.read(), BatteryReading(55, False, True, 3940))
        self.assertEqual(backend.enumerations, [(13853, 0)])
        self.assertEqual(device.writes, [
            bytes([4, 2, 128 | command, 0]) + bytes(60) for command in (36, 37, 5)
        ])
        self.assertEqual(device.modes, [1, 0] * 3)
        self.assertTrue(all(length == 64 for length, _, _ in device.reads))
        self.assertTrue(all(0 < timeout <= QUERY_TIMEOUT_MS
                            for _, timeout, nonblocking in device.reads if not nonblocking))

    def test_curve_boundaries_clamps_and_official_javascript_rounding(self):
        # Expected values were checked against the staged official JS formula.
        for millivolts, expected in (
            (0, 0), (2999, 0), (3000, 0), (3620, 5), (3640, 8), (3660, 10),
            (3668, 12), (3684, 15), (3700, 18), (3740, 25), (3810, 38),
            (3880, 50), (3940, 55), (4025, 63), (4170, 75), (4380, 100), (65535, 100),
        ):
            with self.subTest(millivolts=millivolts):
                self.assertEqual(_voltage_to_percent(millivolts), expected)

    def test_reading_is_frozen_and_charge_is_explicit(self):
        reader, _, _, _ = self.make_reader(status(charging=1))
        reading = reader.read()
        self.assertIs(reading.charging, True)
        self.assertIs(reading.connected, True)
        with self.assertRaises(FrozenInstanceError):
            reading.percent = 99

    def test_four_positional_field_construction_preserves_receiver_defaults(self):
        reading = BatteryReading(55, False, True, 3940)
        self.assertIs(reading.power_connected, False)
        self.assertIs(reading.charging, False)

    def test_wired_presence_returns_power_without_opening_or_querying_hid(self):
        backend = FakeHid([], [wired_descriptor()])
        reader = NativeBatteryReader(hid_module=backend)
        self.addCleanup(reader.close)
        reading = reader.read()
        self.assertEqual(reading, BatteryReading(None, None, True, None, power_connected=True))
        self.assertIsNone(reading.charging)
        self.assertEqual(backend.enumerations, [(13853, 0)])
        self.assertEqual(backend.device_count, 0)

    def test_unique_wired_interface_takes_priority_over_unique_receiver(self):
        for descriptors in ([descriptor(), wired_descriptor()],
                            [wired_descriptor(), descriptor()]):
            with self.subTest(descriptors=descriptors):
                backend = FakeHid([], descriptors)
                reader = NativeBatteryReader(hid_module=backend)
                self.addCleanup(reader.close)
                self.assertEqual(reader.read(),
                                 BatteryReading(None, None, True, None, power_connected=True))
                self.assertEqual(backend.device_count, 0)

    def test_receiver_wired_receiver_transition_closes_handle_and_reads_fresh_voltage(self):
        reader, backend, first, clock = self.make_reader()
        self.assertEqual(reader.read(), BatteryReading(55, False, True, 3940))
        backend.descriptors = [wired_descriptor()]
        expected_wired = BatteryReading(None, None, True, None, power_connected=True)
        self.assertEqual(reader.read(), expected_wired)
        self.assertEqual(reader.read(), expected_wired)
        self.assertEqual(first.close_count, 1)
        self.assertEqual(len(first.writes), 3)
        self.assertEqual(backend.device_count, 1)
        second = FakeDevice(clock, status(millivolts=3880))
        backend.devices.append(second)
        backend.descriptors = [descriptor()]
        self.assertEqual(reader.read(), BatteryReading(50, False, True, 3880))
        self.assertEqual([request[2] & 127 for request in second.writes], [36, 37, 5])
        self.assertEqual(second.opened, [b"fixture-receiver"])
        self.assertEqual(backend.device_count, 2)
        self.assertEqual(backend.enumerations, [(13853, 0)] * 4)

    def test_wired_wrong_usage_or_interface_never_implies_usb_power(self):
        for entry in (wired_descriptor(usage_page=1), wired_descriptor(usage=2),
                      wired_descriptor(interface_number=0), wired_descriptor(interface_number=2),
                      wired_descriptor(vendor_id=1), wired_descriptor(product_id=259)):
            with self.subTest(entry=entry):
                backend = FakeHid([], [entry])
                reader = NativeBatteryReader(hid_module=backend)
                with self.assertRaises(DeviceUnavailable):
                    reader.read()
                self.assertEqual(backend.device_count, 0)

    def test_multiple_exact_interfaces_remain_ambiguous_even_with_other_mode_present(self):
        for descriptors in (
            [wired_descriptor(), wired_descriptor(path=b"fixture-other-wired")],
            [descriptor(), wired_descriptor(), wired_descriptor(path=b"fixture-other-wired")],
            [wired_descriptor(), descriptor(), descriptor(path=b"fixture-other-receiver")],
            [wired_descriptor(path=None)],
        ):
            with self.subTest(descriptors=descriptors):
                backend = FakeHid([], descriptors)
                reader = NativeBatteryReader(hid_module=backend)
                with self.assertRaises(BatteryReadError) as caught:
                    reader.read()
                self.assertNotIsInstance(caught.exception, DeviceUnavailable)
                self.assertEqual(backend.device_count, 0)

    def test_receiver_removal_without_wired_presence_never_implies_usb_power(self):
        reader, backend, first, _ = self.make_reader()
        reader.read()
        backend.descriptors = []
        with self.assertRaises(DeviceUnavailable):
            reader.read()
        self.assertEqual(first.close_count, 1)
        self.assertEqual(backend.device_count, 1)

    def test_link_down_requires_charge_status_and_skips_voltage(self):
        for charging in (0, 1):
            with self.subTest(charging=charging):
                responses = status(link=0, charging=charging)
                responses[5] = []
                reader, _, device, _ = self.make_reader(responses)
                self.assertEqual(reader.read(), BatteryReading(None, charging == 1, False, None))
                self.assertEqual([request[2] & 127 for request in device.writes], [36, 37])

    def test_connected_charging_does_not_query_missing_or_malformed_voltage(self):
        for voltage_responses in ([], [[5, 3, 133, 1, 100]]):
            with self.subTest(voltage_responses=voltage_responses):
                responses = status(charging=1)
                responses[5] = voltage_responses
                reader, _, device, clock = self.make_reader(responses)
                self.assertEqual(reader.read(), BatteryReading(None, True, True, None))
                self.assertEqual(device.writes, [
                    bytes([4, 2, 128 | command, 0]) + bytes(60) for command in (36, 37)
                ])
                self.assertEqual(clock.now, 0)
                self.assertEqual(device.close_count, 0)

    def test_charge_transition_omits_old_percent_and_resumes_fresh_noncharging_voltage(self):
        reader, backend, device, _ = self.make_reader()
        self.assertEqual(reader.read(), BatteryReading(55, False, True, 3940))
        device.responses = status(charging=1)
        device.responses[5] = []
        self.assertEqual(reader.read(), BatteryReading(None, True, True, None))
        device.responses = status(charging=0, millivolts=3880)
        self.assertEqual(reader.read(), BatteryReading(50, False, True, 3880))
        self.assertEqual([request[2] & 127 for request in device.writes],
                         [36, 37, 5, 36, 37, 36, 37, 5])
        self.assertEqual(backend.device_count, 1)

    def test_unknown_charging_never_returns_a_numeric_percent_or_disconnected_guess(self):
        for link in (0, 1):
            for charging in (2, 255, None):
                with self.subTest(link=link, charging=charging):
                    responses = status(link=link, charging=charging)
                    if charging is None:
                        responses[37] = []
                    reader, _, device, _ = self.make_reader(responses)
                    with self.assertRaises(BatteryReadError):
                        reader.read()
                    self.assertEqual([request[2] & 127 for request in device.writes], [36, 37])
                    self.assertEqual(device.close_count, 1)

    def test_unknown_link_is_an_error_not_a_disconnected_or_charging_reading(self):
        for linked in (2, 255):
            with self.subTest(linked=linked):
                reader, _, device, _ = self.make_reader(status(link=linked))
                with self.assertRaisesRegex(BatteryReadError, "unknown link state"):
                    reader.read()
                self.assertEqual(len(device.writes), 1)
                self.assertEqual(device.close_count, 1)

    def test_no_receiver_and_wrong_interfaces_do_not_open_any_handle(self):
        for descriptors in ([], [descriptor(interface_number=1)], [descriptor(usage=2)],
                            [descriptor(usage_page=1)], [descriptor(product_id=258)],
                            [descriptor(vendor_id=1)]):
            with self.subTest(descriptors=descriptors):
                backend = FakeHid([], descriptors)
                reader = NativeBatteryReader(hid_module=backend)
                with self.assertRaises(DeviceUnavailable):
                    reader.read()
                self.assertEqual(backend.device_count, 0)

    def test_ambiguous_or_incomplete_receiver_is_an_error_without_opening(self):
        for descriptors in ([descriptor(), descriptor(path=b"fixture-other")],
                            [descriptor(path=None)], ["invalid"]):
            with self.subTest(descriptors=descriptors):
                backend = FakeHid([], descriptors)
                reader = NativeBatteryReader(hid_module=backend)
                with self.assertRaises(BatteryReadError) as caught:
                    reader.read()
                self.assertNotIsInstance(caught.exception, DeviceUnavailable)
                self.assertEqual(backend.device_count, 0)

    def test_queued_old_same_command_replies_are_drained_before_every_query(self):
        responses = status()
        responses[36].append(report(37, [1]))
        responses[37].append(report(5, [0, 0]))
        reader, _, device, _ = self.make_reader(responses, stale=[report(36, [0])])
        self.assertEqual(reader.read(), BatteryReading(55, False, True, 3940))
        device.queue.extend([report(36, [0]), report(5, [255, 255])])
        device.responses = status(millivolts=4170)
        self.assertEqual(reader.read(), BatteryReading(75, False, True, 4170))

    def test_unrelated_report_ids_and_commands_are_ignored_within_budget(self):
        responses = status()
        responses[36] = [report(36, [0], report_id=4), report(5, [0, 0]),
                         report(36, [1], high_bit=False)]
        reader, _, _, _ = self.make_reader(responses)
        self.assertEqual(reader.read(), BatteryReading(55, False, True, 3940))

    def test_malformed_reports_close_handle(self):
        for malformed in ([5], [5, 2, 164, 61], [5, 2, 164, 1],
                          [5, 2, 164, 0], [5, 2, 164, 1, 256],
                          [5, 2, 164, 1, "bad"], [5] * 65, "bad"):
            with self.subTest(malformed=malformed):
                responses = status()
                responses[36] = [malformed]
                reader, _, device, _ = self.make_reader(responses)
                with self.assertRaises(BatteryReadError):
                    reader.read()
                self.assertEqual(device.close_count, 1)

    def test_voltage_needs_two_payload_bytes_and_honors_declared_length(self):
        responses = status()
        responses[5] = [[5, 3, 133, 1, 100, 15]]
        reader, _, device, _ = self.make_reader(responses)
        with self.assertRaises(BatteryReadError):
            reader.read()
        self.assertEqual(device.close_count, 1)

    def test_timeout_is_bounded_and_closes_handle(self):
        reader, _, device, clock = self.make_reader({})
        with self.assertRaisesRegex(BatteryReadError, "timed out"):
            reader.read()
        self.assertLessEqual(clock.now, QUERY_TIMEOUT_MS / 1000)
        self.assertLessEqual(len(device.reads), MAX_REPORT_SCANS)
        self.assertEqual(device.close_count, 1)

    def test_report_floods_are_capped_in_drain_and_response_scan(self):
        reader, _, device, _ = self.make_reader(stale=[report(36, [0])] * 100)
        with self.assertRaisesRegex(BatteryReadError, "queue did not settle"):
            reader.read()
        self.assertEqual(len(device.reads), MAX_REPORT_SCANS)
        self.assertEqual(device.writes, [])
        self.assertFalse(device.nonblocking)
        reader, _, device, _ = self.make_reader({36: [report(5, [0, 0])] * 100})
        with self.assertRaisesRegex(BatteryReadError, "scan limit"):
            reader.read()
        self.assertEqual(len(device.reads), MAX_REPORT_SCANS)
        self.assertEqual(device.close_count, 1)

    def test_drain_and_reply_share_a_deadline(self):
        reader, _, device, clock = self.make_reader({})
        device.drain_delay = 0.100
        with self.assertRaisesRegex(BatteryReadError, "timed out"):
            reader.read()
        self.assertAlmostEqual(clock.now, QUERY_TIMEOUT_MS / 1000)
        self.assertEqual(device.reads[-1][1], 250)

    def test_failure_discards_previous_values_and_reopens_next_poll(self):
        reader, backend, first, clock = self.make_reader()
        self.assertEqual(reader.read().percent, 55)
        first.responses = status(charging=255)
        with self.assertRaises(BatteryReadError):
            reader.read()
        second = FakeDevice(clock, status(charging=1, millivolts=4170))
        backend.devices.append(second)
        self.assertEqual(reader.read(), BatteryReading(None, True, True, None))
        self.assertEqual(first.close_count, 1)
        self.assertEqual(backend.device_count, 2)

    def test_removal_and_replacement_close_the_old_handle(self):
        reader, backend, first, clock = self.make_reader()
        reader.read()
        backend.descriptors = []
        with self.assertRaises(DeviceUnavailable):
            reader.read()
        self.assertEqual(first.close_count, 1)
        second = FakeDevice(clock)
        backend.descriptors = [descriptor(path=b"fixture-replacement")]
        backend.devices.append(second)
        reader.read()
        self.assertEqual(second.opened, [b"fixture-replacement"])

    def test_partial_writes_and_hid_errors_are_sanitized_and_close(self):
        for failure in ("write", "read", "open"):
            with self.subTest(failure=failure):
                reader, _, device, _ = self.make_reader()
                if failure == "write":
                    device.write_result = 63
                elif failure == "read":
                    device.read_error = OSError("PRIVATE_FIXTURE_PATH_SERIAL")
                else:
                    device.open_error = OSError("PRIVATE_FIXTURE_PATH_SERIAL")
                with self.assertRaises(BatteryReadError) as caught:
                    reader.read()
                self.assertNotIn("PRIVATE_FIXTURE", str(caught.exception))
                self.assertEqual(device.close_count, 1)

    def test_close_is_idempotent_and_all_io_is_serialized_under_lock(self):
        reader, backend, device, _ = self.make_reader()
        device.guard = backend.guard = lambda: self.assertTrue(reader._lock.locked())
        reader.read()
        reader.read()
        self.assertEqual(backend.device_count, 1)
        reader.close()
        reader.close()
        self.assertEqual(device.close_count, 1)


if __name__ == "__main__":
    unittest.main()
