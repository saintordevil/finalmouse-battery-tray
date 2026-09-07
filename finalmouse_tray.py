"""
Finalmouse ULX Battery Tray Monitor
Displays battery percentage in the Windows system tray.
Reads the receiver directly using Xpanel's battery protocol and conversion.
The previous browser reader remains available with --browser.
"""
import atexit
import ctypes
import ctypes.wintypes
import functools
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime

import pystray
from PIL import Image, ImageDraw, ImageFont

webdriver = WebDriverException = Options = By = QuietChromeService = None

POLL_INTERVAL = 10
HIDER_INTERVAL = 5
PID_REFRESH_INTERVAL_SECONDS = 60
WATCHDOG_INTERVAL_SECONDS = 30
WATCHDOG_STALE_SECONDS = 90
PAGE_REFRESH_INTERVAL_SECONDS = 60
REFRESH_SETTLE_SECONDS = 5
REFRESH_MIN_SETTLE_SECONDS = 2
READ_RETRY_INTERVAL_SECONDS = 1.0
CHARGING_ANIMATION_INTERVAL = 1.0
MIN_CHARGE_RECORD_SECONDS = 45
MAX_PENDING_CHARGE_SECONDS = 12 * 60 * 60
MIN_CHARGE_DELTA_PERCENT = 1
WEBDRIVER_COMMAND_TIMEOUT_SECONDS = 15
RESTART_COOLDOWN_SECONDS = 30
RESTART_WINDOW_SECONDS = 300
MAX_RESTARTS_PER_WINDOW = 4
LOG_MAX_BYTES = 1024 * 1024
EXPECTED_STATE_LOG_INTERVAL_SECONDS = 300

