import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import finalmouse_tray as tray


TEST_RUNTIME = tempfile.TemporaryDirectory()
TEST_DATA_DIR = TEST_RUNTIME.name
tray.DATA_DIR = TEST_DATA_DIR
tray.CHROME_PROFILE_DIR = os.path.join(TEST_DATA_DIR, "chrome-isolated")
tray.LOCK_FILE = os.path.join(TEST_DATA_DIR, "tray.lock")
tray.PID_FILE = os.path.join(TEST_DATA_DIR, "chrome.pids")
tray.CHARGE_LOG = os.path.join(TEST_DATA_DIR, "charge_log.json")
tray.SETTINGS_FILE = os.path.join(TEST_DATA_DIR, "settings.json")
tray.LOG_FILE = os.path.join(TEST_DATA_DIR, "tray.log")


class FakeElement:
    def __init__(self, text, displayed=True):
        self.text = text
        self._displayed = displayed

    def is_displayed(self):
        return self._displayed


class FakeDriver:
    def __init__(self, battery_texts=(), connect=False, body_text=""):
        self.battery_texts = list(battery_texts)
        self.connect = connect
        self.body_text = body_text

    def find_elements(self, by, selector):
        if selector == ".battery-text":
            return [FakeElement(text) for text in self.battery_texts]
        if selector == "button" and self.connect:
            return [FakeElement("Connect")]
        return []

    def find_element(self, by, selector):
        if selector == "body":
            return FakeElement(self.body_text)
        raise AssertionError(f"Unexpected selector: {selector}")


class FakeIcon:
    def __init__(self):
        self.icon = None
        self.title = None
        self.menu_updates = 0

    def update_menu(self):
        self.menu_updates += 1


class FakeStopEvent:
    def __init__(self, clock, stop_on_wait=False):
        self.clock = clock
        self.stop_on_wait = stop_on_wait
        self.waits = []
        self.stopped = False

    def wait(self, seconds):
        self.waits.append(seconds)
        self.clock[0] += seconds
        if self.stop_on_wait:
            self.stopped = True
        return self.stopped

    def is_set(self):
        return self.stopped


def make_app(charge_log=None, settings=None):
    with (
        mock.patch.object(tray, "load_charge_log", return_value=copy.deepcopy(charge_log or {})),
        mock.patch.object(tray, "load_settings", return_value=copy.deepcopy(settings or {})),
        mock.patch.object(tray, "save_charge_log"),
    ):
        app = tray.FinalmouseTray()
    app.icon = FakeIcon()
    app._start_charging_anim = mock.Mock()
    return app


