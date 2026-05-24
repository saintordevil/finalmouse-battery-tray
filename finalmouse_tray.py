"""
Finalmouse ULX Battery Tray Monitor
Displays battery percentage in the Windows system tray.
Reads from xpanel.finalmouse.com via a hidden Chrome instance.
"""
import atexit
import ctypes
import ctypes.wintypes
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime

import pystray
from PIL import Image, ImageDraw, ImageFont
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

POLL_INTERVAL = 10
HIDER_INTERVAL = 5
PID_REFRESH_INTERVAL_SECONDS = 60
WATCHDOG_INTERVAL_SECONDS = 30
WATCHDOG_STALE_SECONDS = 90
PAGE_REFRESH_INTERVAL_SECONDS = 60
STARTUP_SETTLE_SECONDS = 3
REFRESH_SETTLE_SECONDS = 5
CHARGING_ANIMATION_INTERVAL = 0.5
MIN_CHARGE_RECORD_SECONDS = 45
MAX_PENDING_CHARGE_SECONDS = 12 * 60 * 60
MIN_CHARGE_DELTA_PERCENT = 1
WEBDRIVER_COMMAND_TIMEOUT_SECONDS = 15
RESTART_COOLDOWN_SECONDS = 30
RESTART_WINDOW_SECONDS = 300
MAX_RESTARTS_PER_WINDOW = 4

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

FONT_CANDIDATES = [
    "segoeuib.ttf",
    "arialbd.ttf",
    "calibrib.ttf",
    "segoeui.ttf",
    "arial.ttf",
    "calibri.ttf",
]
RESAMPLE_LANCZOS = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x00000102


class QuietChromeService(webdriver.ChromeService):
    def command_line_args(self):
        return [
            arg
            for arg in super().command_line_args()
            if arg != "--enable-chrome-logs"
        ]