NO_WINDOW = subprocess.CREATE_NO_WINDOW
DATA_DIR = os.path.join(os.environ["LOCALAPPDATA"], "finalmouse-tray")
CHROME_PROFILE_DIR = os.path.join(DATA_DIR, "chrome-isolated")
XPANEL_URL = "https://xpanel.finalmouse.com/overview"
LOCK_FILE = os.path.join(DATA_DIR, "tray.lock")
PID_FILE = os.path.join(DATA_DIR, "chrome.pids")
CHARGE_LOG = os.path.join(DATA_DIR, "charge_log.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
LOG_FILE = os.path.join(DATA_DIR, "tray.log")
BROWSER_ERROR = "__browser_error__"
CHARGING_READING = "charging"
DISCONNECTED_READING = "disconnected"

FONT_CANDIDATES = [
    "segoeuib.ttf",
    "arialbd.ttf",
    "calibrib.ttf",
    "segoeui.ttf",
    "arial.ttf",
    "calibri.ttf",
]
RESAMPLE_LANCZOS = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ALREADY_EXISTS = 183
INSTANCE_MUTEX_NAME = r"Local\FinalmouseBatteryTray"

_LOG_LOCK = threading.Lock()
_INSTANCE_MUTEX_HANDLE = None
_INSTANCE_MUTEX_KERNEL32 = None


class ProcessSnapshotError(RuntimeError):
    pass


def _load_browser_support():
    """Keep Selenium and its dependency graph out of the native tray process."""
    global webdriver, WebDriverException, Options, By, QuietChromeService
    if webdriver is not None:
        return
    from selenium import webdriver as selenium_webdriver
    from selenium.common.exceptions import WebDriverException as DriverError
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.common.by import By as SeleniumBy

    class ChromeService(selenium_webdriver.ChromeService):
        def command_line_args(self):
            return [arg for arg in super().command_line_args()
                    if arg != "--enable-chrome-logs"]

    webdriver, WebDriverException = selenium_webdriver, DriverError
    Options, By, QuietChromeService = ChromeOptions, SeleniumBy, ChromeService


def load_json_file(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else fallback
    except (OSError, json.JSONDecodeError):
        return fallback


def save_json_file(path, data):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=directory,
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


def load_charge_log():
    return load_json_file(CHARGE_LOG, {})


def save_charge_log(data):
    save_json_file(CHARGE_LOG, data)


def load_settings():
    return load_json_file(SETTINGS_FILE, {})


def save_settings(data):
    save_json_file(SETTINGS_FILE, data)


def log_event(message):
    try:
        with _LOG_LOCK:
            os.makedirs(DATA_DIR, exist_ok=True)
            if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) >= LOG_MAX_BYTES:
                backup_path = f"{LOG_FILE}.1"
                try:
                    os.remove(backup_path)
                except OSError:
                    pass
                os.replace(LOG_FILE, backup_path)
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{timestamp} {message}\n")
    except OSError:
        pass


def parse_percent(reading):
    if reading is None or reading == BROWSER_ERROR:
        return None
    text = str(reading).strip()
    if text == CHARGING_READING or text.startswith(f"{CHARGING_READING}:"):
        return None
    match = re.search(r"(?<![\d.-])(\d{1,3})\s*%", text)
    if not match and re.fullmatch(r"\d{1,3}", text):
        match = re.match(r"(\d{1,3})", text)
    if not match:
        return None
    value = int(match.group(1))
    if value < 0 or value > 100:
        return None
    return value


def format_pct(value):
    if value is None:
        return "unknown"
    try:
        return f"{int(value)}%"
    except (TypeError, ValueError):
        return "unknown"


def normalize_reading(reading):
    pct = parse_percent(reading)
    if pct is None:
        return reading
    return format_pct(pct)


def is_charging_reading(reading):
    if reading is None:
        return False
    text = str(reading).strip()
    return text == CHARGING_READING or text.startswith(f"{CHARGING_READING}:")


def charging_start_percent(reading):
    if not is_charging_reading(reading):
        return None
    text = str(reading).strip()
    if not text.startswith(f"{CHARGING_READING}:"):
        return None
    return parse_percent(text.split(":", 1)[1])


def battery_state(reading):
    if reading == BROWSER_ERROR:
        return None
    if is_charging_reading(reading):
        return "charging"
    if reading == DISCONNECTED_READING:
        return "disconnected"
    if parse_percent(reading) is not None:
        return "battery"
    return None


def format_duration(seconds):
    if seconds is None:
        return "unknown"
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError, OverflowError):
        return "unknown"
    hours, remainder = divmod(seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    if minutes:
        return f"{minutes}m"
    return "<1m"


def parse_iso_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def short_time(value):
    dt = parse_iso_datetime(value)
    if not dt:
        return "unknown"
    return dt.strftime("%d/%m %I:%M%p").lower()


@functools.lru_cache(maxsize=64)
def load_font(font_size):
    for font_name in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(font_name, font_size)
        except OSError:
            continue
    return ImageFont.load_default()


def text_size(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox, bbox[2] - bbox[0], bbox[3] - bbox[1]


def draw_centered_text(img, display_text, font, color):
    draw = ImageDraw.Draw(img)
    bbox, text_w, text_h = text_size(draw, display_text, font)
    x = (img.width - text_w) // 2 - bbox[0]
    y = (img.height - text_h) // 2 - bbox[1]
    draw.text((x, y), display_text, fill=color, font=font)


def create_charging_icon(color=(255, 255, 255, 255)):
    """Create a centered lightning bolt that stays legible at tray size."""
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bolt = [
        (145, 18),
        (64, 142),
        (116, 142),
        (93, 238),
        (192, 104),
        (135, 104),
    ]
    draw.polygon(bolt, fill=color)
    return img


def create_battery_icon(percent_text, color=(255, 255, 255, 255)):
    """Create a system tray icon with stable visual height for 0 through 100."""
    size = 256
    padding = 10
    target_width = size - padding
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    scratch = ImageDraw.Draw(img)

    display_text = str(percent_text or "").replace("%", "").strip()
    if not display_text or display_text == "...":
        display_text = "--"

    if display_text.isdigit():
        font = load_font(200)
        _, ref_w, ref_h = text_size(scratch, "99", font)
        while (ref_w > target_width or ref_h > target_width) and getattr(font, "size", 0) > 44:
            font = load_font(font.size - 4)
            _, ref_w, ref_h = text_size(scratch, "99", font)

        bbox, text_w, text_h = text_size(scratch, display_text, font)
        layer = Image.new("RGBA", (text_w + padding, text_h + padding), (0, 0, 0, 0))
        layer_draw = ImageDraw.Draw(layer)
        layer_draw.text(
            (padding // 2 - bbox[0], padding // 2 - bbox[1]),
            display_text,
            fill=color,
            font=font,
        )
        if layer.width > target_width:
            new_width = target_width
            layer = layer.resize((new_width, layer.height), RESAMPLE_LANCZOS)
        img.alpha_composite(layer, ((size - layer.width) // 2, (size - layer.height) // 2))
        return img

    font = None
    for font_size in range(200, 40, -4):
        font = load_font(font_size)
        _, text_w, text_h = text_size(scratch, display_text, font)
        if text_w <= target_width and text_h <= target_width:
            break
    draw_centered_text(img, display_text, font, color)
    return img


def read_lock_identity():
    try:
        with open(LOCK_FILE, "r", encoding="utf-8") as f:
            text = f.read().strip()
    except OSError:
        return {}
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        return data
    if text.isdigit():
        return {"pid": int(text), "legacy": True}
    return {}


def acquire_instance_mutex():
    global _INSTANCE_MUTEX_HANDLE, _INSTANCE_MUTEX_KERNEL32
    if _INSTANCE_MUTEX_HANDLE:
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p,
        ctypes.wintypes.BOOL,
        ctypes.wintypes.LPCWSTR,
    ]
    kernel32.CreateMutexW.restype = ctypes.wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, INSTANCE_MUTEX_NAME)
    if not handle:
        return False
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _INSTANCE_MUTEX_HANDLE = handle
    _INSTANCE_MUTEX_KERNEL32 = kernel32
    return True


def release_instance_mutex():
    global _INSTANCE_MUTEX_HANDLE, _INSTANCE_MUTEX_KERNEL32
    if _INSTANCE_MUTEX_HANDLE and _INSTANCE_MUTEX_KERNEL32:
        _INSTANCE_MUTEX_KERNEL32.CloseHandle(_INSTANCE_MUTEX_HANDLE)
    _INSTANCE_MUTEX_HANDLE = None
    _INSTANCE_MUTEX_KERNEL32 = None


def acquire_lock():
    if not acquire_instance_mutex():
        return False
    try:
        snapshots = get_process_snapshots()
    except ProcessSnapshotError as error:
        log_event(f"Startup process discovery failed: {error}")
        release_instance_mutex()
        return False
    if get_tray_process_pids(snapshots=snapshots):
        release_instance_mutex()
        return False
    current = snapshots.get(os.getpid(), {})
    identity = {
        "pid": os.getpid(),
        "creation_date": current.get("CreationDate"),
        "script": os.path.abspath(__file__),
    }
    try:
        save_json_file(LOCK_FILE, identity)
    except OSError:
        release_instance_mutex()
        return False
    return True


def release_lock():
    identity = read_lock_identity()
    try:
        if int(identity.get("pid", -1)) == os.getpid():
            os.remove(LOCK_FILE)
    except (OSError, TypeError, ValueError):
        pass
    release_instance_mutex()


def split_windows_command_line(command_line):
    if not command_line:
        return ()
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32
    shell32.CommandLineToArgvW.argtypes = [
        ctypes.wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_int),
    ]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.wintypes.LPWSTR)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    argc = ctypes.c_int()
    argv = shell32.CommandLineToArgvW(str(command_line), ctypes.byref(argc))
    if not argv:
        return ()
    try:
        return tuple(argv[index] for index in range(argc.value))
    finally:
        kernel32.LocalFree(ctypes.cast(argv, ctypes.c_void_p))


def normalize_path_for_match(value):
    if not value:
        return ""
    try:
        return os.path.normcase(os.path.abspath(os.path.normpath(str(value))))
    except (OSError, TypeError, ValueError):
        return ""


def command_line_option(command_line, option_name):
    option_name = str(option_name).lower()
    prefix = f"{option_name}="
    arguments = split_windows_command_line(command_line)
    for index, argument in enumerate(arguments):
        lowered = argument.lower()
        if lowered.startswith(prefix):
            return argument[len(prefix):]
        if lowered == option_name and index + 1 < len(arguments):
            return arguments[index + 1]
    return None


def command_line_has_script(command_line, script_path, allow_same_name=False):
    expected_path = normalize_path_for_match(script_path)
    expected_name = os.path.basename(expected_path)
    arguments = split_windows_command_line(command_line)
    if len(arguments) < 2:
        return False
    script_argument = arguments[1]
    if (
        os.path.isabs(script_argument)
        and normalize_path_for_match(script_argument) == expected_path
    ):
        return True
    return bool(
        allow_same_name
        and os.path.basename(script_argument).lower() == expected_name.lower()
    )


def get_process_snapshots():
    command = (
        "$ErrorActionPreference='Stop'; "
        "$names=@('chrome.exe','chromedriver.exe','python.exe','pythonw.exe'); "
        "$items=@(Get-CimInstance Win32_Process -ErrorAction Stop | "
        "Where-Object { $_.Name -in $names } | "
        "Select-Object ProcessId,Name,ParentProcessId,CreationDate,CommandLine); "
        "if ($items.Count -eq 0) { [Console]::Write('[]') } "
        "else { $items | ConvertTo-Json -Compress }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ProcessSnapshotError("Process discovery did not complete") from error
    if result.returncode != 0 or not result.stdout.strip():
        raise ProcessSnapshotError(
            f"Process discovery failed with exit code {result.returncode}"
        )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ProcessSnapshotError("Process discovery returned invalid JSON") from error
    if isinstance(data, dict):
        data = [data]
    elif not isinstance(data, list):
        raise ProcessSnapshotError("Process discovery returned an invalid payload")
    snapshots = {}
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            pid = int(item.get("ProcessId"))
        except (TypeError, ValueError):
            continue
        snapshots[pid] = item
    return snapshots


def get_process_creation_filetime(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [
        ctypes.wintypes.DWORD,
        ctypes.wintypes.BOOL,
        ctypes.wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.POINTER(ctypes.wintypes.FILETIME),
        ctypes.POINTER(ctypes.wintypes.FILETIME),
        ctypes.POINTER(ctypes.wintypes.FILETIME),
        ctypes.POINTER(ctypes.wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = ctypes.wintypes.BOOL
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created = ctypes.wintypes.FILETIME()
        exited = ctypes.wintypes.FILETIME()
        kernel = ctypes.wintypes.FILETIME()
        user = ctypes.wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)


def capture_process_creation_times(pids):
    identities = {}
    for pid in pids:
        creation_time = get_process_creation_filetime(pid)
        if creation_time is not None:
            identities[int(pid)] = creation_time
    return identities


def is_owned_browser_snapshot(snapshot):
    profile_argument = command_line_option(
        (snapshot or {}).get("CommandLine"),
        "--user-data-dir",
    )
    return (
        str((snapshot or {}).get("Name", "")).lower() == "chrome.exe"
        and bool(profile_argument)
        and normalize_path_for_match(profile_argument)
        == normalize_path_for_match(CHROME_PROFILE_DIR)
    )


def is_tracked_driver_snapshot(snapshot, expected_creation_date=None):
    if str((snapshot or {}).get("Name", "")).lower() != "chromedriver.exe":
        return False
    if not expected_creation_date:
        return False
    return str(snapshot.get("CreationDate", "")) == str(expected_creation_date)


def get_owned_chrome_pids(snapshots=None):
    snapshots = get_process_snapshots() if snapshots is None else snapshots
    return {
        pid
        for pid, snapshot in snapshots.items()
        if is_owned_browser_snapshot(snapshot)
    }


def taskkill_pid(pid):
    try:
        result = subprocess.run(
            ["taskkill", "/f", "/pid", str(int(pid))],
            capture_output=True,
            timeout=3,
            creationflags=NO_WINDOW,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return False


def get_venv_launcher_pid(snapshots):
    """Identify this interpreter's Windows venv redirector, not another app."""
    if sys.prefix == sys.base_prefix:
        return None
    current_pid = os.getpid()
    current = snapshots.get(current_pid, {})
    parent_pid = current.get("ParentProcessId")
    if not isinstance(parent_pid, int) or parent_pid != os.getppid():
        return None
    parent = snapshots.get(parent_pid, {})
    if str(parent.get("Name", "")).lower() not in {"python.exe", "pythonw.exe"}:
        return None
    current_args = split_windows_command_line(current.get("CommandLine"))
    parent_args = split_windows_command_line(parent.get("CommandLine"))
    if (
        len(current_args) < 2
        or len(parent_args) < 2
        or not os.path.isabs(parent_args[0])
        or normalize_path_for_match(parent_args[0]) != normalize_path_for_match(sys.executable)
        or parent_args[1:] != current_args[1:]
    ):
        return None
    current_created = get_process_creation_filetime(current_pid)
    parent_created = get_process_creation_filetime(parent_pid)
    if current_created is None or parent_created is None or parent_created > current_created:
        return None
    return parent_pid


def get_tray_process_pids(exclude_current=True, snapshots=None):
    snapshots = get_process_snapshots() if snapshots is None else snapshots
    current_pid = os.getpid()
    launcher_pid = get_venv_launcher_pid(snapshots) if exclude_current else None
    lock_identity = read_lock_identity()
    try:
        locked_pid = int(lock_identity.get("pid"))
    except (TypeError, ValueError):
        locked_pid = None
    expected_script = os.path.abspath(__file__)
    pids = set()
    for pid, snapshot in snapshots.items():
        name = str(snapshot.get("Name", "")).lower()
        if name not in {"python.exe", "pythonw.exe"}:
            continue
        command_line = snapshot.get("CommandLine")
        if not command_line_has_script(
            command_line,
            expected_script,
            allow_same_name=pid == locked_pid,
        ):
            continue
        if exclude_current and pid in {current_pid, launcher_pid}:
            continue
        pids.add(pid)
    return pids


def load_pid_entries():
    if not os.path.exists(PID_FILE):
        return []
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            text = f.read().strip()
    except OSError:
        return []
    if not text:
        return []

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None

    if isinstance(data, list):
        entries = []
        for item in data:
            if isinstance(item, dict) and str(item.get("pid", "")).isdigit():
                entries.append(item)
        return entries

    entries = []
    for line in text.splitlines():
        line = line.strip()
        if line.isdigit():
            entries.append({"pid": int(line), "role": "legacy"})
    return entries


def build_pid_entry(pid, role, snapshots=None):
    snapshots = get_process_snapshots() if snapshots is None else snapshots
    snapshot = snapshots.get(int(pid), {})
    return {
        "pid": int(pid),
        "role": role,
        "creation_date": snapshot.get("CreationDate"),
    }


def cleanup_tracked_processes():
    try:
        snapshots = get_process_snapshots()
    except ProcessSnapshotError as error:
        log_event(f"Browser cleanup could not inspect processes: {error}")
        return False
    entries = load_pid_entries()
    attempted_pids = set()
    for entry in entries:
        try:
            pid = int(entry.get("pid"))
        except (TypeError, ValueError):
            continue
        role = entry.get("role")
        snapshot = snapshots.get(pid, {})
        if role in {"browser", "legacy"} and is_owned_browser_snapshot(snapshot):
            taskkill_pid(pid)
            attempted_pids.add(pid)
        elif role == "driver" and is_tracked_driver_snapshot(
            snapshot,
            entry.get("creation_date"),
        ):
            taskkill_pid(pid)
            attempted_pids.add(pid)

    for pid in get_owned_chrome_pids(snapshots):
        if pid not in attempted_pids:
            taskkill_pid(pid)

    try:
        remaining_snapshots = get_process_snapshots()
    except ProcessSnapshotError as error:
        log_event(f"Browser cleanup could not verify process exit: {error}")
        return False
    remaining_browser_pids = get_owned_chrome_pids(remaining_snapshots)
    remaining_driver_pids = {
        int(entry["pid"])
        for entry in entries
        if (
            str(entry.get("pid", "")).isdigit()
            and entry.get("role") == "driver"
            and is_tracked_driver_snapshot(
                remaining_snapshots.get(int(entry["pid"]), {}),
                entry.get("creation_date"),
            )
        )
    }
    if remaining_browser_pids or remaining_driver_pids:
        return False
    try:
        os.remove(PID_FILE)
    except OSError:
        pass
    return True


def hide_windows_by_pid(pids):
    if not pids:
        return
    user32 = ctypes.windll.user32
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_APPWINDOW = 0x00040000

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def enum_callback(hwnd, lparam):
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style = (style & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
            user32.ShowWindow(hwnd, 0)
        return True

    user32.EnumWindows(enum_callback, 0)


class FinalmouseTray:
    reconnect_label = "Reconnect Browser"

    def __init__(self, *, use_browser=True):
        if use_browser:
            _load_browser_support()
        self.driver = None
        self.running = True
        self.icon = None
        self.chrome_pids = set()
        self.browser_pids = set()
        self.browser_creation_times = {}
        self.driver_pid = None
        self.driver_creation_date = None
        self.driver_creation_filetime = None
        self.is_charging = False
        self.charge_anim_thread = None
        self.charge_anim_stop = threading.Event()
        self.hider_thread = None
        self.poll_thread = None
        self.watchdog_thread = None
        self.action_threads = set()
        self.action_threads_lock = threading.Lock()
        self.browser_lock = threading.RLock()
        self.state_lock = threading.RLock()
        self.action_lock = threading.Lock()
        self.stop_event = threading.Event()
        self._display_key = None
        self._last_tooltip = None
        self._charging_frames = {}
        self.last_restart_attempt = 0
        self.restart_attempts = []
        now = time.monotonic()
        self.last_poll_heartbeat = now
        self.last_page_refresh_at = 0
        self.last_forced_cleanup_at = 0
        self.last_connect_without_percent_log_at = 0
        self.cleanup_complete = False
        self.charge_log = load_charge_log()
        self._migrate_charge_log()
        self.settings = load_settings()
        self.dark_text = bool(self.settings.get("dark_text", False))
        self.battery_pct = format_pct(self.charge_log.get("last_known_pct"))
        if self.battery_pct == "unknown":
            self.battery_pct = "..."

    def _text_color(self):
        return (0, 0, 0, 255) if self.dark_text else (255, 255, 255, 255)

    def _dim_text_color(self):
        return (70, 70, 70, 255) if self.dark_text else (150, 150, 150, 255)

    def _charging_bolt_color(self, frame):
        t = (frame % 12) / 12.0
        pulse = 0.5 + 0.5 * math.sin(2 * math.pi * t)
        if self.dark_text:
            shade = int(25 + 100 * pulse)
        else:
            shade = int(150 + 105 * pulse)
        return (shade, shade, shade, 255)

    def _migrate_charge_log(self):
        changed = False
        if self.charge_log.get("last_known_pct") is None:
            legacy_pct = parse_percent(self.charge_log.get("last_charge_pct"))
            if legacy_pct is not None:
                self.charge_log["last_known_pct"] = legacy_pct
                changed = True

        pending = self.charge_log.get("pending_charge")
        if pending is not None and not isinstance(pending, dict):
            self.charge_log.pop("pending_charge", None)
            changed = True
            log_event("Cleared malformed pending charge data")
            pending = None
        if pending is not None:
            normalized_start = self._coerce_percent(pending.get("start_pct"))
        else:
            normalized_start = None
        if pending is not None and normalized_start is None:
            self.charge_log.pop("pending_charge", None)
            changed = True
            log_event("Cleared pending charge without a start percent")
            pending = None
        elif pending is not None:
            if pending.get("start_pct") != normalized_start:
                pending["start_pct"] = normalized_start
                changed = True
        if pending is not None and normalized_start == 0:
            fallback_pct = self._last_completed_charge_pct()
            if fallback_pct not in (None, 0):
                pending["start_pct"] = fallback_pct
                self.charge_log["last_known_pct"] = fallback_pct
                changed = True
                log_event(
                    "Repaired pending charge start percent from 0% to "
                    f"{format_pct(fallback_pct)}"
                )
        if (
            pending is not None
            and self._pending_charge_age_seconds(pending) > MAX_PENDING_CHARGE_SECONDS
        ):
            self.charge_log.pop("pending_charge", None)
            changed = True

        last_charge = self.charge_log.get("last_charge")
        if last_charge is not None and not isinstance(last_charge, dict):
            self.charge_log.pop("last_charge", None)
            changed = True
            log_event("Cleared malformed completed charge data")
        elif last_charge is not None:
            for field in ("start_pct", "end_pct"):
                normalized_pct = self._coerce_percent(last_charge.get(field))
                if last_charge.get(field) != normalized_pct:
                    last_charge[field] = normalized_pct
                    changed = True
            duration = last_charge.get("duration_seconds")
            try:
                normalized_duration = max(0, int(duration))
            except (TypeError, ValueError, OverflowError):
                normalized_duration = None
            if duration != normalized_duration:
                last_charge["duration_seconds"] = normalized_duration
                changed = True
                log_event("Repaired malformed completed charge duration")

        if changed:
            save_charge_log(self.charge_log)

    def _pending_charge_age_seconds(self, pending):
        started_at = parse_iso_datetime((pending or {}).get("started_at"))
        if not started_at:
            return MAX_PENDING_CHARGE_SECONDS + 1
        now = datetime.now(started_at.tzinfo) if started_at.tzinfo else datetime.now()
        return max(0, int((now - started_at).total_seconds()))

    def _cleanup_previous(self):
        if not cleanup_tracked_processes():
            log_event("Previous tracked browser processes did not exit")
            return False

        try:
            owned_chrome_pids = get_owned_chrome_pids()
        except ProcessSnapshotError as error:
            log_event(f"Could not verify previous browser cleanup: {error}")
            return False
        if owned_chrome_pids:
            log_event("Previous app-owned Chrome processes did not exit")
            return False

        for lock_name in ["lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket"]:
            try:
                lock_path = os.path.join(CHROME_PROFILE_DIR, lock_name)
                if os.path.exists(lock_path):
                    os.remove(lock_path)
            except OSError:
                pass
        return True

    def _save_pids(self, snapshots=None):
        try:
            snapshots = get_process_snapshots() if snapshots is None else snapshots
            entries = []
            for pid in sorted(self.browser_pids):
                snapshot = snapshots.get(pid, {})
                if is_owned_browser_snapshot(snapshot):
                    entries.append(build_pid_entry(pid, "browser", snapshots))
            driver_snapshot = snapshots.get(self.driver_pid, {})
            if self.driver_pid and is_tracked_driver_snapshot(
                driver_snapshot,
                self.driver_creation_date,
            ):
                entries.append(build_pid_entry(self.driver_pid, "driver", snapshots))
            if entries != load_pid_entries():
                save_json_file(PID_FILE, entries)
        except (OSError, ProcessSnapshotError):
            pass

    def _track_browser_pids(self):
        previous_driver_pid = self.driver_pid
        previous_driver_creation_date = self.driver_creation_date
        try:
            service_pid = self.driver.service.process.pid
        except Exception:
            service_pid = None
        snapshots = get_process_snapshots()
        self.browser_pids = get_owned_chrome_pids(snapshots)
        self.browser_creation_times = capture_process_creation_times(self.browser_pids)
        self.chrome_pids = set(self.browser_pids)
        self.driver_pid = service_pid
        observed_creation_date = snapshots.get(service_pid, {}).get("CreationDate")
        if observed_creation_date:
            self.driver_creation_date = observed_creation_date
        elif service_pid == previous_driver_pid:
            self.driver_creation_date = previous_driver_creation_date
        else:
            self.driver_creation_date = None
        if service_pid:
            self.chrome_pids.add(service_pid)
        self.driver_creation_filetime = get_process_creation_filetime(service_pid)
        self._save_pids(snapshots)
        hide_windows_by_pid(self.chrome_pids)

    def _has_live_browser_process(self):
        self.browser_pids = {
            pid for pid in self.browser_pids
            if (
                pid in self.browser_creation_times
                and get_process_creation_filetime(pid)
                == self.browser_creation_times[pid]
            )
        }
        self.browser_creation_times = {
            pid: self.browser_creation_times[pid]
            for pid in self.browser_pids
        }
        if self.browser_pids:
            return True

        if not self.driver:
            return False

        snapshots = get_process_snapshots()
        self.browser_pids = get_owned_chrome_pids(snapshots)
        self.browser_creation_times = capture_process_creation_times(self.browser_pids)
        self.chrome_pids = set(self.browser_pids)
        if (
            self.driver_pid
            and self.driver_creation_filetime is not None
            and get_process_creation_filetime(self.driver_pid)
            == self.driver_creation_filetime
        ):
            self.chrome_pids.add(self.driver_pid)
        self._save_pids(snapshots)
        return bool(self.browser_pids)

    def start_browser(self):
        if self.stop_event.is_set() or not self.running:
            return False
        with self.browser_lock:
            if self.stop_event.is_set() or not self.running:
                return False
            os.makedirs(CHROME_PROFILE_DIR, exist_ok=True)
            if not self._cleanup_previous():
                return False
            options = Options()
            options.add_argument(f"--user-data-dir={CHROME_PROFILE_DIR}")
            options.add_argument("--no-first-run")
            options.add_argument("--no-default-browser-check")
            options.add_argument("--disable-extensions")
            options.add_argument("--disable-sync")
            options.add_argument("--disable-background-networking")
            # The hidden tray never uses Chrome's address-bar WebUI pages.
            options.add_argument(
                "--disable-features=WebUIOmniboxPopup,WebUIOmniboxAimPopup,"
                "WebUIOmniboxFullPopup,WebUIOmniboxPopupDebug"
            )
            options.add_argument("--disable-gpu")
            options.add_argument("--mute-audio")
            options.add_argument("--user-agent=OpenAI File Downloader, XaiImageApiFetch/1.0")
            options.add_argument("--log-level=3")
            options.add_argument("--window-size=800,600")
            options.add_argument("--window-position=-32000,-32000")
            options.add_experimental_option("excludeSwitches", ["enable-logging"])

            try:
                service = QuietChromeService(log_output=subprocess.DEVNULL)
                service.creation_flags = NO_WINDOW
                self.driver = webdriver.Chrome(options=options, service=service)
                if self.stop_event.is_set() or not self.running:
                    self._kill_chrome_locked()
                    return False
                self.driver.command_executor.client_config.timeout = (
                    WEBDRIVER_COMMAND_TIMEOUT_SECONDS
                )
                self.driver.set_page_load_timeout(20)
                self.driver.set_script_timeout(10)
                self._track_browser_pids()
                if self.stop_event.is_set() or not self.running:
                    self._kill_chrome_locked()
                    return False

                self.driver.get(XPANEL_URL)
                if self.stop_event.is_set() or not self.running:
                    self._kill_chrome_locked()
                    return False
                self.last_page_refresh_at = time.monotonic()
                self._track_browser_pids()
                if self.stop_event.is_set() or not self.running:
                    self._kill_chrome_locked()
                    return False

                if not self.hider_thread or not self.hider_thread.is_alive():
                    self.hider_thread = threading.Thread(
                        target=self._persistent_hider,
                        daemon=True,
                    )
                    self.hider_thread.start()
                log_event(f"Started browser with tracked PIDs: {sorted(self.chrome_pids)}")
                return True
            except Exception as e:
                log_event(f"Failed to start Chrome: {e}")
                print(f"Failed to start Chrome: {e}", file=sys.stderr)
                self._kill_chrome_locked()
                return False

    def _persistent_hider(self):
        last_pid_refresh = time.monotonic()
        while self.running and not self.stop_event.wait(HIDER_INTERVAL):
            try:
                now = time.monotonic()
                with self.browser_lock:
                    self.browser_pids = {
                        pid for pid in self.browser_pids
                        if (
                            pid in self.browser_creation_times
                            and get_process_creation_filetime(pid)
                            == self.browser_creation_times[pid]
                        )
                    }
                    self.browser_creation_times = {
                        pid: self.browser_creation_times[pid]
                        for pid in self.browser_pids
                    }
                    if now - last_pid_refresh >= PID_REFRESH_INTERVAL_SECONDS:
                        # The browser owns the visible windows across renderer
                        # reloads. Rediscover only after the cached identities
                        # disappear; startup, recovery and cleanup also rescan.
                        if not self.browser_pids:
                            self._has_live_browser_process()
                        last_pid_refresh = now
                    self.chrome_pids = set(self.browser_pids)
                    if (
                        self.driver_pid
                        and self.driver_creation_filetime is not None
                        and get_process_creation_filetime(self.driver_pid)
                        == self.driver_creation_filetime
                    ):
                        self.chrome_pids.add(self.driver_pid)
                    pids_to_hide = set(self.chrome_pids)
                hide_windows_by_pid(pids_to_hide)
            except Exception:
                pass

    def _kill_chrome_locked(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

        try:
            snapshots = get_process_snapshots()
        except ProcessSnapshotError as error:
            log_event(f"Browser cleanup inspection failed: {error}")
            return False
        for pid in get_owned_chrome_pids(snapshots):
            taskkill_pid(pid)
        if self.driver_pid and is_tracked_driver_snapshot(
            snapshots.get(self.driver_pid, {}),
            self.driver_creation_date,
        ):
            taskkill_pid(self.driver_pid)

        try:
            remaining_snapshots = get_process_snapshots()
        except ProcessSnapshotError as error:
            log_event(f"Browser cleanup verification failed: {error}")
            return False
        remaining_browser_pids = get_owned_chrome_pids(remaining_snapshots)
        driver_still_running = bool(
            self.driver_pid
            and is_tracked_driver_snapshot(
                remaining_snapshots.get(self.driver_pid, {}),
                self.driver_creation_date,
            )
        )
        if remaining_browser_pids or driver_still_running:
            self.browser_pids = remaining_browser_pids
            self.browser_creation_times = capture_process_creation_times(
                remaining_browser_pids
            )
            self.chrome_pids = set(remaining_browser_pids)
            if driver_still_running:
                self.chrome_pids.add(self.driver_pid)
                self.driver_creation_filetime = get_process_creation_filetime(
                    self.driver_pid
                )
            else:
                self.driver_pid = None
                self.driver_creation_date = None
                self.driver_creation_filetime = None
            self._save_pids(remaining_snapshots)
            log_event(
                "Browser cleanup left tracked processes running: "
                f"{sorted(self.chrome_pids)}"
            )
            return False
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
        self.chrome_pids = set()
        self.browser_pids = set()
        self.browser_creation_times = {}
        self.driver_pid = None
        self.driver_creation_date = None
        self.driver_creation_filetime = None
        return True

    def kill_chrome(self):
        if self.cleanup_complete:
            return True
        with self.browser_lock:
            if self.cleanup_complete:
                return True
            cleaned = self._kill_chrome_locked()
            if cleaned:
                self.cleanup_complete = True
            return cleaned

    def _restart_allowed(self, force):
        if force:
            return True
        now = time.monotonic()
        self.restart_attempts = [
            attempt for attempt in self.restart_attempts
            if now - attempt < RESTART_WINDOW_SECONDS
        ]
        if now - self.last_restart_attempt < RESTART_COOLDOWN_SECONDS:
            wait_left = int(RESTART_COOLDOWN_SECONDS - (now - self.last_restart_attempt))
            log_event(f"Browser restart skipped by cooldown, wait {wait_left}s")
            return False
        if len(self.restart_attempts) >= MAX_RESTARTS_PER_WINDOW:
            log_event("Browser restart skipped by safety limit")
            return False
        self.last_restart_attempt = now
        self.restart_attempts.append(now)
        return True

    def restart_browser(self, reason, force=False):
        if self.stop_event.is_set() or not self.running:
            return None
        with self.browser_lock:
            if self.stop_event.is_set() or not self.running:
                return None
            if not self._restart_allowed(force):
                return None
            log_event(f"Restarting browser: {reason}")
            self._kill_chrome_locked()
            if self.stop_event.is_set() or not self.running:
                return None
            with self.state_lock:
                color = self._dim_text_color()
                self._set_icon_image_locked(
                    ("reconnecting", bool(self.dark_text)),
                    lambda: create_battery_icon("...", color=color),
                )
                self._set_tooltip_text_locked("Finalmouse ULX: Reconnecting...")
            if not self.start_browser():
                log_event("Browser restart failed")
                return None
            reading = self._wait_for_battery_locked(
                REFRESH_SETTLE_SECONDS,
                minimum_wait_seconds=REFRESH_MIN_SETTLE_SECONDS,
            )
            log_event(f"Browser restart reading: {reading}")
            return reading

    def _read_battery_locked(self):
        if not self.driver:
            log_event("Browser read failed: driver is not initialized")
            return BROWSER_ERROR
        try:
            visible_reading = None
            visible_pct = None
            els = self.driver.find_elements(By.CSS_SELECTOR, ".battery-text")
            for el in els:
                if not el.is_displayed():
                    continue
                reading = normalize_reading(el.text)
                pct = parse_percent(reading)
                if pct is not None:
                    visible_reading = reading
                    visible_pct = pct
                    break

            connect_visible = False
            buttons = self.driver.find_elements(By.CSS_SELECTOR, "button")
            for btn in buttons:
                if btn.text.strip() == "Connect" and btn.is_displayed():
                    connect_visible = True
                    break

            if visible_pct is not None:
                if connect_visible and visible_pct == 0:
                    return CHARGING_READING
                return visible_reading

            body_text = self.driver.find_element(By.TAG_NAME, "body").text
            reading = normalize_reading(body_text)
            pct = parse_percent(reading)
            if pct is not None:
                if connect_visible and pct == 0:
                    return CHARGING_READING
                return reading
            if connect_visible:
                now = time.monotonic()
                last_logged = getattr(
                    self,
                    "last_connect_without_percent_log_at",
                    0,
                )
                if now - last_logged >= EXPECTED_STATE_LOG_INTERVAL_SECONDS:
                    log_event(
                        "Xpanel shows Connect without a visible battery percent; "
                        "preserving the last known percent"
                    )
                    self.last_connect_without_percent_log_at = now
                return DISCONNECTED_READING
            return None
        except WebDriverException as e:
            log_event(f"Browser read failed: {e.__class__.__name__}: {str(e)[:250]}")
            return BROWSER_ERROR
        except Exception as e:
            log_event(f"Battery read failed unexpectedly: {e.__class__.__name__}: {str(e)[:250]}")
            return None

    def _wait_for_battery_locked(self, timeout_seconds, minimum_wait_seconds=0):
        started_at = time.monotonic()
        deadline = started_at + max(0, timeout_seconds)
        initial_wait = min(max(0, minimum_wait_seconds), max(0, timeout_seconds))
        if initial_wait and self.stop_event.wait(initial_wait):
            return None
        reading = None
        while self.running and not self.stop_event.is_set():
            reading = self._read_battery_locked()
            if reading == BROWSER_ERROR:
                return reading
            if battery_state(reading):
                return reading
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.stop_event.wait(min(READ_RETRY_INTERVAL_SECONDS, remaining))
        return reading

    def read_battery(self):
        if self.stop_event.is_set() or not self.running:
            return None
        with self.browser_lock:
            if self.stop_event.is_set() or not self.running:
                return None
            if not self._has_live_browser_process():
                log_event("Browser read failed: tracked Chrome process is not running")
                return BROWSER_ERROR
            return self._read_battery_locked()

    def recover_browser(
        self,
        reason,
        force_restart=False,
        force_restart_on_failure=False,
    ):
        if self.stop_event.is_set() or not self.running:
            return None
        with self.browser_lock:
            if self.stop_event.is_set() or not self.running:
                return None
            if force_restart:
                return self.restart_browser(reason, force=True)
            if not self.driver:
                return self.restart_browser(
                    f"{reason}; browser missing",
                    force=force_restart_on_failure,
                )
            if not self._has_live_browser_process():
                return self.restart_browser(
                    f"{reason}; tracked Chrome process missing",
                    force=force_restart_on_failure,
                )

            if reason != "scheduled page refresh":
                log_event(f"Refreshing browser: {reason}")
            try:
                self.driver.refresh()
                self.last_page_refresh_at = time.monotonic()
                reading = self._wait_for_battery_locked(
                    REFRESH_SETTLE_SECONDS,
                    minimum_wait_seconds=REFRESH_MIN_SETTLE_SECONDS,
                )
            except WebDriverException as e:
                log_event(f"Refresh failed: {e.__class__.__name__}: {str(e)[:250]}")
                reading = BROWSER_ERROR
            except Exception as e:
                log_event(f"Refresh failed unexpectedly: {e.__class__.__name__}: {str(e)[:250]}")
                reading = BROWSER_ERROR

            if self.stop_event.is_set() or not self.running:
                return None
            if battery_state(reading):
                return reading
            return self.restart_browser(
                f"{reason}; refresh did not recover",
                force=force_restart_on_failure,
            )

    def _page_refresh_due(self):
        return (
            time.monotonic() - self.last_page_refresh_at
            >= PAGE_REFRESH_INTERVAL_SECONDS
        )

    def _build_tooltip(self):
        if self.is_charging:
            tip = "Finalmouse ULX: Charging"
        else:
            tip = f"Finalmouse ULX: {self.battery_pct}"

        pending = self.charge_log.get("pending_charge")
        if isinstance(pending, dict):
            tip += (
                f"\nCharging from {format_pct(pending.get('start_pct'))}"
                f" since {short_time(pending.get('started_at'))}"
            )

        last_charge = self.charge_log.get("last_charge")
        if isinstance(last_charge, dict):
            tip += (
                f"\nLast charged: {short_time(last_charge.get('ended_at'))}, "
                f"{format_pct(last_charge.get('start_pct'))} to "
                f"{format_pct(last_charge.get('end_pct'))} in "
                f"{format_duration(last_charge.get('duration_seconds'))}"
            )
        elif self.charge_log.get("last_charge_time") and self.charge_log.get("last_charge_pct"):
            tip += (
                f"\nLast charged: {self.charge_log['last_charge_time']} "
                f"@{self.charge_log['last_charge_pct']}"
            )
        return tip

    def _stored_last_known_pct(self):
        value = self.charge_log.get("last_known_pct")
        return self._coerce_percent(value)

    def _last_completed_charge_pct(self):
        last_charge = self.charge_log.get("last_charge")
        if isinstance(last_charge, dict):
            pct = self._coerce_percent(last_charge.get("end_pct"))
            if pct is not None:
                return pct
        return parse_percent(self.charge_log.get("last_charge_pct"))

    def _coerce_percent(self, value):
        try:
            pct = int(value)
        except (TypeError, ValueError):
            return None
        return pct if 0 <= pct <= 100 else None

    def _charge_session_start_percent(self, start_pct=None):
        current_pct = parse_percent(self.battery_pct)
        stored_pct = self._stored_last_known_pct()
        completed_pct = self._last_completed_charge_pct()
        if start_pct == 0:
            for pct in (current_pct, stored_pct, completed_pct):
                if pct not in (None, 0):
                    return pct
        if start_pct is not None:
            return start_pct
        for pct in (current_pct, stored_pct, completed_pct):
            if pct not in (None, 0):
                return pct
        for pct in (current_pct, stored_pct, completed_pct):
            if pct is not None:
                return pct
        return None

    def _start_charge_session(self, start_pct=None):
        pending = self.charge_log.get("pending_charge")
        if not isinstance(pending, dict):
            pending = None
        start_pct = self._charge_session_start_percent(start_pct)
        if pending:
            pending_start_pct = self._charge_session_start_percent(
                pending.get("start_pct")
            )
            if pending_start_pct != pending.get("start_pct"):
                pending["start_pct"] = pending_start_pct
                if pending_start_pct is not None:
                    self.charge_log["last_known_pct"] = pending_start_pct
                    self.battery_pct = format_pct(pending_start_pct)
                save_charge_log(self.charge_log)
            return True
        if start_pct is not None:
            self.battery_pct = format_pct(start_pct)
            self.charge_log["last_known_pct"] = start_pct
        self.charge_log["pending_charge"] = {
            "start_pct": start_pct,
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }
        save_charge_log(self.charge_log)
        if start_pct is None:
            log_event("Charge session started from unknown percent")
        else:
            log_event(f"Charge session started from {format_pct(start_pct)}")
        return True

    def _finish_charge_session(self, end_pct):
        pending = self.charge_log.get("pending_charge")
        if not isinstance(pending, dict):
            return
        end_pct = self._coerce_percent(end_pct)
        if end_pct is None:
            return
        started_at = parse_iso_datetime(pending.get("started_at"))
        ended_at = (
            datetime.now(started_at.tzinfo)
            if started_at and started_at.tzinfo
            else datetime.now()
        )
        duration = None
        if started_at:
            duration = int((ended_at - started_at).total_seconds())
        start_pct = self._coerce_percent(pending.get("start_pct"))
        if duration is not None and duration > MAX_PENDING_CHARGE_SECONDS:
            self.charge_log.pop("pending_charge", None)
            save_charge_log(self.charge_log)
            log_event(
                "Ignored stale charge session: "
                f"{format_pct(start_pct)} to {format_pct(end_pct)} "
                f"in {format_duration(duration)}"
            )
            return
        if start_pct is None:
            self.charge_log.pop("pending_charge", None)
            save_charge_log(self.charge_log)
            log_event(
                "Ignored charge session without a start percent: "
                f"unknown to {format_pct(end_pct)}"
            )
            return
        if end_pct - start_pct < MIN_CHARGE_DELTA_PERCENT:
            self.charge_log.pop("pending_charge", None)
            save_charge_log(self.charge_log)
            log_event(
                "Ignored charge session without percent increase: "
                f"{format_pct(start_pct)} to {format_pct(end_pct)}"
            )
            return
        if (
            duration is not None
            and duration < MIN_CHARGE_RECORD_SECONDS
        ):
            self.charge_log.pop("pending_charge", None)
            save_charge_log(self.charge_log)
            log_event(
                "Ignored short charge session: "
                f"{format_pct(start_pct)} to {format_pct(end_pct)} "
                f"in {format_duration(duration)}"
            )
            return
        last_charge = {
            "start_pct": start_pct,
            "end_pct": end_pct,
            "started_at": pending.get("started_at"),
            "ended_at": ended_at.isoformat(timespec="seconds"),
            "duration_seconds": duration,
        }
        self.charge_log["last_charge"] = last_charge
        self.charge_log.pop("pending_charge", None)
        save_charge_log(self.charge_log)
        log_event(
            "Charge session finished: "
            f"{format_pct(last_charge['start_pct'])} to "
            f"{format_pct(end_pct)} in {format_duration(duration)}"
        )

    def _set_icon_image_locked(self, display_key, image):
        if self.icon and display_key != self._display_key:
            self.icon.icon = image() if callable(image) else image
            self._display_key = display_key

    def _set_tooltip_locked(self):
        self._set_tooltip_text_locked(self._build_tooltip())

    def _set_tooltip_text_locked(self, tooltip):
        if self.icon and tooltip != self._last_tooltip:
            self.icon.title = tooltip
            self._last_tooltip = tooltip

    def _charging_frames_locked(self):
        theme_key = bool(self.dark_text)
        frames = self._charging_frames.get(theme_key)
        if frames is None:
            frames = tuple(
                create_charging_icon(color=self._charging_bolt_color(frame))
                for frame in range(12)
            )
            self._charging_frames = {theme_key: frames}
        return frames

    def _charging_animation(self, animation_stop, frames):
        frame = 1
        while self.running and not self.stop_event.is_set():
            if animation_stop.wait(CHARGING_ANIMATION_INTERVAL):
                return
            with self.state_lock:
                if not self.is_charging or animation_stop.is_set():
                    return
                frame_index = frame % len(frames)
                self._set_icon_image_locked(
                    ("charging", bool(self.dark_text), frame_index),
                    frames[frame_index],
                )
            frame += 1

    def _start_charging_anim(self):
        self.charge_anim_stop.set()
        animation_stop = threading.Event()
        self.charge_anim_stop = animation_stop
        frames = self._charging_frames_locked()
        self.charge_anim_thread = threading.Thread(
            target=self._charging_animation,
            args=(animation_stop, frames),
            daemon=True,
        )
        self.charge_anim_thread.start()

    def _update_icon(self, reading):
        if self.stop_event.is_set() or not self.running:
            return
        with self.state_lock:
            if self.stop_event.is_set() or not self.running or not self.icon:
                return

            if is_charging_reading(reading):
                start_pct = charging_start_percent(reading)
                if not self.is_charging:
                    self._start_charge_session(start_pct)
                    self.is_charging = True
                    frames = self._charging_frames_locked()
                    self._set_icon_image_locked(
                        ("charging", bool(self.dark_text), 0),
                        frames[0],
                    )
                    self._start_charging_anim()
                else:
                    self._start_charge_session(start_pct)
                self._set_tooltip_locked()
                return

            pct = parse_percent(reading)
            if pct is None:
                if self.is_charging:
                    self._set_tooltip_locked()
                    return
                color = self._dim_text_color()
                self._set_icon_image_locked(
                    ("battery", self.battery_pct, color),
                    lambda: create_battery_icon(self.battery_pct, color=color),
                )
                self._set_tooltip_locked()
                return

            if self.is_charging:
                self.is_charging = False
                self.charge_anim_stop.set()

            if isinstance(self.charge_log.get("pending_charge"), dict):
                self._finish_charge_session(pct)
            self.battery_pct = format_pct(pct)
            if self._stored_last_known_pct() != pct:
                self.charge_log["last_known_pct"] = pct
                save_charge_log(self.charge_log)
            color = self._text_color()
            self._set_icon_image_locked(
                ("battery", self.battery_pct, color),
                lambda: create_battery_icon(self.battery_pct, color=color),
            )
            self._set_tooltip_locked()

    def poll_loop(self):
        if self.stop_event.wait(POLL_INTERVAL):
            return
        prev_state = None
        none_count = 0

        while self.running and not self.stop_event.is_set():
            self.last_poll_heartbeat = time.monotonic()
            try:
                with self.browser_lock:
                    if self._page_refresh_due():
                        reading = self.recover_browser("scheduled page refresh")
                    else:
                        reading = self.read_battery()
                    if reading == BROWSER_ERROR:
                        reading = self.restart_browser("lost Selenium browser connection")
                        prev_state = None

                    cur_state = battery_state(reading)

                    if prev_state and cur_state and cur_state != prev_state:
                        reading = self.recover_browser("battery state changed")
                        cur_state = battery_state(reading)

                    if cur_state is None:
                        none_count += 1
                        if none_count >= 3:
                            reading = self.recover_browser("three empty battery reads")
                            cur_state = battery_state(reading)
                            none_count = 0
                    else:
                        none_count = 0

                    if cur_state is not None:
                        prev_state = cur_state
                    self._update_icon(reading)
            except Exception as e:
                log_event(
                    "Poll loop recovered from error: "
                    f"{e.__class__.__name__}: {str(e)[:250]}"
                )
            finally:
                self.last_poll_heartbeat = time.monotonic()

            if self.stop_event.wait(POLL_INTERVAL):
                break

    def _start_poll_thread(self):
        if self.stop_event.is_set() or not self.running:
            return
        if self.poll_thread and self.poll_thread.is_alive():
            return
        self.poll_thread = threading.Thread(target=self.poll_loop, daemon=True)
        self.poll_thread.start()

    def _start_watchdog_thread(self):
        if self.stop_event.is_set() or not self.running:
            return
        if self.watchdog_thread and self.watchdog_thread.is_alive():
            return
        self.watchdog_thread = threading.Thread(target=self.watchdog_loop, daemon=True)
        self.watchdog_thread.start()

    def watchdog_loop(self):
        if self.stop_event.wait(WATCHDOG_INTERVAL_SECONDS):
            return
        while self.running and not self.stop_event.is_set():
            try:
                self._watchdog_check()
            except Exception as e:
                log_event(f"Watchdog recovered from error: {e.__class__.__name__}: {str(e)[:250]}")
            if self.stop_event.wait(WATCHDOG_INTERVAL_SECONDS):
                break

    def _watchdog_check(self):
        if self.stop_event.is_set() or not self.running:
            return
        if self.poll_thread and not self.poll_thread.is_alive():
            log_event("Poll thread was not running; starting a new poll thread")
            self._start_poll_thread()

        stale_for = time.monotonic() - self.last_poll_heartbeat
        if not self.browser_lock.acquire(blocking=False):
            if stale_for > WATCHDOG_STALE_SECONDS:
                self._force_cleanup_stuck_browser(stale_for)
            return

        try:
            missing_browser = not self.driver or not self._has_live_browser_process()
            stale_poll = stale_for > WATCHDOG_STALE_SECONDS

            if not missing_browser and not stale_poll:
                return

            reasons = []
            if missing_browser:
                reasons.append("tracked Chrome process missing")
            if stale_poll:
                reasons.append(f"poll stale for {int(stale_for)}s")
            reason = "watchdog: " + ", ".join(reasons)

            if not self.action_lock.acquire(blocking=False):
                log_event(f"{reason}; skipped because a menu action is running")
                return
            try:
                reading = self.restart_browser(reason)
                self._update_icon(reading)
            finally:
                self.action_lock.release()
        finally:
            self.browser_lock.release()

    def _force_cleanup_stuck_browser(self, stale_for):
        if self.stop_event.is_set() or not self.running:
            return
        now = time.monotonic()
        if now - self.last_forced_cleanup_at < RESTART_COOLDOWN_SECONDS:
            log_event(
                "Watchdog skipped stuck-browser cleanup by cooldown, "
                f"poll stale for {int(stale_for)}s"
            )
            return
        self.last_forced_cleanup_at = now
        log_event(
            "Watchdog forcing browser cleanup to unblock Selenium, "
            f"poll stale for {int(stale_for)}s"
        )
        cleanup_tracked_processes()

    def _run_menu_action(self, label, target):
        if self.stop_event.is_set() or not self.running:
            return

        def runner():
            try:
                if self.stop_event.is_set() or not self.running:
                    return
                if not self.action_lock.acquire(blocking=False):
                    log_event(f"{label} skipped because another menu action is running")
                    return
                try:
                    if not self.stop_event.is_set() and self.running:
                        target()
                except Exception as e:
                    log_event(
                        f"{label} failed: {e.__class__.__name__}: {str(e)[:250]}"
                    )
                finally:
                    self.action_lock.release()
            finally:
                with self.action_threads_lock:
                    self.action_threads.discard(threading.current_thread())

        thread = threading.Thread(target=runner, daemon=True)
        with self.action_threads_lock:
            self.action_threads.add(thread)
        thread.start()

    def on_quit(self, icon, item):
        self._signal_stop()
        icon.stop()

    def on_refresh(self, icon, item):
        def refresh():
            with self.browser_lock:
                reading = self.recover_browser(
                    "manual refresh",
                    force_restart_on_failure=True,
                )
                self._update_icon(reading)

        self._run_menu_action("Refresh", refresh)

    def on_reconnect(self, icon, item):
        def reconnect():
            with self.browser_lock:
                reading = self.restart_browser("manual reconnect", force=True)
                self._update_icon(reading)

        self._run_menu_action(self.reconnect_label, reconnect)

    def on_toggle_dark_text(self, icon, item):
        with self.state_lock:
            self.dark_text = not self.dark_text
            self.settings["dark_text"] = self.dark_text
            save_settings(self.settings)
            if self.icon:
                if self.is_charging:
                    frames = self._charging_frames_locked()
                    self._set_icon_image_locked(
                        ("charging", bool(self.dark_text), 0),
                        frames[0],
                    )
                    self._start_charging_anim()
                else:
                    color = self._text_color()
                    self._set_icon_image_locked(
                        ("battery", self.battery_pct, color),
                        lambda: create_battery_icon(self.battery_pct, color=color),
                    )
                self._set_tooltip_locked()
        if self.icon:
            try:
                self.icon.update_menu()
            except Exception:
                pass

    def _signal_stop(self):
        with self.state_lock:
            self.running = False
            self.is_charging = False
            self.charge_anim_stop.set()
            self.stop_event.set()

    def _join_worker_threads(self, timeout_seconds=3):
        deadline = time.monotonic() + timeout_seconds
        current = threading.current_thread()
        with self.action_threads_lock:
            action_threads = tuple(self.action_threads)
        for thread in (
            self.charge_anim_thread,
            self.hider_thread,
            self.poll_thread,
            self.watchdog_thread,
            *action_threads,
        ):
            if not thread or thread is current or not thread.is_alive():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)

    def run(self):
        atexit.register(self.kill_chrome)
        try:
            if not self.start_browser():
                print("Could not initialize battery reader. Exiting.", file=sys.stderr)
                sys.exit(1)

            initial_color = self._text_color()
            initial_icon = create_battery_icon(self.battery_pct, color=initial_color)
            self._display_key = ("battery", self.battery_pct, initial_color)
            menu = pystray.Menu(
                pystray.MenuItem("Refresh", self.on_refresh),
                pystray.MenuItem(self.reconnect_label, self.on_reconnect),
                pystray.MenuItem(
                    "Dark text",
                    self.on_toggle_dark_text,
                    checked=lambda item: self.dark_text,
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self.on_quit),
            )
            self.icon = pystray.Icon(
                "finalmouse-battery",
                initial_icon,
                "Finalmouse ULX: Loading...",
                menu,
            )

            self._start_poll_thread()
            self._start_watchdog_thread()
            self.icon.run()
        finally:
            self._signal_stop()
            self.kill_chrome()
            self._join_worker_threads()


class NativeFinalmouseTray(FinalmouseTray):
    """Use the existing tray/history UI with a bounded native receiver reader."""

    reconnect_label = "Reconnect Receiver"

    def __init__(self, *, reader=None):
        super().__init__(use_browser=False)
        from native_hid import NativeBatteryReader
        self.native_reader = reader if reader is not None else NativeBatteryReader()
        self.last_native_error = None
        self.last_native_error_at = 0.0

    def start_browser(self):
        # Base run() supplies the established tray/menu/shutdown lifecycle.
        # A missing receiver does not prevent the tray from starting.
        with self.browser_lock:
            if not self.running or self.stop_event.is_set():
                return False
            # An unclean exit from the former browser mode can leave Chrome
            # holding the receiver. Reuse its verified cleanup once at startup;
            # native polling and menu actions never launch discovery helpers.
            if not self._cleanup_previous():
                return False
            log_event("Started native ULX battery reader")
            return self.running and not self.stop_event.is_set()

    def kill_chrome(self):
        if self.cleanup_complete:
            return True
        with self.browser_lock:
            if not self.cleanup_complete:
                self.native_reader.close()
                self.cleanup_complete = True
        return True

    def read_battery(self):
        from native_hid import BatteryReadError, DeviceUnavailable
        if not self.running or self.stop_event.is_set():
            return None
        with self.browser_lock:
            if not self.running or self.stop_event.is_set():
                return None
            try:
                sample = self.native_reader.read()
            except BatteryReadError as error:
                now = time.monotonic()
                reason = str(error)
                if (reason != self.last_native_error
                        or now - self.last_native_error_at >= EXPECTED_STATE_LOG_INTERVAL_SECONDS):
                    log_event(f"Native battery reader: {reason}; preserving the last known reading")
                    self.last_native_error, self.last_native_error_at = reason, now
                return DISCONNECTED_READING if isinstance(error, DeviceUnavailable) else None
            if self.last_native_error is not None:
                log_event("Native battery reader recovered")
                self.last_native_error = None
            # Radio charging status and the verified wired USB connection both
            # use the established charging UI. Wired mode has no voltage reply;
            # preserve the session's starting percentage until wireless returns.
            if sample.charging or sample.power_connected:
                return CHARGING_READING
            if (not sample.connected or sample.percent is None
                    or sample.millivolts == 0):
                return DISCONNECTED_READING
            return format_pct(sample.percent)

    def recover_browser(self, reason, force_restart=False, force_restart_on_failure=False):
        if force_restart or force_restart_on_failure:
            return self.restart_browser(reason, force=True)
        return self.read_battery()

    def restart_browser(self, reason, force=False):
        if not self.running or self.stop_event.is_set():
            return None
        with self.browser_lock:
            self.native_reader.close()
            log_event(f"Reconnecting native receiver: {reason}")
            return self.read_battery()

    def poll_loop(self):
        # Direct queries need neither page reloads nor browser-settle delays.
        while self.running and not self.stop_event.is_set():
            self.last_poll_heartbeat = time.monotonic()
            try:
                # Refresh/reconnect must not apply a newer sample between this
                # read and its history/icon update.
                with self.browser_lock:
                    self._update_icon(self.read_battery())
            except Exception as error:
                log_event(f"Native poll recovered from error: {error.__class__.__name__}")
            finally:
                self.last_poll_heartbeat = time.monotonic()
            if self.stop_event.wait(POLL_INTERVAL):
                break

    def _watchdog_check(self):
        if not self.running or self.stop_event.is_set():
            return
        if self.poll_thread and not self.poll_thread.is_alive():
            log_event("Native poll thread stopped; restarting polling")
            self._start_poll_thread()


if __name__ == "__main__":
    if not acquire_lock():
        print("Already running. Exiting.", file=sys.stderr)
        sys.exit(0)
    atexit.register(release_lock)
    app = FinalmouseTray() if "--browser" in sys.argv[1:] else NativeFinalmouseTray()
    app.run()