class HelperTests(unittest.TestCase):
    def test_parse_percent_accepts_valid_values_and_rejects_invalid_values(self):
        cases = {
            "0%": 0,
            "Battery 54%": 54,
            "100": 100,
            "101%": None,
            "-1%": None,
            "1.5%": None,
            "charging": None,
            None: None,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(tray.parse_percent(value), expected)

    def test_battery_and_charging_icons_have_expected_geometry(self):
        for value in ("0%", "9%", "10%", "99%", "100%", "..."):
            with self.subTest(value=value):
                image = tray.create_battery_icon(value)
                self.assertEqual(image.mode, "RGBA")
                self.assertEqual(image.size, (256, 256))
                self.assertIsNotNone(image.getbbox())

        bolt = tray.create_charging_icon()
        self.assertEqual(bolt.mode, "RGBA")
        self.assertEqual(bolt.size, (256, 256))
        self.assertIsNotNone(bolt.getbbox())

    def test_json_save_is_atomic_and_leaves_no_temp_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir, "state.json")
            tray.save_json_file(str(path), {"last_known_pct": 62})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"last_known_pct": 62})
            self.assertEqual(list(pathlib.Path(temp_dir).glob("*.tmp")), [])

    def test_json_save_failure_preserves_previous_file_and_cleans_temp_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir, "state.json")
            path.write_text('{"original": true}\n', encoding="utf-8")
            with (
                mock.patch.object(tray.os, "replace", side_effect=OSError("busy")),
                self.assertRaises(OSError),
            ):
                tray.save_json_file(str(path), {"replacement": True})
            self.assertEqual(path.read_text(encoding="utf-8"), '{"original": true}\n')
            self.assertEqual(list(pathlib.Path(temp_dir).glob("*.tmp")), [])

    def test_process_identity_checks_fail_closed(self):
        owned = {
            "Name": "chrome.exe",
            "CommandLine": f'chrome --user-data-dir="{tray.CHROME_PROFILE_DIR}"',
        }
        unrelated = {
            "Name": "chrome.exe",
            "CommandLine": "chrome --user-data-dir=C:/Users/User/Chrome",
        }
        prefix_collision = {
            "Name": "chrome.exe",
            "CommandLine": f'chrome --user-data-dir="{tray.CHROME_PROFILE_DIR}-backup"',
        }
        driver = {"Name": "chromedriver.exe", "CreationDate": "created"}
        self.assertTrue(tray.is_owned_browser_snapshot(owned))
        self.assertFalse(tray.is_owned_browser_snapshot(unrelated))
        self.assertFalse(tray.is_owned_browser_snapshot(prefix_collision))
        self.assertFalse(tray.is_tracked_driver_snapshot(driver, None))
        self.assertTrue(tray.is_tracked_driver_snapshot(driver, "created"))
        self.assertFalse(tray.is_tracked_driver_snapshot(driver, "different"))

    def test_process_discovery_failure_is_not_an_empty_success(self):
        failed = subprocess.CompletedProcess(
            args=["powershell"],
            returncode=1,
            stdout="",
            stderr="CIM unavailable",
        )
        with (
            mock.patch.object(tray.subprocess, "run", return_value=failed),
            self.assertRaises(tray.ProcessSnapshotError),
        ):
            tray.get_process_snapshots()

    def test_process_creation_identity_is_stable_for_current_process(self):
        first = tray.get_process_creation_filetime(os.getpid())
        second = tray.get_process_creation_filetime(os.getpid())
        self.assertIsInstance(first, int)
        self.assertGreater(first, 0)
        self.assertEqual(first, second)


class ReadingTests(unittest.TestCase):
    def read(self, battery_texts=(), connect=False, body_text=""):
        app = tray.FinalmouseTray.__new__(tray.FinalmouseTray)
        app.driver = FakeDriver(battery_texts, connect, body_text)
        return app._read_battery_locked()

    def test_xpanel_reading_matrix(self):
        cases = [
            (("62%",), True, "", "62%"),
            (("0%",), True, "", tray.CHARGING_READING),
            (("100%",), False, "", "100%"),
            (("43%",), False, "", "43%"),
            ((), True, "", tray.DISCONNECTED_READING),
            ((), True, "Battery 0%", tray.CHARGING_READING),
            ((), False, "Battery 17%", "17%"),
        ]
        for battery_texts, connect, body_text, expected in cases:
            with self.subTest(
                battery_texts=battery_texts,
                connect=connect,
                body_text=body_text,
            ):
                self.assertEqual(
                    self.read(battery_texts, connect, body_text),
                    expected,
                )

    def test_refresh_settle_waits_before_one_ready_dom_read(self):
        clock = [100.0]
        app = tray.FinalmouseTray.__new__(tray.FinalmouseTray)
        app.running = True
        app.stop_event = FakeStopEvent(clock)
        app._read_battery_locked = mock.Mock(return_value="62%")
        with mock.patch.object(tray.time, "monotonic", side_effect=lambda: clock[0]):
            reading = app._wait_for_battery_locked(5, minimum_wait_seconds=2)

        self.assertEqual(reading, "62%")
        self.assertEqual(app.stop_event.waits, [2])
        app._read_battery_locked.assert_called_once()

    def test_refresh_settle_stops_without_reading(self):
        clock = [100.0]
        app = tray.FinalmouseTray.__new__(tray.FinalmouseTray)
        app.running = True
        app.stop_event = FakeStopEvent(clock, stop_on_wait=True)
        app._read_battery_locked = mock.Mock(return_value="62%")
        with mock.patch.object(tray.time, "monotonic", side_effect=lambda: clock[0]):
            reading = app._wait_for_battery_locked(5, minimum_wait_seconds=2)

        self.assertIsNone(reading)
        app._read_battery_locked.assert_not_called()


