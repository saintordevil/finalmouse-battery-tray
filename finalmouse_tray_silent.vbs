' Launches finalmouse_tray.py silently (no console window)
Set WshShell = CreateObject("WScript.Shell")
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = scriptDir
WshShell.Run "pythonw """ & scriptDir & "\finalmouse_tray.py""", 0, False
