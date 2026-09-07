# Optional browser fallback

The default native reader is the recommended setup. The previous Xpanel reader remains available with **`--browser`**. It runs hidden, isolated Chrome, accesses Xpanel over the network, and uses substantially more resources.

## Install and launch

Stop the tray, install [Google Chrome](https://www.google.com/chrome/) if needed, and run these commands in Command Prompt. Replace the example path with your extracted project folder:

```bat
cd /d "C:\Users\User\Desktop\Programs\finalmouse-battery-tray"
".venv\Scripts\python.exe" -m pip install -r requirements-browser.txt
start.bat --browser
```

The fallback menu offers **Reconnect Browser**. It refreshes Xpanel every 60 seconds and has bounded restart recovery. To return to native mode, stop the tray and run `start.bat` without arguments.

## Browser permissions

The optional `setup_policy.reg` grants Xpanel access to listed Finalmouse USB devices through Chrome's machine-wide WebHID policy. Importing it requires administrator approval and changes `HKEY_LOCAL_MACHINE\SOFTWARE\Policies\Google\Chrome\WebHidAllowDevicesForUrls`. Review it and any existing policy first: its named entry can replace an existing entry at that location. Native mode does not use this policy.

Fresh browser pairing, login, and permission setup have not been validated for this release. The hidden fallback has no documented first-run setup wizard. If Xpanel requires interaction or the fallback remains disconnected, use native mode or inspect `%LOCALAPPDATA%\finalmouse-tray\tray.log`.

The browser profile is stored in `%LOCALAPPDATA%\finalmouse-tray\chrome-isolated`. The app's cleanup targets its verified isolated profile and process identities.

[Back to installation and usage](../README.md)