class StateTests(unittest.TestCase):
    def test_disconnected_state_preserves_last_known_percent(self):
        app = make_app({"last_known_pct": 62})
        app.battery_pct = "62%"

        with mock.patch.object(tray, "save_charge_log") as save:
            app._update_icon(tray.DISCONNECTED_READING)

        self.assertEqual(tray.battery_state(tray.DISCONNECTED_READING), "disconnected")
        self.assertEqual(app.battery_pct, "62%")
        self.assertIn("62%", app.icon.title)
        save.assert_not_called()

    def test_unknown_reading_does_not_leave_charging_state(self):
        app = make_app(
            {
                "last_known_pct": 54,
                "pending_charge": {
                    "start_pct": 54,
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                },
            }
        )
        app.battery_pct = "54%"
        app.is_charging = True

        app._update_icon(None)

        self.assertTrue(app.is_charging)
        self.assertIn("Charging", app.icon.title)
        self.assertIn("pending_charge", app.charge_log)

    def test_charging_at_zero_preserves_last_nonzero_percent(self):
        app = make_app({"last_known_pct": 54})
        app.battery_pct = "54%"

        with mock.patch.object(tray, "save_charge_log") as save:
            app._update_icon(tray.CHARGING_READING)

        self.assertTrue(app.is_charging)
        self.assertEqual(app.battery_pct, "54%")
        self.assertEqual(app.charge_log["last_known_pct"], 54)
        self.assertEqual(app.charge_log["pending_charge"]["start_pct"], 54)
        save.assert_called_once()

    def test_unchanged_percent_does_not_rewrite_state_or_redraw_icon(self):
        app = make_app({"last_known_pct": 62})
        app.battery_pct = "62%"

        with (
            mock.patch.object(tray, "save_charge_log") as save,
            mock.patch.object(
                tray,
                "create_battery_icon",
                wraps=tray.create_battery_icon,
            ) as render,
        ):
            app._update_icon("62%")
            app._update_icon("62%")

        save.assert_not_called()
        self.assertEqual(render.call_count, 1)

    def test_valid_percent_finishes_a_real_charge_session(self):
        started_at = datetime.now() - timedelta(minutes=5)
        app = make_app(
            {
                "last_known_pct": 54,
                "pending_charge": {
                    "start_pct": 54,
                    "started_at": started_at.isoformat(timespec="seconds"),
                },
            }
        )
        app.battery_pct = "54%"
        app.is_charging = True

        with mock.patch.object(tray, "save_charge_log"):
            app._update_icon("60%")

        self.assertFalse(app.is_charging)
        self.assertNotIn("pending_charge", app.charge_log)
        self.assertEqual(app.charge_log["last_charge"]["start_pct"], 54)
        self.assertEqual(app.charge_log["last_charge"]["end_pct"], 60)
        self.assertEqual(app.charge_log["last_known_pct"], 60)

    def test_migration_clears_malformed_nested_state(self):
        with (
            mock.patch.object(
                tray,
                "load_charge_log",
                return_value={
                    "pending_charge": ["not", "a", "mapping"],
                    "last_charge": "bad",
                },
            ),
            mock.patch.object(tray, "load_settings", return_value={}),
            mock.patch.object(tray, "save_charge_log") as save,
            mock.patch.object(tray, "log_event"),
        ):
            app = tray.FinalmouseTray()

        self.assertNotIn("pending_charge", app.charge_log)
        self.assertNotIn("last_charge", app.charge_log)
        save.assert_called_once()

    def test_migration_handles_aware_pending_time_and_bad_duration(self):
        charge_log = {
            "last_known_pct": 62,
            "pending_charge": {
                "start_pct": 62,
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
            "last_charge": {
                "start_pct": 40,
                "end_pct": 62,
                "ended_at": datetime.now(timezone.utc).isoformat(),
                "duration_seconds": "bad",
            },
        }
        app = make_app(charge_log)

        self.assertIn("pending_charge", app.charge_log)
        self.assertIsNone(app.charge_log["last_charge"]["duration_seconds"])
        self.assertIn("in unknown", app._build_tooltip())

    def test_reconnect_ui_is_replaced_when_battery_state_is_unchanged(self):
        app = make_app({"last_known_pct": 62})
        app.battery_pct = "62%"
        app._update_icon("62%")

        with (
            mock.patch.object(app, "_kill_chrome_locked", return_value=True),
            mock.patch.object(app, "start_browser", return_value=True),
            mock.patch.object(app, "_wait_for_battery_locked", return_value="62%"),
        ):
            reading = app.restart_browser("test", force=True)
        self.assertEqual(app.icon.title, "Finalmouse ULX: Reconnecting...")

        app._update_icon(reading)

        self.assertEqual(app.icon.title, "Finalmouse ULX: 62%")
        self.assertEqual(app._display_key[0], "battery")

    def test_restart_is_blocked_after_shutdown_signal(self):
        app = make_app({"last_known_pct": 62})
        app._signal_stop()
        with mock.patch.object(app, "start_browser") as start:
            self.assertIsNone(app.restart_browser("late menu action", force=True))
        start.assert_not_called()


class LockTests(unittest.TestCase):
    def test_lock_identity_supports_json_and_legacy_pid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir, "tray.lock")
            with mock.patch.object(tray, "LOCK_FILE", str(path)):
                path.write_text('{"pid": 123, "creation_date": "x"}', encoding="utf-8")
                self.assertEqual(tray.read_lock_identity()["pid"], 123)
                path.write_text("456", encoding="utf-8")
                self.assertEqual(tray.read_lock_identity()["pid"], 456)

    def test_acquire_lock_writes_identity_after_exclusive_ownership(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir, "tray.lock")
            snapshots = {
                os.getpid(): {
                    "ProcessId": os.getpid(),
                    "Name": "python.exe",
                    "CreationDate": "created",
                    "CommandLine": "python tests",
                }
            }
            with (
                mock.patch.object(tray, "LOCK_FILE", str(path)),
                mock.patch.object(tray, "acquire_instance_mutex", return_value=True),
                mock.patch.object(tray, "release_instance_mutex"),
                mock.patch.object(tray, "get_process_snapshots", return_value=snapshots),
            ):
                self.assertTrue(tray.acquire_lock())
                identity = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(identity["pid"], os.getpid())
                self.assertEqual(identity["creation_date"], "created")

    def test_duplicate_process_releases_mutex_and_does_not_write_lock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir, "tray.lock")
            snapshots = {
                999: {
                    "ProcessId": 999,
                    "Name": "pythonw.exe",
                    "CreationDate": "created",
                    "CommandLine": f'pythonw "{pathlib.Path(tray.__file__).resolve()}"',
                }
            }
            with (
                mock.patch.object(tray, "LOCK_FILE", str(path)),
                mock.patch.object(tray, "acquire_instance_mutex", return_value=True),
                mock.patch.object(tray, "release_instance_mutex") as release,
                mock.patch.object(tray, "get_process_snapshots", return_value=snapshots),
            ):
                self.assertFalse(tray.acquire_lock())
                self.assertFalse(path.exists())
                release.assert_called_once()

    def test_legacy_lock_blocks_same_app_running_from_installed_copy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir, "tray.lock")
            path.write_text("999", encoding="utf-8")
            snapshots = {
                999: {
                    "ProcessId": 999,
                    "Name": "pythonw.exe",
                    "CreationDate": "created",
                    "CommandLine": (
                        'pythonw "C:\\Different\\Install\\finalmouse_tray.py"'
                    ),
                }
            }
            with (
                mock.patch.object(tray, "LOCK_FILE", str(path)),
                mock.patch.object(tray, "acquire_instance_mutex", return_value=True),
                mock.patch.object(tray, "release_instance_mutex") as release,
                mock.patch.object(tray, "get_process_snapshots", return_value=snapshots),
            ):
                self.assertFalse(tray.acquire_lock())
                self.assertEqual(path.read_text(encoding="utf-8"), "999")
                release.assert_called_once()

    def test_lock_acquisition_fails_closed_when_process_discovery_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir, "tray.lock")
            with (
                mock.patch.object(tray, "LOCK_FILE", str(path)),
                mock.patch.object(tray, "acquire_instance_mutex", return_value=True),
                mock.patch.object(tray, "release_instance_mutex") as release,
                mock.patch.object(
                    tray,
                    "get_process_snapshots",
                    side_effect=tray.ProcessSnapshotError("unavailable"),
                ),
            ):
                self.assertFalse(tray.acquire_lock())
                self.assertFalse(path.exists())
                release.assert_called_once()

    def test_release_lock_does_not_remove_another_owners_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir, "tray.lock")
            path.write_text(json.dumps({"pid": os.getpid() + 1}), encoding="utf-8")
            with (
                mock.patch.object(tray, "LOCK_FILE", str(path)),
                mock.patch.object(tray, "release_instance_mutex") as release,
            ):
                tray.release_lock()
                self.assertTrue(path.exists())
                release.assert_called_once()

    def test_release_lock_removes_current_owners_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir, "tray.lock")
            path.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
            with (
                mock.patch.object(tray, "LOCK_FILE", str(path)),
                mock.patch.object(tray, "release_instance_mutex") as release,
            ):
                tray.release_lock()
                self.assertFalse(path.exists())
                release.assert_called_once()


