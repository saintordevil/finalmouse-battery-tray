"""Windows launcher smoke tests using temporary recorders, never the tray app."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import venv


ROOT = Path(__file__).resolve().parents[1]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@unittest.skipUnless(os.name == "nt", "Windows launchers require Windows")
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="finalmouse-launcher-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.project = self.base / "project with spaces & bang!"
        self.project.mkdir()
        self.caller = self.base / "unrelated working directory"
        self.caller.mkdir()
        for name in ("start.bat", "finalmouse_tray_silent.vbs", "install.bat"):
            shutil.copy2(ROOT / name, self.project / name)
        self.record = self.base / "launch.json"
        self.env = dict(os.environ, FINALMOUSE_LAUNCH_RECORD=str(self.record),
                        PYTHONDONTWRITEBYTECODE="1")
        self.project.joinpath("finalmouse_tray.py").write_text(
            "import json, os, pathlib, sys\n"
            "pathlib.Path(os.environ['FINALMOUSE_LAUNCH_RECORD']).write_text(json.dumps({"
            "'argv':sys.argv, 'cwd':os.getcwd(), 'executable':sys.executable}), encoding='utf-8')\n",
            encoding="utf-8",
        )

    def environment(self):
        venv.EnvBuilder(with_pip=False).create(self.project / ".venv")

    def batch(self, name, *arguments):
        return subprocess.run(
            [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "call",
             str(self.project / name), *arguments],
            cwd=self.caller, env=self.env, input="\n", capture_output=True, text=True,
            timeout=20, creationflags=NO_WINDOW,
        )

    def script(self, *arguments):
        return subprocess.run(
            ["cscript.exe", "//nologo", str(self.project / "finalmouse_tray_silent.vbs"),
             *arguments],
            cwd=self.caller, env=self.env, capture_output=True, text=True,
            timeout=10, creationflags=NO_WINDOW,
        )

    def assert_record(self, arguments):
        deadline = time.monotonic() + 5
        while not self.record.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.record.exists(), "Fixture Python did not start")
        for attempt in range(20):
            try:
                data = json.loads(self.record.read_text(encoding="utf-8"))
                break
            except json.JSONDecodeError:
                if attempt == 19:
                    raise
                time.sleep(0.01)
        self.assertEqual(data["argv"], [str(self.project / "finalmouse_tray.py"), *arguments])
        self.assertEqual(Path(data["cwd"]), self.project)
        self.assertEqual(Path(data["executable"]), self.project / ".venv" / "Scripts" / "pythonw.exe")

    def test_batch_launch_is_absolute_and_native_by_default(self):
        self.environment()
        result = self.batch("start.bat")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_record([])

    def test_batch_rejects_removed_browser_mode_without_launching(self):
        self.environment()
        result = self.batch("start.bat", "--browser")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Usage:", result.stderr)
        self.assertFalse(self.record.exists())

    def test_direct_vbs_launch_uses_its_own_folder(self):
        self.environment()
        result = subprocess.run(
            ["wscript.exe", "//B", "//nologo",
             str(self.project / "finalmouse_tray_silent.vbs")],
            cwd=self.caller, env=self.env, capture_output=True, text=True,
            timeout=10, creationflags=NO_WINDOW,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_record([])

    def test_missing_environment_is_actionable_and_does_not_launch(self):
        for invoke in (lambda: self.batch("start.bat"), self.script):
            with self.subTest(invoke=invoke):
                result = invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("install.bat", result.stdout + result.stderr)
                self.assertFalse(self.record.exists())

    def test_unknown_or_extra_arguments_are_rejected_without_launch(self):
        self.environment()
        for arguments in (("--unknown",), ("--browser",), ("--browser", "extra"),
                          ('--browser & echo unexpected',), ('--browser"',)):
            with self.subTest(arguments=arguments):
                result = self.script(*arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Usage:", result.stderr)
                self.assertFalse(self.record.exists())

    def stub_installer(self):
        self.project.joinpath("requirements.txt").write_text("# fixture only\n", encoding="utf-8")
        self.pip_record = self.base / "pip.json"
        self.env["FINALMOUSE_PIP_RECORD"] = str(self.pip_record)
        self.project.joinpath("pip.py").write_text(
            "import json, os, pathlib, sys\n"
            "pathlib.Path(os.environ['FINALMOUSE_PIP_RECORD']).write_text(json.dumps({"
            "'argv':sys.argv[1:], 'prefix':sys.prefix, 'cwd':os.getcwd()}), encoding='utf-8')\n"
            "sys.exit(int(os.environ.get('FINALMOUSE_PIP_EXIT', '0')))\n",
            encoding="utf-8",
        )

    def test_installer_reuses_existing_environment_and_targets_its_requirements(self):
        self.environment()
        self.stub_installer()
        marker = self.project / ".venv" / "preserve-me.txt"
        marker.write_text("preserved", encoding="utf-8")
        result = self.batch("install.bat")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(self.pip_record.read_text(encoding="utf-8"))
        self.assertEqual(data["argv"], ["install", "--disable-pip-version-check", "-r",
                                        str(self.project / "requirements.txt")])
        self.assertEqual(Path(data["prefix"]), self.project / ".venv")
        self.assertEqual(Path(data["cwd"]), self.project)
        self.assertEqual(marker.read_text(encoding="utf-8"), "preserved")

    def test_installer_preserves_incomplete_environment_and_reports_failure(self):
        self.stub_installer()
        environment = self.project / ".venv"
        environment.mkdir()
        marker = environment / "preserve-me.txt"
        marker.write_text("preserved", encoding="utf-8")
        result = self.batch("install.bat")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("preserved", result.stdout)
        self.assertFalse(self.pip_record.exists())
        self.assertEqual(marker.read_text(encoding="utf-8"), "preserved")

    def test_installer_propagates_dependency_failure(self):
        self.environment()
        self.stub_installer()
        self.env["FINALMOUSE_PIP_EXIT"] = "7"
        result = self.batch("install.bat")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Dependency installation failed", result.stdout)

    def stub_runtimes(self, selected):
        binaries = self.base / "fixture tools"
        binaries.mkdir()
        runtime_log = self.base / "runtime.jsonl"
        self.env["FINALMOUSE_RUNTIME_LOG"] = str(runtime_log)
        self.env["FINALMOUSE_FAKE_SELECTION"] = selected
        self.env["PYLAUNCHER_ALLOW_INSTALL"] = "1"
        self.env["PYTHON_MANAGER_AUTOMATIC_INSTALL"] = "1"
        self.env["PATH"] = str(binaries) + os.pathsep + self.env.get("PATH", "")
        runtime = binaries / "runtime.py"
        runtime.write_text(
            "import json, os, pathlib, sys, venv\n"
            "kind, *args=sys.argv[1:]\n"
            "selected=os.environ['FINALMOUSE_FAKE_SELECTION']\n"
            "assert not os.environ.get('PYLAUNCHER_ALLOW_INSTALL')\n"
            "assert os.environ.get('PYTHON_MANAGER_AUTOMATIC_INSTALL') == '0'\n"
            "with open(os.environ['FINALMOUSE_RUNTIME_LOG'], 'a', encoding='utf-8') as log:\n"
            "    log.write(json.dumps({'launcher':kind,'args':args}) + '\\n')\n"
            "if args == ['-0p']:\n"
            "    if selected == 'py311': print(' -V:3.11 fixture-installed-python')\n"
            "    elif selected == 'py3': print(' -V:3.12 fixture-installed-python')\n"
            "    sys.exit(0)\n"
            "selector=args[0] if kind == 'py' else 'python'\n"
            "expected={'py311':'-3.11','py3':'-3','python':'python'}.get(selected)\n"
            "if selector != expected: sys.exit(1)\n"
            "remaining=args[1:] if kind == 'py' else args\n"
            "if remaining[:2] == ['-m', 'venv']:\n"
            "    venv.EnvBuilder(with_pip=False).create(remaining[2])\n"
            "elif remaining[:2] != ['-I', '-c']:\n"
            "    sys.exit(1)\n",
            encoding="utf-8",
        )
        for kind in ("py", "python"):
            binaries.joinpath(kind + ".cmd").write_text(
                f'@"{sys.executable}" "{runtime}" {kind} %*\n@exit /b %ERRORLEVEL%\n',
                encoding="utf-8")
        return runtime_log

    def test_installer_selects_registered_python_311_and_creates_only_local_venv(self):
        self.stub_installer()
        runtime_log = self.stub_runtimes("py311")
        result = self.batch("install.bat")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [json.loads(line) for line in runtime_log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(calls[0], {"launcher": "py", "args": ["-0p"]})
        self.assertEqual(calls[1]["args"][:3], ["-3.11", "-I", "-c"])
        self.assertEqual(calls[2], {"launcher": "py", "args":
                                  ["-3.11", "-m", "venv", str(self.project / ".venv")]})
        self.assertTrue(self.pip_record.exists())

    def test_installer_falls_back_to_compatible_registered_python3(self):
        self.stub_installer()
        runtime_log = self.stub_runtimes("py3")
        result = self.batch("install.bat")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [json.loads(line) for line in runtime_log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(calls[-1], {"launcher": "py", "args":
                                   ["-3", "-m", "venv", str(self.project / ".venv")]})

    def test_installer_falls_back_to_compatible_path_python(self):
        self.stub_installer()
        runtime_log = self.stub_runtimes("python")
        result = self.batch("install.bat")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [json.loads(line) for line in runtime_log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(calls[-1], {"launcher": "python", "args":
                                   ["-m", "venv", str(self.project / ".venv")]})

    def test_installer_without_compatible_python_fails_without_creating_environment(self):
        self.stub_installer()
        self.stub_runtimes("none")
        result = self.batch("install.bat")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("64-bit Python 3.10", result.stdout)
        self.assertFalse(self.project.joinpath(".venv").exists())
        self.assertFalse(self.pip_record.exists())


if __name__ == "__main__":
    unittest.main()
