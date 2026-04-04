"""
Finalmouse ULX Battery Tray Monitor
Displays battery percentage in the Windows system tray.
Reads from xpanel.finalmouse.com via a hidden Chrome instance.
"""
import os
import sys
import subprocess
import time
import json
import threading
import ctypes
import ctypes.wintypes
import atexit
import math
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
import pystray
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

POLL_INTERVAL = 10  # seconds between polls (low cost — just reads DOM)
NO_WINDOW = subprocess.CREATE_NO_WINDOW
DATA_DIR = os.path.join(os.environ["LOCALAPPDATA"], "finalmouse-tray")
CHROME_PROFILE_DIR = os.path.join(DATA_DIR, "chrome-isolated")
XPANEL_URL = "https://xpanel.finalmouse.com/overview"
LOCK_FILE = os.path.join(DATA_DIR, "tray.lock")
PID_FILE = os.path.join(DATA_DIR, "chrome.pids")
CHARGE_LOG = os.path.join(DATA_DIR, "charge_log.json")


def load_charge_log():
    """Load the last-charged info from disk."""
    try:
        with open(CHARGE_LOG, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_charge_log(data):
    """Save last-charged info to disk."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CHARGE_LOG, "w") as f:
        json.dump(data, f)


def create_battery_icon(percent_text, color=(255, 255, 255, 255)):
    """Create a system tray icon with large, readable battery percentage."""
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    display_text = percent_text.replace("%", "").strip()
    if not display_text or display_text == "...":
        display_text = "--"

    font = None
    for font_size in range(200, 40, -4):
        for font_name in ["segoeuib.ttf", "arialbd.ttf", "calibrib.ttf",
                          "segoeui.ttf", "arial.ttf", "calibri.ttf"]:
            try:
                font = ImageFont.truetype(font_name, font_size)
                break
            except OSError:
                continue
        if font is None:
            font = ImageFont.load_default()
            break
        bbox = draw.textbbox((0, 0), display_text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        if text_w <= size - 10 and text_h <= size - 10:
            break

    bbox = draw.textbbox((0, 0), display_text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (size - text_w) // 2 - bbox[0]
    y = (size - text_h) // 2 - bbox[1]

    draw.text((x, y), display_text, fill=color, font=font)
    return img


def acquire_lock():
    """Prevent duplicate instances."""
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r") as f:
                old_pid = int(f.read().strip())
            result = subprocess.run(["tasklist", "/FI", f"PID eq {old_pid}"],
                                    capture_output=True, text=True,
                                    creationflags=NO_WINDOW)
            if str(old_pid) in result.stdout:
                return False
        except (ValueError, OSError):
            pass
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    return True


def release_lock():
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


def get_all_chrome_pids():
    """Get all current chrome.exe PIDs."""
    pids = set()
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
            creationflags=NO_WINDOW
        )
        for line in result.stdout.strip().split("\n"):
            parts = line.strip().strip('"').split('","')
            if len(parts) >= 2 and parts[1].isdigit():
                pids.add(int(parts[1]))
    except Exception:
        pass
    return pids


def hide_windows_by_pid(pids):
    """Hide all windows belonging to the given PIDs from taskbar and screen."""
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
            user32.ShowWindow(hwnd, 0)  # SW_HIDE
        return True

    user32.EnumWindows(enum_callback, 0)


class FinalmouseTray:
    def __init__(self):
        self.driver = None
        self.battery_pct = "..."
        self.running = True
        self.icon = None
        self.chrome_pids = set()
        self.is_charging = False
        self.charge_anim_thread = None
        self.last_battery_before_charge = None
        self.charge_log = load_charge_log()

    def _cleanup_previous(self):
        """Kill any leftover processes from a previous run and clean profile locks."""
        if os.path.exists(PID_FILE):
            try:
                with open(PID_FILE, "r") as f:
                    for line in f:
                        pid = line.strip()
                        if pid.isdigit():
                            subprocess.run(["taskkill", "/f", "/pid", pid],
                                           capture_output=True, timeout=3,
                                           creationflags=NO_WINDOW)
                os.remove(PID_FILE)
            except Exception:
                pass
        for lock_name in ["lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket"]:
            try:
                lock_path = os.path.join(CHROME_PROFILE_DIR, lock_name)
                if os.path.exists(lock_path):
                    os.remove(lock_path)
            except OSError:
                pass

    def _save_pids(self):
        try:
            with open(PID_FILE, "w") as f:
                for pid in self.chrome_pids:
                    f.write(f"{pid}\n")
        except OSError:
            pass

    def start_browser(self):
        """Launch a fully hidden Chrome instance pointed at Xpanel."""
        os.makedirs(CHROME_PROFILE_DIR, exist_ok=True)
        self._cleanup_previous()
        options = Options()
        options.add_argument(f"--user-data-dir={CHROME_PROFILE_DIR}")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=800,600")
        options.add_argument("--window-position=-32000,-32000")
        options.add_argument("--disable-background-timer-throttling")
        options.add_argument("--disable-renderer-backgrounding")

        try:
            pids_before = get_all_chrome_pids()
            service = webdriver.ChromeService()
            service.creation_flags = NO_WINDOW
            self.driver = webdriver.Chrome(options=options, service=service)
            time.sleep(3)

            pids_after = get_all_chrome_pids()
            self.chrome_pids = pids_after - pids_before
            self.chrome_pids.add(self.driver.service.process.pid)
            hide_windows_by_pid(self.chrome_pids)

            self.driver.get(XPANEL_URL)
            time.sleep(2)

            pids_after_nav = get_all_chrome_pids()
            self.chrome_pids = pids_after_nav - pids_before
            self.chrome_pids.add(self.driver.service.process.pid)
            hide_windows_by_pid(self.chrome_pids)
            self._save_pids()

            hider = threading.Thread(target=self._persistent_hider, daemon=True)
            hider.start()
            return True
        except Exception as e:
            print(f"Failed to start Chrome: {e}", file=sys.stderr)
            return False

    def _persistent_hider(self):
        """Keep hiding our Chrome windows in case they reappear."""
        while self.running:
            time.sleep(2)
            try:
                hide_windows_by_pid(self.chrome_pids)
            except Exception:
                pass

    def kill_chrome(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
        for pid in self.chrome_pids:
            try:
                subprocess.run(["taskkill", "/f", "/pid", str(pid)],
                               capture_output=True, timeout=3,
                               creationflags=NO_WINDOW)
            except Exception:
                pass
        try:
            os.remove(PID_FILE)
        except OSError:
            pass

    def read_battery(self):
        """Read battery from Xpanel page.
        Returns percentage string, 'charging', or None."""
        try:
            buttons = self.driver.find_elements(By.CSS_SELECTOR, "button")
            for btn in buttons:
                if btn.text.strip() == "Connect" and btn.is_displayed():
                    return "charging"

            els = self.driver.find_elements(By.CSS_SELECTOR, ".battery-text")
            for el in els:
                text = el.text.strip()
                if text and "%" in text:
                    return text

            # Fallback: search all text for a percentage pattern
            body_text = self.driver.find_element(By.TAG_NAME, "body").text
            import re
            match = re.search(r'(\d{1,3})%', body_text)
            if match:
                return match.group(0)
            return None
        except Exception:
            return None

    def _build_tooltip(self):
        """Build the tooltip string with battery + last charged info."""
        if self.is_charging:
            tip = "Finalmouse ULX: Charging"
        else:
            tip = f"Finalmouse ULX: {self.battery_pct}"

        log = self.charge_log
        if log.get("last_charge_time") and log.get("last_charge_pct"):
            tip += f"\nLast charged: {log['last_charge_time']} @{log['last_charge_pct']}"
        return tip

    def _charging_animation(self):
        """Animate the tray icon while charging — green pulse cycle."""
        frame = 0
        while self.running and self.is_charging:
            # Sine wave pulse: brightness cycles between 80 and 255
            t = (frame % 30) / 30.0  # 0.0 to 1.0 over 30 frames
            brightness = int(80 + 175 * (0.5 + 0.5 * math.sin(2 * math.pi * t)))
            color = (0, brightness, 0, 255)  # green pulse

            # Show the current battery number (or lightning bolt char)
            display = self.battery_pct if self.battery_pct != "..." else "C"
            if self.icon:
                self.icon.icon = create_battery_icon(display, color=color)
            frame += 1
            time.sleep(0.1)  # ~10 fps animation

    def _start_charging_anim(self):
        """Start the charging animation thread."""
        if self.charge_anim_thread and self.charge_anim_thread.is_alive():
            return
        self.charge_anim_thread = threading.Thread(
            target=self._charging_animation, daemon=True)
        self.charge_anim_thread.start()

    def _stop_charging_anim(self):
        """Stop the charging animation by setting is_charging=False."""
        self.is_charging = False
        # Thread will exit on its own

    def _update_icon(self, reading):
        """Update the tray icon based on battery reading."""
        if not self.icon:
            return

        if reading == "charging":
            if not self.is_charging:
                # Just started charging — record the last battery % and time
                if self.battery_pct and self.battery_pct != "...":
                    self.charge_log["last_charge_pct"] = self.battery_pct
                    self.charge_log["last_charge_time"] = datetime.now().strftime(
                        "%d/%m %I:%M%p").lower()
                    save_charge_log(self.charge_log)
                self.is_charging = True
                self._start_charging_anim()
            self.icon.title = self._build_tooltip()

        else:
            # Either we got a battery %, or None (transitional state)
            # Either way, stop charging animation if it was running
            if self.is_charging:
                self.is_charging = False
                time.sleep(0.2)  # let anim thread exit

            if reading:
                self.battery_pct = reading
                self.icon.icon = create_battery_icon(self.battery_pct)
                self.icon.title = self._build_tooltip()
            else:
                # None reading — show last known value in dim white
                self.icon.icon = create_battery_icon(
                    self.battery_pct, color=(150, 150, 150, 255))
                self.icon.title = self._build_tooltip()

    def poll_loop(self):
        """Background thread that polls battery every POLL_INTERVAL seconds."""
        time.sleep(10)
        prev_state = None  # track "charging", "battery", or None
        none_count = 0

        while self.running:
            reading = self.read_battery()

            # Determine current state
            if reading == "charging":
                cur_state = "charging"
            elif reading:
                cur_state = "battery"
            else:
                cur_state = None

            # Detect state transitions — refresh page to re-establish connection
            if prev_state and cur_state != prev_state:
                try:
                    self.driver.refresh()
                    time.sleep(5)
                    reading = self.read_battery()
                    if reading == "charging":
                        cur_state = "charging"
                    elif reading:
                        cur_state = "battery"
                    else:
                        cur_state = None
                except Exception:
                    pass

            # If we get None multiple times in a row, try refreshing
            if cur_state is None:
                none_count += 1
                if none_count >= 3:
                    try:
                        self.driver.refresh()
                        time.sleep(5)
                        reading = self.read_battery()
                        if reading == "charging":
                            cur_state = "charging"
                        elif reading:
                            cur_state = "battery"
                    except Exception:
                        pass
                    none_count = 0
            else:
                none_count = 0

            prev_state = cur_state
            self._update_icon(reading)

            for _ in range(POLL_INTERVAL * 2):
                if not self.running:
                    break
                time.sleep(0.5)

    def on_quit(self, icon, item):
        self.running = False
        self.is_charging = False
        icon.stop()

    def on_refresh(self, icon, item):
        reading = self.read_battery()
        self._update_icon(reading)

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

        initial_icon = create_battery_icon("...")
        menu = pystray.Menu(
            pystray.MenuItem("Refresh", self.on_refresh),
            pystray.MenuItem("Quit", self.on_quit),
        )
        self.icon = pystray.Icon(
            "finalmouse-battery",
            initial_icon,
            "Finalmouse ULX: Loading...",
            menu,
        )

        poll_thread = threading.Thread(target=self.poll_loop, daemon=True)
        poll_thread.start()

        self.icon.run()

        self.running = False
        self.is_charging = False
        self.kill_chrome()
        release_lock()


if __name__ == "__main__":
    app = FinalmouseTray()
    app.run()