class CleanupTests(unittest.TestCase):
    def test_cleanup_preserves_tracking_when_process_discovery_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pid_file = pathlib.Path(temp_dir, "chrome.pids")
            pid_file.write_text(
                json.dumps([{"pid": 321, "role": "browser"}]),
                encoding="utf-8",
            )
            with (
                mock.patch.object(tray, "PID_FILE", str(pid_file)),
                mock.patch.object(
                    tray,
                    "get_process_snapshots",
                    side_effect=tray.ProcessSnapshotError("unavailable"),
                ),
                mock.patch.object(tray, "taskkill_pid") as taskkill,
            ):
                self.assertFalse(tray.cleanup_tracked_processes())

            self.assertTrue(pid_file.exists())
            taskkill.assert_not_called()

    def test_cleanup_preserves_pid_file_when_owned_browser_survives(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pid_file = pathlib.Path(temp_dir, "chrome.pids")
            pid_file.write_text(
                json.dumps([{"pid": 321, "role": "browser"}]),
                encoding="utf-8",
            )
            owned = {
                321: {
                    "ProcessId": 321,
                    "Name": "chrome.exe",
                    "CommandLine": f'chrome --user-data-dir="{tray.CHROME_PROFILE_DIR}"',
                }
            }
            with (
                mock.patch.object(tray, "PID_FILE", str(pid_file)),
                mock.patch.object(
                    tray,
                    "get_process_snapshots",
                    side_effect=[owned, owned],
                ),
                mock.patch.object(tray, "taskkill_pid", return_value=False),
            ):
                self.assertFalse(tray.cleanup_tracked_processes())

            self.assertTrue(pid_file.exists())

    def test_cleanup_removes_pid_file_after_owned_processes_exit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pid_file = pathlib.Path(temp_dir, "chrome.pids")
            pid_file.write_text(
                json.dumps([{"pid": 321, "role": "browser"}]),
                encoding="utf-8",
            )
            owned = {
                321: {
                    "ProcessId": 321,
                    "Name": "chrome.exe",
                    "CommandLine": f'chrome --user-data-dir="{tray.CHROME_PROFILE_DIR}"',
                }
            }
            with (
                mock.patch.object(tray, "PID_FILE", str(pid_file)),
                mock.patch.object(
                    tray,
                    "get_process_snapshots",
                    side_effect=[owned, {}],
                ),
                mock.patch.object(tray, "taskkill_pid", return_value=True),
            ):
                self.assertTrue(tray.cleanup_tracked_processes())

            self.assertFalse(pid_file.exists())


class BrowserLifecycleTests(unittest.TestCase):
    def test_cached_browser_pid_reuse_requires_an_identity_rescan(self):
        app = tray.FinalmouseTray.__new__(tray.FinalmouseTray)
        app.browser_pids = {321}
        app.browser_creation_times = {321: 100}
        app.chrome_pids = {321}
        app.driver = object()
        app.driver_pid = None
        app.driver_creation_filetime = None
        with (
            mock.patch.object(tray, "get_process_creation_filetime", return_value=200),
            mock.patch.object(tray, "get_process_snapshots", return_value={}) as scan,
            mock.patch.object(app, "_save_pids"),
        ):
            self.assertFalse(app._has_live_browser_process())

        scan.assert_called_once()
        self.assertEqual(app.browser_pids, set())

    def test_browser_start_aborts_if_shutdown_arrives_during_constructor(self):
        app = make_app({"last_known_pct": 62})
        fake_driver = mock.Mock()

        def construct(*args, **kwargs):
            app._signal_stop()
            return fake_driver

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                mock.patch.object(app, "_cleanup_previous", return_value=True),
                mock.patch.object(tray.webdriver, "Chrome", side_effect=construct),
                mock.patch.object(tray, "PID_FILE", str(pathlib.Path(temp_dir, "pids"))),
                mock.patch.object(tray, "get_process_snapshots", return_value={}),
                mock.patch.object(app, "_track_browser_pids") as track,
            ):
                self.assertFalse(app.start_browser())

        fake_driver.quit.assert_called_once()
        track.assert_not_called()
        fake_driver.get.assert_not_called()


