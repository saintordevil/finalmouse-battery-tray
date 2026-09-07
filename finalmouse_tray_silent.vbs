' Launch the project-local environment, then exit without a resident helper.
Option Explicit
Dim shell, files, scriptDir, pythonw, appScript, command, launchError
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
scriptDir = files.GetParentFolderName(WScript.ScriptFullName)
pythonw = files.BuildPath(scriptDir, ".venv\Scripts\pythonw.exe")
appScript = files.BuildPath(scriptDir, "finalmouse_tray.py")

If WScript.Arguments.Count <> 0 Then Fail "Usage: start.bat (no arguments)"
If Not files.FileExists(pythonw) Then
    Fail "Local Python environment is missing. Run " & files.BuildPath(scriptDir, "install.bat") & " first."
End If
If Not files.FileExists(appScript) Then
    Fail "finalmouse_tray.py is missing. Restore the complete downloaded project folder."
End If

shell.CurrentDirectory = scriptDir
command = Chr(34) & pythonw & Chr(34) & " " & Chr(34) & appScript & Chr(34)
On Error Resume Next
shell.Run command, 0, False
launchError = Err.Number
On Error GoTo 0
If launchError <> 0 Then Fail "Could not start Finalmouse Battery Tray. Run install.bat to check the local environment."
WScript.Quit 0

Sub Fail(message)
    If LCase(files.GetFileName(WScript.FullName)) = "cscript.exe" Then
        WScript.StdErr.WriteLine message
    Else
        WScript.Echo message
    End If
    WScript.Quit 1
End Sub
