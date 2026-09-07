# Changelog

## 2026-09-07: Native HID monitoring and portable Windows installation

### What changed

- Native HID is now the default battery reader. The percentage, pulsing bolt, text-color control, tray menu, and saved charge history remain available.
- Wireless status is read every 10 seconds through a strictly selected ULX vendor interface. Receiver loss preserves the last percentage and retries discovery without inferring charging.
- The distinct wired mouse interface activates the USB-power bolt when moving the cable from the receiver to the mouse. A fresh wireless reading completes the pending charge session after reconnection.
- The previous browser reader is an explicit `--browser` fallback. Selenium is installed only through the optional dependency file. Healthy fallback operation also avoids repeated process-discovery helpers and unused address-bar WebUI pages.
- `install.bat` selects installed 64-bit Python 3.10+, preferring 3.11, and creates or validates a local `.venv`. It does not download Python, alter global packages, or replace an incompatible environment.
- Both launchers use the local environment and an absolute application path. Windows' Python environment redirector is identified precisely so it is not mistaken for a second tray instance. Genuine duplicates still exit.
- The README now covers installation on other PCs, upgrades, startup shortcuts, everyday controls, and troubleshooting. Browser fallback and developer details have separate pages. Obsolete setup instructions were removed.

### Why

A browser running continuously imposed substantial overhead on a small tray utility. Direct receiver queries preserve the display and charge tracking while removing Chrome, Xpanel, browser permissions, and network access from normal operation. Explicit wired-interface handling supports cable switching, and the installer removes machine-specific Python paths.

### Measured impact

The final published source and actual `start.bat` were exercised in a clean Python 3.11.9 environment on Windows 11 with an Intel i9-12900K. Selenium was absent. Values below are MiB, measured across the application's complete process tree.

| Observation | Resident working set | Private committed memory |
|---|---:|---:|
| Warm native use, 58.19-second CPU observation | 38.89 | 22.70 |
| Sampled startup/run tree maximum, 74.23-second capture | 118.42 | 84.90 |
| Tray interpreter's kernel lifetime peak | 33.05 | 22.25 |
| Environment redirector's kernel lifetime peak | 5.96 | 1.11 |

Warm CPU use was approximately **0.0011% of the 24-logical-processor machine**, measured from timed process CPU deltas. This is near Windows CPU-accounting resolution. Sampling used 0.1-second intervals during the first eight seconds, then one-second intervals. Temporary startup process-cleanup helpers were included; steady operation retained only the tray interpreter and the small Windows environment redirector. No browser remained.

Working set and private commit are different counters. Summed working sets can double-count shared pages. Individual kernel lifetime peaks are not simultaneous tree peaks, and sampling can miss brief tree-wide spikes. These are observations on one PC, not guaranteed resource limits.

### Validation

- A clean local environment installed Pillow 12.3.0, pystray 0.19.5, hidapi 0.15.0, and six 1.17.0. Native import and 50 native/integration/launcher tests passed without Selenium.
- **All 91 final regression tests passed**, including redirector identity, genuine duplicates, native protocol, charge-state handling, browser lifecycle, launchers, and process cleanup. Browser tests use fakes and do not launch Chrome.
- The real tray started through the supplied launcher in the clean environment, remained active through the measured polling interval, and shut down cleanly with its redirector. A separate real duplicate launch exited while the normal installed tray remained running. Test processes were gone after cleanup.
- Launcher tests covered spaces and shell metacharacters in paths, unrelated working directories, explicit fallback arguments, installed-runtime selection, and preservation of incompatible environments.
- Separate physical checks on a ULX Prophecy Tfue Wireless, Small, verified cable-switch bolt behavior, return to a fresh wireless percentage, receiver-only removal without a false bolt, reconnection recovery, and history across restart. The protocol and history code remained unchanged for the final launcher check.
- The wired bolt indicates USB power, not measured charging current or a fresh wired percentage. Charge duration includes waiting for a fresh wireless reading. Multiple mice are not correlated; duplicate matching interfaces are rejected. Separate physical sleep/off tests, every model/firmware combination, a second PC, and fresh browser login/pairing were not verified.