class StopScriptTests(unittest.TestCase):
    def test_start_batch_uses_an_absolute_quoted_script_path(self):
        start_path = pathlib.Path(__file__).resolve().parents[1] / "start.bat"
        content = start_path.read_text(encoding="utf-8").lower()
        self.assertIn('pythonw "%~dp0finalmouse_tray.py"', content)

    def test_stop_batch_preserves_cleanup_exit_code(self):
        stop_path = pathlib.Path(__file__).resolve().parents[1] / "stop.bat"
        content = stop_path.read_text(encoding="utf-8").lower()
        self.assertIn('set "stopcode=%errorlevel%"', content)
        self.assertIn("exit /b %stopcode%", content)

    def test_stop_script_stops_an_exact_absolute_script_launch(self):
        stop_script = pathlib.Path(__file__).resolve().parents[1] / "stop_finalmouse.ps1"
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_script = pathlib.Path(temp_dir, "finalmouse_tray.py")
            fake_script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
            child = subprocess.Popen(
                [sys.executable, str(fake_script)],
                creationflags=tray.NO_WINDOW,
            )
            try:
                result = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-File",
                        str(stop_script),
                        "-DataDir",
                        temp_dir,
                        "-ScriptPath",
                        str(fake_script),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    creationflags=tray.NO_WINDOW,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                child.wait(timeout=5)
                self.assertIsNotNone(child.returncode)
            finally:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()

    def test_stop_script_ignores_script_path_used_as_data_argument(self):
        stop_script = pathlib.Path(__file__).resolve().parents[1] / "stop_finalmouse.ps1"
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_script = pathlib.Path(temp_dir, "finalmouse_tray.py")
            fake_script.write_text("# decoy path only\n", encoding="utf-8")
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import time; time.sleep(30)",
                    str(fake_script),
                ],
                creationflags=tray.NO_WINDOW,
            )
            try:
                result = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-File",
                        str(stop_script),
                        "-DataDir",
                        temp_dir,
                        "-ScriptPath",
                        str(fake_script),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    creationflags=tray.NO_WINDOW,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIsNone(child.poll(), "A decoy data argument was treated as a script")
            finally:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()

    def test_stop_script_preserves_legacy_relative_launch_state(self):
        stop_script = pathlib.Path(__file__).resolve().parents[1] / "stop_finalmouse.ps1"
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_script = pathlib.Path(temp_dir, "finalmouse_tray.py")
            fake_script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
            child = subprocess.Popen(
                [sys.executable, fake_script.name],
                cwd=temp_dir,
                creationflags=tray.NO_WINDOW,
            )
            lock_path = pathlib.Path(temp_dir, "tray.lock")
            lock_path.write_text(str(child.pid), encoding="utf-8")
            try:
                result = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-File",
                        str(stop_script),
                        "-DataDir",
                        temp_dir,
                        "-ScriptPath",
                        str(fake_script),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    creationflags=tray.NO_WINDOW,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(child.poll())
                self.assertEqual(lock_path.read_text(encoding="utf-8"), str(child.pid))
            finally:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()

    def test_legacy_stale_lock_pid_cannot_kill_unrelated_process(self):
        script_path = pathlib.Path(__file__).resolve().parents[1] / "stop_finalmouse.ps1"
        with tempfile.TemporaryDirectory() as temp_dir:
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                creationflags=tray.NO_WINDOW,
            )
            try:
                pathlib.Path(temp_dir, "tray.lock").write_text(
                    str(child.pid),
                    encoding="utf-8",
                )
                fake_script = pathlib.Path(temp_dir, "not-the-live-app", "finalmouse_tray.py")
                result = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-File",
                        str(script_path),
                        "-DataDir",
                        temp_dir,
                        "-ScriptPath",
                        str(fake_script),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    creationflags=tray.NO_WINDOW,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIsNone(child.poll(), "The unrelated stale-lock PID was terminated")
            finally:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()


if __name__ == "__main__":
    unittest.main()
