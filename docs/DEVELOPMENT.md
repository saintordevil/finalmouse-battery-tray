# Development and tests

Install the project first. Run these commands in Command Prompt, replacing the example path with your checkout location.

The native reader, integration, and Windows launcher checks use the normal dependencies:

```bat
cd /d "C:\Users\User\Desktop\Programs\finalmouse-battery-tray"
".venv\Scripts\python.exe" -B -m unittest tests.test_native_hid tests.test_finalmouse_tray.NativeIntegrationTests tests.test_launchers -v
```

The full regression suite also needs Selenium, although these tests do not launch Chrome:

```bat
cd /d "C:\Users\User\Desktop\Programs\finalmouse-battery-tray"
".venv\Scripts\python.exe" -m pip install -r requirements-browser.txt
".venv\Scripts\python.exe" -B -m unittest discover -v
```

Tests use fake HID responses and isolated application state. Windows launcher and cleanup tests start temporary recorder/helper processes. They do not start the real tray or query live hardware. Physical battery, cable-switch, disconnect, and history checks are separate. See [release validation](../CHANGELOG.md).

## Reader behavior

`native_hid.py` selects the ULX vendor interface and avoids the mouse pointer interface. It queries link status, charging status, and voltage without changing mouse settings or firmware. Percentage conversion follows Xpanel. Wired power detection checks interface presence without sending wired-device commands.

At most one receiver handle remains open. Each status query shares a 350 ms response deadline and 64-report limit between queue draining and response reading. Synchronous OS enumeration, open, write, and close calls are outside that response-time guarantee. Failed reads close the handle for rediscovery. Polling and manual actions serialize reading and applying results to prevent stale charge-history updates.

`finalmouse_tray.py` owns the UI, charge tracking, and optional browser reader. Native startup checks for verified leftovers from the isolated browser profile; its temporary discovery helpers exit. Ordinary native polling and reconnects do not launch browser helpers.

On Windows, a virtual environment can retain a small Python redirector process. Startup excludes only the current interpreter's verified redirector from duplicate detection. The named mutex and other app-instance checks remain active.

[Back to installation and usage](../README.md)