def load_json_file(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else fallback
    except (OSError, json.JSONDecodeError):
        return fallback


def save_json_file(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)


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
        os.makedirs(DATA_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{timestamp} {message}\n")
    except OSError:
        pass


def parse_percent(reading):
    if reading is None or reading == BROWSER_ERROR or reading == "charging":
        return None
    text = str(reading).strip()
    match = re.search(r"\b(\d{1,3})\s*%", text)
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


def battery_state(reading):
    if reading == BROWSER_ERROR:
        return None
    if reading == "charging":
        return "charging"
    if parse_percent(reading) is not None:
        return "battery"
    return None


def format_duration(seconds):
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
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


def acquire_lock():
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r", encoding="utf-8") as f:
                old_pid = int(f.read().strip())
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {old_pid}"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=NO_WINDOW,
            )
            if str(old_pid) in result.stdout:
                return False
        except (ValueError, OSError, subprocess.SubprocessError):
            pass
    if get_tray_process_pids():
        return False
    with open(LOCK_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    return True


def release_lock():
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


def normalize_for_match(value):
    return str(value or "").replace("\\", "/").lower()


def pid_is_running(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [
        ctypes.wintypes.DWORD,
        ctypes.wintypes.BOOL,
        ctypes.wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.wintypes.DWORD,
    ]
    kernel32.WaitForSingleObject.restype = ctypes.wintypes.DWORD
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    kernel32.CloseHandle.restype = ctypes.wintypes.BOOL

    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def get_process_info(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return "", ""
    command = (
        "$p=Get-CimInstance Win32_Process -Filter \"ProcessId=%d\"; "
        "if ($p) { [Console]::WriteLine($p.Name); [Console]::WriteLine($p.CommandLine) }"
    ) % pid
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=NO_WINDOW,
        )
    except Exception:
        return "", ""
    lines = result.stdout.splitlines()
    name = lines[0].strip() if lines else ""
    command_line = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
    return name, command_line


def get_process_snapshot(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return {}

    command = (
        "$p=Get-CimInstance Win32_Process -Filter \"ProcessId=%d\"; "
        "if ($p) { "
        "$p | Select-Object ProcessId,Name,ParentProcessId,CreationDate,CommandLine "
        "| ConvertTo-Json -Compress "
        "}"
    ) % pid
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=NO_WINDOW,
        )
    except Exception:
        return {}

    text = result.stdout.strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if isinstance(data, list):
        data = data[0] if data else {}
    return data if isinstance(data, dict) else {}


def get_all_chrome_pids():
    pids = set()
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=NO_WINDOW,
        )
        for line in result.stdout.strip().splitlines():
            parts = line.strip().strip('"').split('","')
            if len(parts) >= 2 and parts[1].isdigit():
                pids.add(int(parts[1]))
    except Exception:
        pass
    return pids


def is_owned_browser_pid(pid):
    name, command_line = get_process_info(pid)
    command_match = normalize_for_match(command_line)
    profile_match = normalize_for_match(CHROME_PROFILE_DIR)
    return (
        name.lower() == "chrome.exe"
        and profile_match
        and profile_match in command_match
    )


def is_tracked_driver_pid(pid, expected_creation_date=None):
    snapshot = get_process_snapshot(pid)
    if str(snapshot.get("Name", "")).lower() != "chromedriver.exe":
        return False
    if expected_creation_date is None:
        return True
    return str(snapshot.get("CreationDate", "")) == str(expected_creation_date)


def get_owned_chrome_pids():
    return {pid for pid in get_all_chrome_pids() if is_owned_browser_pid(pid)}


def taskkill_pid(pid):
    try:
        subprocess.run(
            ["taskkill", "/f", "/pid", str(int(pid))],
            capture_output=True,
            timeout=3,
            creationflags=NO_WINDOW,
        )
    except Exception:
        pass


def get_tray_process_pids(exclude_current=True):
    command = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { "
        "$_.Name -in @('python.exe','pythonw.exe') -and "
        "$_.CommandLine -match 'finalmouse_tray\\.py' "
        "} | ForEach-Object { $_.ProcessId }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=NO_WINDOW,
        )
    except Exception:
        return set()

    current_pid = os.getpid()
    pids = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.isdigit():
            continue
        pid = int(line)
        if exclude_current and pid == current_pid:
            continue
        pids.add(pid)
    return pids


def cleanup_tray_processes():
    for pid in get_tray_process_pids():
        taskkill_pid(pid)
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


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


def build_pid_entry(pid, role):
    snapshot = get_process_snapshot(pid)
    return {
        "pid": int(pid),
        "role": role,
        "creation_date": snapshot.get("CreationDate"),
    }


def cleanup_tracked_processes():
    for entry in load_pid_entries():
        try:
            pid = int(entry.get("pid"))
        except (TypeError, ValueError):
            continue
        role = entry.get("role")
        if role in {"browser", "legacy"} and is_owned_browser_pid(pid):
            taskkill_pid(pid)
        elif role == "driver" and is_tracked_driver_pid(
            pid,
            entry.get("creation_date"),
        ):
            taskkill_pid(pid)
    try:
        os.remove(PID_FILE)
    except OSError:
        pass

    for pid in get_owned_chrome_pids():
        taskkill_pid(pid)


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
    def __init__(self):
        self.driver = None
        self.running = True
        self.icon = None
        self.chrome_pids = set()
        self.browser_pids = set()
        self.driver_pid = None
        self.is_charging = False
        self.charge_anim_thread = None
        self.hider_thread = None
        self.poll_thread = None
        self.watchdog_thread = None
        self.browser_lock = threading.RLock()
        self.action_lock = threading.Lock()
        self.last_restart_attempt = 0
        self.restart_attempts = []
        now = time.monotonic()
        self.last_poll_heartbeat = now
        self.last_successful_read_at = 0
        self.last_page_refresh_at = 0
        self.last_forced_cleanup_at = 0
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

    def _migrate_charge_log(self):
        changed = False
        if self.charge_log.get("last_known_pct") is None:
            legacy_pct = parse_percent(self.charge_log.get("last_charge_pct"))
            if legacy_pct is not None:
                self.charge_log["last_known_pct"] = legacy_pct
                changed = True

        pending = self.charge_log.get("pending_charge")
        if pending and pending.get("start_pct") is None:
            self.charge_log.pop("pending_charge", None)
            changed = True
            log_event("Cleared pending charge without a start percent")
            pending = None
        if pending and self._pending_charge_age_seconds(pending) > MAX_PENDING_CHARGE_SECONDS:
            self.charge_log.pop("pending_charge", None)
            changed = True

        if changed:
            save_charge_log(self.charge_log)

    def _pending_charge_age_seconds(self, pending):
        started_at = parse_iso_datetime((pending or {}).get("started_at"))
        if not started_at:
            return MAX_PENDING_CHARGE_SECONDS + 1
        return max(0, int((datetime.now() - started_at).total_seconds()))

    def _cleanup_previous(self):
        cleanup_tracked_processes()

        for lock_name in ["lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket"]:
            try:
                lock_path = os.path.join(CHROME_PROFILE_DIR, lock_name)
                if os.path.exists(lock_path):
                    os.remove(lock_path)
            except OSError:
                pass

    def _save_pids(self):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            entries = [
                build_pid_entry(pid, "browser")
                for pid in sorted(self.browser_pids)
            ]
            if self.driver_pid:
                entries.append(build_pid_entry(self.driver_pid, "driver"))
            with open(PID_FILE, "w", encoding="utf-8") as f:
                json.dump(entries, f, indent=2, sort_keys=True)
        except OSError:
            pass

    def _track_browser_pids(self):
        self.browser_pids = get_owned_chrome_pids()
        self.chrome_pids = set(self.browser_pids)
        try:
            service_pid = self.driver.service.process.pid
        except Exception:
            service_pid = None
        self.driver_pid = service_pid
        if service_pid:
            self.chrome_pids.add(service_pid)
        self._save_pids()
        hide_windows_by_pid(self.chrome_pids)

    def _has_live_browser_process(self):
        self.browser_pids = {
            pid for pid in self.browser_pids
            if pid_is_running(pid)
        }
        if self.browser_pids:
            return True

        if not self.driver:
            return False

        self.browser_pids = get_owned_chrome_pids()
        self.chrome_pids = set(self.browser_pids)
        if self.driver_pid and pid_is_running(self.driver_pid):
            self.chrome_pids.add(self.driver_pid)
        self._save_pids()
        return bool(self.browser_pids)

    def start_browser(self):
        with self.browser_lock:
            os.makedirs(CHROME_PROFILE_DIR, exist_ok=True)
            self._cleanup_previous()
            options = Options()
            options.add_argument(f"--user-data-dir={CHROME_PROFILE_DIR}")
            options.add_argument("--no-first-run")
            options.add_argument("--no-default-browser-check")
            options.add_argument("--disable-extensions")
            options.add_argument("--disable-sync")
            options.add_argument("--disable-background-networking")
            options.add_argument("--disable-gpu")
            options.add_argument("--mute-audio")
            options.add_argument("--log-level=3")
            options.add_argument("--window-size=800,600")
            options.add_argument("--window-position=-32000,-32000")
            options.add_experimental_option("excludeSwitches", ["enable-logging"])

            try:
                service = QuietChromeService(log_output=subprocess.DEVNULL)
                service.creation_flags = NO_WINDOW
                self.driver = webdriver.Chrome(options=options, service=service)
                self.driver.command_executor.set_timeout(
                    WEBDRIVER_COMMAND_TIMEOUT_SECONDS
                )
                self.driver.set_page_load_timeout(20)
                self.driver.set_script_timeout(10)
                time.sleep(STARTUP_SETTLE_SECONDS)
                self._track_browser_pids()

                self.driver.get(XPANEL_URL)
                time.sleep(2)
                self.last_page_refresh_at = time.monotonic()
                self._track_browser_pids()

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
        last_pid_refresh = 0
        while self.running:
            time.sleep(HIDER_INTERVAL)
            try:
                now = time.monotonic()
                if now - last_pid_refresh >= PID_REFRESH_INTERVAL_SECONDS:
                    self.browser_pids |= get_owned_chrome_pids()
                    self.chrome_pids = set(self.browser_pids)
                    if self.driver_pid and pid_is_running(self.driver_pid):
                        self.chrome_pids.add(self.driver_pid)
                    self._save_pids()
                    last_pid_refresh = now
                hide_windows_by_pid(self.chrome_pids)
            except Exception:
                pass

    def _kill_chrome_locked(self):
        live_browser_pids = {
            pid for pid in self.browser_pids
            if pid_is_running(pid)
        }
        if self.driver:
            if live_browser_pids:
                try:
                    self.driver.quit()
                except Exception:
                    pass
            elif self.driver_pid and is_tracked_driver_pid(self.driver_pid):
                taskkill_pid(self.driver_pid)
            self.driver = None

        pids_to_check = set(self.chrome_pids) | live_browser_pids | get_owned_chrome_pids()
        for pid in pids_to_check:
            if is_owned_browser_pid(pid) or (
                self.driver_pid
                and pid == self.driver_pid
                and is_tracked_driver_pid(pid)
            ):
                taskkill_pid(pid)
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
        self.chrome_pids = set()
        self.browser_pids = set()
        self.driver_pid = None

    def kill_chrome(self):
        with self.browser_lock:
            self._kill_chrome_locked()

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
        with self.browser_lock:
            if not self._restart_allowed(force):
                return None
            log_event(f"Restarting browser: {reason}")
            self._kill_chrome_locked()
            if self.icon:
                self.icon.icon = create_battery_icon("...", color=self._dim_text_color())
                self.icon.title = "Finalmouse ULX: Reconnecting..."
            if not self.start_browser():
                log_event("Browser restart failed")
                return None
            time.sleep(REFRESH_SETTLE_SECONDS)
            reading = self._read_battery_locked()
            log_event(f"Browser restart reading: {reading}")
            return reading

    def _read_battery_locked(self):
        if not self.driver:
            log_event("Browser read failed: driver is not initialized")
            return BROWSER_ERROR
        try:
            els = self.driver.find_elements(By.CSS_SELECTOR, ".battery-text")
            for el in els:
                if not el.is_displayed():
                    continue
                reading = normalize_reading(el.text)
                if parse_percent(reading) is not None:
                    return reading

            buttons = self.driver.find_elements(By.CSS_SELECTOR, "button")
            for btn in buttons:
                if btn.text.strip() == "Connect" and btn.is_displayed():
                    log_event("Xpanel shows Connect without a visible battery percent; retrying")
                    return None

            body_text = self.driver.find_element(By.TAG_NAME, "body").text
            reading = normalize_reading(body_text)
            if parse_percent(reading) is not None:
                return reading
            return None
        except WebDriverException as e:
            log_event(f"Browser read failed: {e.__class__.__name__}: {str(e)[:250]}")
            return BROWSER_ERROR
        except Exception as e:
            log_event(f"Battery read failed unexpectedly: {e.__class__.__name__}: {str(e)[:250]}")
            return None

    def read_battery(self):
        with self.browser_lock:
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
        with self.browser_lock:
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

            log_event(f"Refreshing browser: {reason}")
            try:
                self.driver.refresh()
                time.sleep(REFRESH_SETTLE_SECONDS)
                self.last_page_refresh_at = time.monotonic()
                reading = self._read_battery_locked()
            except WebDriverException as e:
                log_event(f"Refresh failed: {e.__class__.__name__}: {str(e)[:250]}")
                reading = BROWSER_ERROR
            except Exception as e:
                log_event(f"Refresh failed unexpectedly: {e.__class__.__name__}: {str(e)[:250]}")
                reading = BROWSER_ERROR

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
        if pending:
            tip += (
                f"\nCharging from {format_pct(pending.get('start_pct'))}"
                f" since {short_time(pending.get('started_at'))}"
            )

        last_charge = self.charge_log.get("last_charge")
        if last_charge:
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

    def _start_charge_session(self):
        if self.charge_log.get("pending_charge"):
            return True
        start_pct = parse_percent(self.battery_pct)
        if start_pct is None:
            start_pct = self.charge_log.get("last_known_pct")
        if start_pct is None:
            log_event("Ignored charging state without a start percent; retrying battery read")
            return False
        self.charge_log["pending_charge"] = {
            "start_pct": start_pct,
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }
        save_charge_log(self.charge_log)
        log_event(f"Charge session started from {format_pct(start_pct)}")
        return True

    def _finish_charge_session(self, end_pct):
        pending = self.charge_log.get("pending_charge")
        if not pending:
            return
        ended_at = datetime.now()
        started_at = parse_iso_datetime(pending.get("started_at"))
        duration = None
        if started_at:
            duration = int((ended_at - started_at).total_seconds())
        start_pct = pending.get("start_pct")
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

    def _charging_animation(self):
        frame = 0
        while self.running and self.is_charging:
            t = (frame % 12) / 12.0
            brightness = int(80 + 175 * (0.5 + 0.5 * math.sin(2 * math.pi * t)))
            color = (0, brightness, 0, 255)
            display = self.battery_pct
            if display == "...":
                pending = self.charge_log.get("pending_charge") or {}
                display = format_pct(pending.get("start_pct"))
                if display == "unknown":
                    display = "..."
            if self.icon:
                self.icon.icon = create_battery_icon(display, color=color)
            frame += 1
            time.sleep(CHARGING_ANIMATION_INTERVAL)

    def _start_charging_anim(self):
        if self.charge_anim_thread and self.charge_anim_thread.is_alive():
            return
        self.charge_anim_thread = threading.Thread(
            target=self._charging_animation,
            daemon=True,
        )
        self.charge_anim_thread.start()

    def _update_icon(self, reading):
        if not self.icon:
            return

        if reading == "charging":
            if not self.is_charging:
                if not self._start_charge_session():
                    self.icon.icon = create_battery_icon(self.battery_pct, color=self._dim_text_color())
                    self.icon.title = self._build_tooltip()
                    return
                self.is_charging = True
                self._start_charging_anim()
            self.icon.title = self._build_tooltip()
            return

        if self.is_charging:
            self.is_charging = False
            time.sleep(0.2)

        pct = parse_percent(reading)
        if pct is not None:
            if self.charge_log.get("pending_charge"):
                self._finish_charge_session(pct)
            self.battery_pct = format_pct(pct)
            self.charge_log["last_known_pct"] = pct
            save_charge_log(self.charge_log)
            self.icon.icon = create_battery_icon(self.battery_pct, color=self._text_color())
            self.icon.title = self._build_tooltip()
            return

        self.icon.icon = create_battery_icon(self.battery_pct, color=self._dim_text_color())
        self.icon.title = self._build_tooltip()

    def poll_loop(self):
        time.sleep(POLL_INTERVAL)
        prev_state = None
        none_count = 0

        while self.running:
            self.last_poll_heartbeat = time.monotonic()
            try:
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
                    self.last_successful_read_at = time.monotonic()
                self._update_icon(reading)
            except Exception as e:
                log_event(f"Poll loop recovered from error: {e.__class__.__name__}: {str(e)[:250]}")
                try:
                    self._update_icon(None)
                except Exception:
                    pass
            finally:
                self.last_poll_heartbeat = time.monotonic()

            for _ in range(POLL_INTERVAL * 2):
                if not self.running:
                    break
                time.sleep(0.5)

    def _start_poll_thread(self):
        if self.poll_thread and self.poll_thread.is_alive():
            return
        self.poll_thread = threading.Thread(target=self.poll_loop, daemon=True)
        self.poll_thread.start()

    def _start_watchdog_thread(self):
        if self.watchdog_thread and self.watchdog_thread.is_alive():
            return
        self.watchdog_thread = threading.Thread(target=self.watchdog_loop, daemon=True)
        self.watchdog_thread.start()

    def watchdog_loop(self):
        time.sleep(WATCHDOG_INTERVAL_SECONDS)
        while self.running:
            try:
                self._watchdog_check()
            except Exception as e:
                log_event(f"Watchdog recovered from error: {e.__class__.__name__}: {str(e)[:250]}")

            for _ in range(WATCHDOG_INTERVAL_SECONDS * 2):
                if not self.running:
                    break
                time.sleep(0.5)

    def _watchdog_check(self):
        if self.poll_thread and not self.poll_thread.is_alive():
            log_event("Poll thread was not running; starting a new poll thread")
            self._start_poll_thread()

        stale_for = time.monotonic() - self.last_poll_heartbeat
        if not self.browser_lock.acquire(blocking=False):
            if stale_for > WATCHDOG_STALE_SECONDS:
                self._force_cleanup_stuck_browser(stale_for)
            else:
                log_event("Watchdog skipped because browser work is already running")
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
        def runner():
            if not self.action_lock.acquire(blocking=False):
                log_event(f"{label} skipped because another menu action is running")
                return
            try:
                target()
            finally:
                self.action_lock.release()

        threading.Thread(target=runner, daemon=True).start()

    def on_quit(self, icon, item):
        self.running = False
        self.is_charging = False
        icon.stop()

    def on_refresh(self, icon, item):
        def refresh():
            reading = self.recover_browser(
                "manual refresh",
                force_restart_on_failure=True,
            )
            self._update_icon(reading)

        self._run_menu_action("Refresh", refresh)

    def on_reconnect(self, icon, item):
        def reconnect():
            reading = self.restart_browser("manual reconnect", force=True)
            self._update_icon(reading)

        self._run_menu_action("Reconnect Browser", reconnect)

    def on_toggle_dark_text(self, icon, item):
        self.dark_text = not self.dark_text
        self.settings["dark_text"] = self.dark_text
        save_settings(self.settings)
        if self.icon:
            self.icon.icon = create_battery_icon(self.battery_pct, color=self._text_color())
            self.icon.title = self._build_tooltip()
            try:
                self.icon.update_menu()
            except Exception:
                pass

    def run(self):
        if not acquire_lock():
            print("Already running. Exiting.", file=sys.stderr)
            sys.exit(0)
        atexit.register(release_lock)
        atexit.register(self.kill_chrome)

        if not self.start_browser():
            print("Could not start browser. Exiting.", file=sys.stderr)
            release_lock()
            sys.exit(1)

        initial_icon = create_battery_icon(self.battery_pct, color=self._text_color())
        menu = pystray.Menu(
            pystray.MenuItem("Refresh", self.on_refresh),
            pystray.MenuItem("Reconnect Browser", self.on_reconnect),
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

        self.running = False
        self.is_charging = False
        self.kill_chrome()
        release_lock()


if __name__ == "__main__":
    app = FinalmouseTray()
    app.run()
