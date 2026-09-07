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
tray.LOCK_FILE = os.path.join(TEST_DATA_DIR, "tray.lock")
tray.CHARGE_LOG = os.path.join(TEST_DATA_DIR, "charge_log.json")
tray.SETTINGS_FILE = os.path.join(TEST_DATA_DIR, "settings.json")
tray.LOG_FILE = os.path.join(TEST_DATA_DIR, "tray.log")


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
        app = tray.FinalmouseTray(reader=mock.Mock())
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
            app = tray.FinalmouseTray(reader=mock.Mock())

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


class LockTests(unittest.TestCase):
    def test_venv_redirector_is_not_a_second_tray_instance(self):
        current_pid, parent_pid = os.getpid(), os.getppid()
        script = str(pathlib.Path(tray.__file__).resolve())
        launcher = r'C:\Tray\.venv\Scripts\pythonw.exe'
        snapshots = {
            current_pid: {"ProcessId": current_pid, "ParentProcessId": parent_pid,
                "Name": "pythonw.exe", "CommandLine": f'"C:\\Python\\pythonw.exe" "{script}"'},
            parent_pid: {"ProcessId": parent_pid, "Name": "pythonw.exe",
                "CommandLine": f'"{launcher}" "{script}"'},
        }
        with (
            mock.patch.object(tray.sys, "prefix", r'C:\Tray\.venv'),
            mock.patch.object(tray.sys, "base_prefix", r'C:\Python'),
            mock.patch.object(tray.sys, "executable", launcher),
            mock.patch.object(tray, "read_lock_identity", return_value={}),
            mock.patch.object(tray, "get_process_creation_filetime", side_effect=lambda pid: {current_pid: 200, parent_pid: 100}.get(pid)),
        ):
            self.assertEqual(tray.get_tray_process_pids(snapshots=snapshots), set())
            self.assertEqual(tray.get_tray_process_pids(exclude_current=False, snapshots=snapshots), {current_pid, parent_pid})
            snapshots[999] = {"ProcessId": 999, "Name": "pythonw.exe", "CommandLine": f'"{launcher}" "{script}"'}
            self.assertEqual(tray.get_tray_process_pids(snapshots=snapshots), {999})
            snapshots.pop(999)
            snapshots[parent_pid]["CommandLine"] += ' --unexpected-option'
            self.assertEqual(tray.get_tray_process_pids(snapshots=snapshots), {parent_pid})
            snapshots[parent_pid]["CommandLine"] = f'"C:\\Other\\pythonw.exe" "{script}"'
            self.assertEqual(tray.get_tray_process_pids(snapshots=snapshots), {parent_pid})

    def test_venv_parent_exclusion_requires_verified_creation_order(self):
        current_pid, parent_pid = os.getpid(), os.getppid()
        script = str(pathlib.Path(tray.__file__).resolve())
        launcher = r'C:\Tray\.venv\Scripts\pythonw.exe'
        snapshots = {
            current_pid: {"ProcessId": current_pid, "ParentProcessId": parent_pid,
                "Name": "pythonw.exe", "CommandLine": f'"C:\\Python\\pythonw.exe" "{script}"'},
            parent_pid: {"ProcessId": parent_pid, "Name": "pythonw.exe", "CommandLine": f'"{launcher}" "{script}"'},
        }
        with (
            mock.patch.object(tray.sys, "prefix", r'C:\Tray\.venv'),
            mock.patch.object(tray.sys, "base_prefix", r'C:\Python'),
            mock.patch.object(tray.sys, "executable", launcher),
            mock.patch.object(tray, "read_lock_identity", return_value={}),
        ):
            for parent_created in (None, 300):
                with self.subTest(parent_created=parent_created), mock.patch.object(
                    tray, "get_process_creation_filetime", side_effect=lambda pid: 200 if pid == current_pid else parent_created
                ):
                    self.assertEqual(tray.get_tray_process_pids(snapshots=snapshots), {parent_pid})

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


class StopScriptTests(unittest.TestCase):
    def test_start_batch_uses_an_absolute_quoted_script_path(self):
        start_path = pathlib.Path(__file__).resolve().parents[1] / "start.bat"
        content = start_path.read_text(encoding="utf-8").lower()
        self.assertIn('cscript.exe //nologo "%~dp0finalmouse_tray_silent.vbs"', content)
        launcher = start_path.with_name("finalmouse_tray_silent.vbs").read_text(encoding="utf-8").lower()
        self.assertIn('files.buildpath(scriptdir, ".venv\\scripts\\pythonw.exe")', launcher)
        self.assertIn('files.buildpath(scriptdir, "finalmouse_tray.py")', launcher)

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


