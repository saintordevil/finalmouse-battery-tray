# ULX Finalmouse Battery Tray

- Preserve native HID, the existing tray UI, saved charge history, and lightweight local launcher. Keep the runtime and published package HID-only.
- Select the exact model's vendor HID interface; never query pointer or keyboard interfaces. Keep ULX and SLX state, mutexes, and stop targets separate.
- The wired bolt indicates a USB power connection, not measured charging current. Receiver absence alone does not mean charging. Finish history only after a fresh noncharging wireless percentage returns.
- For device or charge-state changes, verify real hardware where available plus disconnect recovery and saved state. Use disposable fixtures for offline tests. For lifecycle changes, include the Python environment redirector and temporary startup helper in full-tree resource checks.

ULX keeps the legacy `%LOCALAPPDATA%\finalmouse-tray` history and `Local\FinalmouseBatteryTray` mutex. Its receiver is `361D:0100`, interface 0; wired mouse `361D:0102`, interface 1.
