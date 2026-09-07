"""
Finalmouse ULX Battery Tray Monitor
Displays battery percentage in the Windows system tray.
Reads the receiver directly using Xpanel's battery protocol and conversion.
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

from native_hid import BatteryReadError, DeviceUnavailable, NativeBatteryReader

POLL_INTERVAL = 10
WATCHDOG_INTERVAL_SECONDS = 30
CHARGING_ANIMATION_INTERVAL = 1.0
MIN_CHARGE_RECORD_SECONDS = 45
MAX_PENDING_CHARGE_SECONDS = 12 * 60 * 60
MIN_CHARGE_DELTA_PERCENT = 1
LOG_MAX_BYTES = 1024 * 1024
EXPECTED_STATE_LOG_INTERVAL_SECONDS = 300

NO_WINDOW = subprocess.CREATE_NO_WINDOW
DATA_DIR = os.path.join(os.environ["LOCALAPPDATA"], "finalmouse-tray")
LOCK_FILE = os.path.join(DATA_DIR, "tray.lock")
CHARGE_LOG = os.path.join(DATA_DIR, "charge_log.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
LOG_FILE = os.path.join(DATA_DIR, "tray.log")
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
    if reading is None:
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
        "$names=@('python.exe','pythonw.exe'); "
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


class FinalmouseTray:
    """Display native receiver readings with the saved tray and history UI."""

    reconnect_label = "Reconnect Receiver"

    def __init__(self, *, reader=None):
        self.native_reader = reader if reader is not None else NativeBatteryReader()
        self.last_native_error = None
        self.last_native_error_at = 0.0
        self.running = True
        self.icon = None
        self.is_charging = False
        self.charge_anim_thread = None
        self.charge_anim_stop = threading.Event()
        self.poll_thread = None
        self.watchdog_thread = None
        self.action_threads = set()
        self.action_threads_lock = threading.Lock()
        self.reader_lock = threading.RLock()
        self.state_lock = threading.RLock()
        self.action_lock = threading.Lock()
        self.stop_event = threading.Event()
        self._display_key = None
        self._last_tooltip = None
        self._charging_frames = {}
        self.cleanup_complete = False
        self.charge_log = load_charge_log()
        self._migrate_charge_log()
        self.settings = load_settings()
        self.dark_text = bool(self.settings.get("dark_text", False))
        self.battery_pct = format_pct(self.charge_log.get("last_known_pct"))
        if self.battery_pct == "unknown":
            self.battery_pct = "..."

    def start_reader(self):
        # A missing receiver does not prevent the tray from starting.
        with self.reader_lock:
            if not self.running or self.stop_event.is_set() or self.cleanup_complete:
                return False
            log_event("Started native ULX battery reader")
            return True

    def close_reader(self):
        if self.cleanup_complete:
            return True
        with self.reader_lock:
            if not self.cleanup_complete:
                self.native_reader.close()
                self.cleanup_complete = True
        return True

    def read_battery(self):
        if not self.running or self.stop_event.is_set() or self.cleanup_complete:
            return None
        with self.reader_lock:
            if not self.running or self.stop_event.is_set() or self.cleanup_complete:
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

    def reconnect_receiver(self, reason):
        if not self.running or self.stop_event.is_set() or self.cleanup_complete:
            return None
        with self.reader_lock:
            if not self.running or self.stop_event.is_set() or self.cleanup_complete:
                return None
            self.native_reader.close()
            log_event(f"Reconnecting native receiver: {reason}")
            return self.read_battery()

    def poll_loop(self):
        while self.running and not self.stop_event.is_set():
            try:
                # Refresh/reconnect must not apply a newer sample between this
                # read and its history/icon update.
                with self.reader_lock:
                    self._update_icon(self.read_battery())
            except Exception as error:
                log_event(f"Native poll recovered from error: {error.__class__.__name__}")
            if self.stop_event.wait(POLL_INTERVAL):
                break

    def _watchdog_check(self):
        if not self.running or self.stop_event.is_set() or self.cleanup_complete:
            return
        if self.poll_thread and not self.poll_thread.is_alive():
            log_event("Native poll thread stopped; restarting polling")
            self._start_poll_thread()

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
            with self.reader_lock:
                reading = self.reconnect_receiver("manual refresh")
                self._update_icon(reading)

        self._run_menu_action("Refresh", refresh)

    def on_reconnect(self, icon, item):
        def reconnect():
            with self.reader_lock:
                reading = self.reconnect_receiver("manual reconnect")
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
        atexit.register(self.close_reader)
        try:
            if not self.start_reader():
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
            self.close_reader()
            self._join_worker_threads()


if __name__ == "__main__":
    if sys.argv[1:]:
        print("Unsupported arguments. Run finalmouse_tray.py without arguments.", file=sys.stderr)
        sys.exit(2)
    if not acquire_lock():
        print("Already running. Exiting.", file=sys.stderr)
        sys.exit(0)
    atexit.register(release_lock)
    app = FinalmouseTray()
    app.run()