class NativeIntegrationTests(unittest.TestCase):
    def make_native(self, charge_log=None):
        reader = mock.Mock()
        with (
            mock.patch.object(tray, "load_charge_log", return_value=copy.deepcopy(charge_log or {})),
            mock.patch.object(tray, "load_settings", return_value={}),
            mock.patch.object(tray, "save_charge_log"),
        ):
            app = tray.FinalmouseTray(reader=reader)
        app.icon = FakeIcon()
        app._start_charging_anim = mock.Mock()
        return app, reader

    def test_native_startup_does_not_load_selenium(self):
        environment = dict(os.environ, LOCALAPPDATA=TEST_DATA_DIR)
        result = subprocess.run(
            [sys.executable, "-B", "-c",
             "import sys; import finalmouse_tray as t; "
             "app=t.FinalmouseTray(reader=object()); "
             "assert not any(x=='selenium' or x.startswith('selenium.') for x in sys.modules)"],
            env=environment, capture_output=True, text=True, timeout=10,
            creationflags=tray.NO_WINDOW,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_default_controller_constructs_only_the_native_reader(self):
        with (
            mock.patch.object(tray, "NativeBatteryReader") as factory,
            mock.patch.object(tray.subprocess, "run", side_effect=AssertionError("Unexpected helper")),
            mock.patch.object(tray, "load_charge_log", return_value={}),
            mock.patch.object(tray, "load_settings", return_value={}),
        ):
            app = tray.FinalmouseTray()
        factory.assert_called_once_with()
        self.assertIs(app.native_reader, factory.return_value)
        self.assertEqual(app.reconnect_label, "Reconnect Receiver")

    def test_unsupported_browser_argument_exits_without_starting_or_locking(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            environment = dict(os.environ, LOCALAPPDATA=temp_dir)
            result = subprocess.run(
                [sys.executable, "-B", str(pathlib.Path(tray.__file__).resolve()), "--browser"],
                env=environment, capture_output=True, text=True, timeout=10,
                creationflags=tray.NO_WINDOW,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("Unsupported arguments", result.stderr)
            self.assertFalse(pathlib.Path(temp_dir, "finalmouse-tray", "tray.lock").exists())
            self.assertFalse(pathlib.Path(temp_dir, "finalmouse-tray", "tray.log").exists())

    def test_start_refresh_reconnect_and_close_have_no_process_helpers(self):
        from native_hid import BatteryReading
        app, reader = self.make_native()
        reader.read.return_value = BatteryReading(55, False, True, 3940)
        with (
            mock.patch.object(tray.subprocess, "run", side_effect=AssertionError("Unexpected helper")),
            mock.patch.object(tray, "get_process_snapshots", side_effect=AssertionError("Unexpected discovery")),
            mock.patch.object(app, "_run_menu_action", side_effect=lambda label, action: action()),
        ):
            self.assertTrue(app.start_reader())
            reader.read.assert_not_called()
            app.on_refresh(app.icon, None)
            app.on_reconnect(app.icon, None)
            self.assertEqual(app.battery_pct, "55%")
            self.assertEqual(reader.close.call_count, 2)
            app.close_reader()
            app.close_reader()
            self.assertEqual(reader.close.call_count, 3)
            self.assertIsNone(app.read_battery())
            self.assertIsNone(app.reconnect_receiver("late action"))
            self.assertFalse(app.start_reader())
            self.assertEqual(reader.read.call_count, 2)

    def test_shutdown_blocks_new_reads_and_reconnects(self):
        app, reader = self.make_native()
        app._signal_stop()
        self.assertFalse(app.start_reader())
        self.assertIsNone(app.read_battery())
        self.assertIsNone(app.reconnect_receiver("late action"))
        reader.read.assert_not_called()
        reader.close.assert_not_called()
        app.close_reader()
        reader.close.assert_called_once_with()

    def test_reconnect_waiting_for_read_lock_rechecks_shutdown(self):
        app, reader = self.make_native()
        waiting = threading.Event()
        results = []
        original_lock = app.reader_lock

        class ObservedLock:
            def __enter__(self):
                waiting.set()
                return original_lock.__enter__()

            def __exit__(self, *args):
                return original_lock.__exit__(*args)

        app.reader_lock = ObservedLock()
        worker = threading.Thread(target=lambda: results.append(app.reconnect_receiver("waiting action")))
        with original_lock:
            worker.start()
            try:
                self.assertTrue(waiting.wait(2))
            finally:
                app._signal_stop()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [None])
        reader.read.assert_not_called()
        reader.close.assert_not_called()

    def test_poll_reads_immediately_then_waits_the_existing_interval(self):
        app, _ = self.make_native()
        app.stop_event = FakeStopEvent([0], stop_on_wait=True)
        with (
            mock.patch.object(app, "read_battery", return_value="62%") as read,
            mock.patch.object(app, "_update_icon") as update,
        ):
            app.poll_loop()
        read.assert_called_once_with()
        update.assert_called_once_with("62%")
        self.assertEqual(app.stop_event.waits, [tray.POLL_INTERVAL])

    def test_watchdog_restarts_only_a_stopped_poll_worker(self):
        app, reader = self.make_native()
        app.poll_thread = mock.Mock()
        with mock.patch.object(app, "_start_poll_thread") as start:
            app.poll_thread.is_alive.return_value = True
            app._watchdog_check()
            start.assert_not_called()
            app.poll_thread.is_alive.return_value = False
            app._watchdog_check()
            start.assert_called_once_with()
            app._signal_stop()
            app._watchdog_check()
            start.assert_called_once_with()
        reader.read.assert_not_called()
        reader.close.assert_not_called()

    def test_run_preserves_menu_and_closes_reader_after_icon_exit(self):
        app, reader = self.make_native({"last_known_pct": 55})
        with (
            mock.patch.object(tray.atexit, "register"),
            mock.patch.object(tray.pystray, "Icon") as icon,
            mock.patch.object(app, "_start_poll_thread") as poll,
            mock.patch.object(app, "_start_watchdog_thread") as watchdog,
            mock.patch.object(app, "_join_worker_threads") as join,
        ):
            app.run()
        menu = icon.call_args.args[3]
        self.assertEqual([item.text for item in menu.items if item is not tray.pystray.Menu.SEPARATOR],
                         ["Refresh", "Reconnect Receiver", "Dark text", "Quit"])
        icon.return_value.run.assert_called_once_with()
        poll.assert_called_once_with()
        watchdog.assert_called_once_with()
        join.assert_called_once_with()
        reader.close.assert_called_once_with()
        self.assertTrue(app.stop_event.is_set())

    def test_native_status_updates_same_tray_and_charge_history(self):
        from native_hid import BatteryReading
        app, reader = self.make_native({"last_known_pct": 55})
        reader.read.return_value = BatteryReading(55, False, True, 3940)
        app._update_icon(app.read_battery())
        self.assertEqual(app.battery_pct, "55%")
        reader.read.return_value = BatteryReading(60, True, True, 3996)
        app._update_icon(app.read_battery())
        self.assertTrue(app.is_charging)
        pending = copy.deepcopy(app.charge_log["pending_charge"])
        self.assertEqual(pending["start_pct"], 55)
        reader.read.return_value = BatteryReading(70, True, True, 4112)
        app._update_icon(app.read_battery())
        self.assertEqual(app.charge_log["pending_charge"], pending)
        app.charge_log["pending_charge"]["started_at"] = (datetime.now() - timedelta(minutes=2)).isoformat()
        reader.read.return_value = BatteryReading(70, False, True, 4112)
        app._update_icon(app.read_battery())
        self.assertFalse(app.is_charging)
        self.assertEqual(app.charge_log["last_charge"]["start_pct"], 55)
        self.assertEqual(app.charge_log["last_charge"]["end_pct"], 70)

    def test_unknown_and_inactive_reads_do_not_finish_charge(self):
        from native_hid import BatteryReadError, DeviceUnavailable, BatteryReading
        app, reader = self.make_native({"last_known_pct": 55})
        app._update_icon(tray.CHARGING_READING)
        pending = copy.deepcopy(app.charge_log["pending_charge"])
        for value in (DeviceUnavailable("Unavailable"), BatteryReadError("Timed out"),
                      BatteryReading(None, False, False, None), BatteryReading(0, False, True, 0)):
            with self.subTest(value=value):
                reader.read.side_effect = value if isinstance(value, Exception) else None
                reader.read.return_value = value
                app._update_icon(app.read_battery())
                self.assertTrue(app.is_charging)
                self.assertEqual(app.charge_log["pending_charge"], pending)
                self.assertEqual(app.battery_pct, "55%")

    def test_affirmative_charge_is_distinct_from_link_inactivity(self):
        from native_hid import BatteryReading
        app, reader = self.make_native({"last_known_pct": 55})
        reader.read.return_value = BatteryReading(None, True, False, None)
        self.assertEqual(app.read_battery(), tray.CHARGING_READING)
        reader.read.return_value = BatteryReading(None, False, False, None)
        self.assertEqual(app.read_battery(), tray.DISCONNECTED_READING)

    def test_wired_power_uses_charging_ui_until_a_fresh_wireless_percent(self):
        from native_hid import BatteryReading, DeviceUnavailable
        app, reader = self.make_native({"last_known_pct": 53})
        reader.read.return_value = BatteryReading(None, None, True, None, power_connected=True)
        app._update_icon(app.read_battery())
        self.assertTrue(app.is_charging)
        self.assertEqual(app.battery_pct, "53%")
        pending = copy.deepcopy(app.charge_log["pending_charge"])
        self.assertEqual(pending["start_pct"], 53)
        reader.read.side_effect = DeviceUnavailable("ULX is unavailable")
        app._update_icon(app.read_battery())
        self.assertEqual(app.charge_log["pending_charge"], pending)
        reader.read.side_effect = None
        reader.read.return_value = BatteryReading(60, False, True, 3996)
        app.charge_log["pending_charge"]["started_at"] = (datetime.now() - timedelta(minutes=2)).isoformat()
        app._update_icon(app.read_battery())
        self.assertFalse(app.is_charging)
        self.assertEqual(app.charge_log["last_charge"]["start_pct"], 53)
        self.assertEqual(app.charge_log["last_charge"]["end_pct"], 60)


    def test_poll_applies_its_read_before_a_waiting_menu_refresh(self):
        app, _ = self.make_native()
        read_started = threading.Event()
        menu_attempted = threading.Event()
        allow_read = threading.Event()
        applied = []
        failures = []

        def poll_read():
            read_started.set()
            if not allow_read.wait(2):
                raise AssertionError("Timed out waiting to finish the poll read")
            return "55%"

        def menu_refresh():
            menu_attempted.set()
            try:
                with app.reader_lock:
                    applied.append("60%")
            except Exception as error:
                failures.append(error)

        def apply_read(reading):
            # Check lock ownership from another thread at the critical join.
            # RLock can be acquired recursively by the polling thread itself.
            acquired = []
            def check_lock():
                held = app.reader_lock.acquire(blocking=False)
                acquired.append(held)
                if held:
                    app.reader_lock.release()
            checker = threading.Thread(target=check_lock)
            checker.start()
            checker.join(2)
            if checker.is_alive() or acquired != [False]:
                failures.append(AssertionError("Poll released the lock before applying its sample"))
            applied.append(reading)
            app.stop_event.set()

        with (
            mock.patch.object(app, "read_battery", side_effect=poll_read),
            mock.patch.object(app, "_update_icon", side_effect=apply_read),
        ):
            poll = threading.Thread(target=app.poll_loop)
            menu = threading.Thread(target=menu_refresh)
            try:
                poll.start()
                self.assertTrue(read_started.wait(2))
                menu.start()
                self.assertTrue(menu_attempted.wait(2))
            finally:
                allow_read.set()
                poll.join(3)
                if menu.ident is not None:
                    menu.join(3)
                app._signal_stop()
            self.assertFalse(poll.is_alive())
            self.assertFalse(menu.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(applied, ["55%", "60%"])

    def test_native_idle_reads_do_not_redraw_or_rewrite_unchanged_state(self):
        from native_hid import BatteryReading
        app, reader = self.make_native({"last_known_pct": 55})
        reader.read.return_value = BatteryReading(55, False, True, 3940)
        app._update_icon(app.read_battery())
        first_icon = app.icon.icon
        with mock.patch.object(tray, "save_charge_log") as save:
            for _ in range(12):
                app._update_icon(app.read_battery())
            save.assert_not_called()
        self.assertIs(first_icon, app.icon.icon)


if __name__ == "__main__":
    unittest.main()
