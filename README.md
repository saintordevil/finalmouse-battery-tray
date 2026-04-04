# Finalmouse ULX Battery Tray Monitor

A lightweight Windows system tray utility that displays your Finalmouse ULX mouse battery percentage in real time.

## How It Works

The app launches a hidden Chrome instance pointed at [xpanel.finalmouse.com](https://xpanel.finalmouse.com/overview) and reads the battery level directly from the page DOM. The percentage is rendered as the system tray icon itself — no extra windows, no clutter.

- Polls battery every 10 seconds (low overhead — just reads a DOM element)
- Displays the current percentage as the tray icon with bold, readable text
- Animates a green pulse when the mouse is charging
- Tracks the last time and percentage at which charging began (shown in tooltip)
- Automatically refreshes the page on connection loss or state transitions
- Runs completely hidden — no console window, no visible browser

## Requirements

- **Windows 10/11**
- **Python 3.10+**
- **Google Chrome** installed
- Python packages:
  ```
  pip install pystray pillow selenium
  ```

## Setup

### 1. Chrome WebHID Policy (Required)

Finalmouse Xpanel uses WebHID to communicate with the mouse. Chrome requires a policy to allow this automatically (without a manual prompt in the hidden browser).

Run `setup_policy.reg` as Administrator to add the required registry key. This permits `xpanel.finalmouse.com` to access Finalmouse USB devices.

### 2. First-Time Browser Login

The app uses an isolated Chrome profile stored in `%LOCALAPPDATA%\finalmouse-tray\chrome-isolated`. On first launch, Xpanel may need you to pair/connect the mouse:

1. Temporarily edit `finalmouse_tray.py` and comment out the `--window-position=-32000,-32000` line
2. Run `start.bat`
3. Complete any pairing prompts in the Chrome window
4. Stop the app with `stop.bat`, restore the line, and relaunch — it will connect automatically from then on

## Usage

| Action | Command |
|--------|---------|
| **Start** | Double-click `start.bat` (or use `finalmouse_tray_silent.vbs` for zero windows) |
| **Stop** | Double-click `stop.bat` |

Once running, look for the battery percentage number in your system tray. Right-click the icon for **Refresh** or **Quit**.

### Run at Startup (Optional)

To launch automatically when Windows starts:

1. Press `Win + R`, type `shell:startup`, press Enter
2. Create a shortcut to `finalmouse_tray_silent.vbs` in that folder

## File Overview

| File | Purpose |
|------|---------|
| `finalmouse_tray.py` | Main application — tray icon, browser control, battery polling |
| `start.bat` | Launches the tray app minimized |
| `stop.bat` | Kills the tray app and any associated Chrome processes |
| `finalmouse_tray_silent.vbs` | Launches the app with no console window at all |
| `setup_policy.reg` | Registry policy to allow WebHID access for Finalmouse devices |

## License

MIT
