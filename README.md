# Finalmouse Battery Tray

Windows tray monitor for Finalmouse ULX battery status. It keeps a hidden Chrome session connected to Finalmouse Xpanel and renders the battery percentage directly in the system tray.

The app is designed for a quiet daily setup: no visible browser, no console window, low polling overhead, manual recovery controls, and charge history in the tray tooltip.

## Features

| Feature | Details |
|---|---|
| Tray battery icon | Shows the current battery percentage as the tray icon |
| Consistent text size | Keeps `100` visually aligned with `0` through `99` |
| Hidden browser session | Uses an isolated Chrome profile for Xpanel |
| Manual refresh | Right-click `Refresh` reloads Xpanel and falls back to browser restart if needed |
| Scheduled refresh | Reloads Xpanel every 60 seconds so stale hidden tabs do not freeze the percentage |
| Manual reconnect | Right-click `Reconnect Browser` restarts only the app-owned browser session |
| Last charged tooltip | Shows when the mouse last finished charging, from percent, to percent, and duration |
| Text color toggle | Right-click `Dark text` to switch the tray number from white to black |
| Watchdog recovery | Restarts the isolated browser if Chrome disappears or polling gets stale |
| Restart safety | Uses locks, cooldowns, and restart limits to avoid Chrome restart loops |
| Safer cleanup | Kills only Chrome processes tied to the isolated Finalmouse profile |

## Requirements

* Windows 10 or Windows 11
* Python 3.10+
* Google Chrome
* Python packages:

```powershell
pip install pystray pillow selenium
```

## Install

Clone the repo or download the folder, then install dependencies:

```powershell
cd C:\Users\User\Desktop\Programs\finalmouse-battery-tray-github
pip install pystray pillow selenium
```

## Chrome WebHID Policy

Finalmouse Xpanel uses WebHID to talk to the mouse. Chrome needs a policy entry so the hidden browser can access the mouse without a visible permission prompt.

Run `setup_policy.reg` as Administrator before first use.

## First Run

The app stores its isolated Chrome profile in:

```text
%LOCALAPPDATA%\finalmouse-tray\chrome-isolated
```

If Xpanel needs a first-time login, pairing, or permission approval:

1. Temporarily edit `finalmouse_tray.py`.
2. Comment out the `--window-position=-32000,-32000` line.
3. Run `start.bat`.
4. Complete the Xpanel setup in Chrome.
5. Stop the app with `stop.bat`.
6. Restore the hidden window line and start the app again.

## Usage

```powershell
cd C:\Users\User\Desktop\Programs\finalmouse-battery-tray-github
.\start.bat
```

Stop the app:

```powershell
cd C:\Users\User\Desktop\Programs\finalmouse-battery-tray-github
.\stop.bat
```

For a silent launch with no console window, run:

```powershell
cd C:\Users\User\Desktop\Programs\finalmouse-battery-tray-github
wscript .\finalmouse_tray_silent.vbs
```

## Tray Menu

| Menu item | Action |
|---|---|
| `Refresh` | Reloads Xpanel, waits for a fresh read, then restarts the browser if refresh fails |
| `Reconnect Browser` | Forces a clean restart of the isolated browser session |
| `Dark text` | Toggles the tray icon number between white and black text |
| `Quit` | Stops the tray app and cleans up app-owned browser processes |

## Charge History

The tooltip tracks completed charge sessions:

```text
Last charged: 21/05 10:42pm, 54% to 100% in 1h 18m
```

While charging, it shows the active session start:

```text
Charging from 54% since 21/05 09:24pm
```

Charge and settings data are stored in:

```text
%LOCALAPPDATA%\finalmouse-tray\charge_log.json
%LOCALAPPDATA%\finalmouse-tray\settings.json
%LOCALAPPDATA%\finalmouse-tray\tray.log
```

## Reliability Notes

* Automatic recovery uses a real page refresh first.
* The hidden Xpanel tab is refreshed every 60 seconds to pick up battery changes and charging transitions.
* A visible Xpanel `Connect` state takes priority over stale battery text when deciding whether the mouse is charging.
* If Selenium reports that Chrome is gone, the app restarts the browser.
* A low-frequency watchdog checks that the poll thread and tracked Chrome process are still alive.
* If Selenium holds the browser lock too long, the watchdog cleans up only the app-owned browser processes so polling can recover.
* Automatic browser restarts are limited to 4 attempts per 5 minutes.
* Automatic browser restarts have a 30 second cooldown.
* Manual `Refresh` can force a restart if a normal page refresh does not recover the battery read.
* Menu actions are serialized so refresh and reconnect cannot fight each other.
* Cleanup verifies the isolated Chrome profile path before killing Chrome.

## File Overview

| File | Purpose |
|---|---|
| `finalmouse_tray.py` | Tray app, browser control, battery polling, charge tracking |
| `start.bat` | Starts the tray app minimized with `pythonw` |
| `stop.bat` | Stops the tray app through the PowerShell cleanup helper |
| `stop_finalmouse.ps1` | Dependency-free process cleanup for tray and app-owned Chrome processes |
| `finalmouse_tray_silent.vbs` | Starts the tray app without a console window |
| `setup_policy.reg` | Chrome WebHID policy for Finalmouse Xpanel |

## License

MIT
