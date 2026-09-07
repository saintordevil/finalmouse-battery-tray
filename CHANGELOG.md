# Changelog

## 2026-09-07: HID-only monitoring and updated previews

### What changed

- Battery monitoring now uses native HID exclusively. Removed the browser fallback, Chrome/Selenium transport, browser process tracking and recovery, optional dependency file, WebHID registry policy, and browser setup guide.
- The controller directly manages the native receiver, polling, tray controls, animation, and saved charge history. The stop helper targets verified tray identities only and checks process creation identity through the same handle used for stopping.
- Both launchers reject arguments for the removed mode. The app and complete regression suite use one native dependency set.
- Replaced all five old previews with three supplied images showing the battery tooltip, charging details, and tray menu. The README displays the original images without resizing.
- The portable installer, startup shortcut, upgrade instructions, percentage display, wired-power bolt, text-color control, and saved-history workflow remain available.

### Why

The project now has one monitoring path and one dependency set. Removing the unused browser implementation simplifies maintenance and eliminates its recovery and process-discovery work. The previews show the current application.

### Measured impact

The final source started through the actual project-local launcher on Windows 11 with Python 3.11.9, Pillow 12.3.0, pystray 0.19.5, hidapi 0.15.0, and six 1.17.0. Selenium was absent. Values are MiB across the complete application process tree.

| Observation | Resident working set | Private committed memory |
|---|---:|---:|
| Warm operation | 38.06 | 22.14 |
| Sampled startup/run maximum, 74.05-second capture | 116.57 | 82.66 |
| Tray interpreter's observed kernel lifetime peak | 33.17 | 21.84 |
| Environment redirector's observed kernel lifetime peak | 5.96 | 1.12 |

Warm CPU use was approximately **0.0022% of the 24-logical-processor machine**, measured from process CPU-time deltas over 58.98 seconds. This is near Windows CPU-accounting resolution. Sampling used 0.1-second intervals for the first eight seconds, then one-second intervals. Startup included one temporary process-discovery helper for duplicate protection. Steady operation retained only the tray interpreter and Windows' small Python environment redirector. Both exited after shutdown.

Working set and private commit are different counters. Summed working sets may count shared pages twice. Individual lifetime peaks are not simultaneous tree peaks, and samples can miss brief spikes. These are observations on one PC, not guaranteed ceilings.

### Validation

- **All 83 tests passed in the clean native environment without Selenium.** Tests cover HID responses and timeouts, charging/history state, polling and menu concurrency, shutdown, duplicate-instance identity, local installation, launchers, and exact-process stopping.
- The real tray started through `start.bat`, remained active through the measured polling interval, and exited cleanly with its redirector. A separate real duplicate launch was rejected while the normal installed tray continued running. Task-owned validation processes were gone after cleanup.
- Saved completed-charge and pending-session data matched the restored normal tray. Source and lifecycle changes received independent review.
- All three replacement PNGs match the user-supplied files byte for byte. The old five images are removed from the current repository.
- Earlier physical checks verified cable-switch bolt behavior, fresh wireless percentage on return, receiver-only removal without false charging, reconnection, and history across restart on a ULX Prophecy Tfue Wireless, Small. The HID protocol and charge-history logic are unchanged in this cleanup. Separate physical sleep/off tests, every model/firmware combination, and a second PC were not verified.

The wired bolt indicates USB power connection, not measured charging current or a fresh wired percentage. Charge duration includes waiting for a fresh wireless reading. Multiple mice are not correlated; duplicate matching interfaces are rejected.

Earlier changes are recorded in [the native-reader and installer update](https://github.com/saintordevil/finalmouse-battery-tray/pull/2) and [the tray reliability update](https://github.com/saintordevil/finalmouse-battery-tray/pull/1).
