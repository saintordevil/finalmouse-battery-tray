param(
    [string] $DataDir = (Join-Path $env:LOCALAPPDATA "finalmouse-tray"),
    [string] $ScriptPath = (Join-Path $PSScriptRoot "finalmouse_tray.py")
)

$ErrorActionPreference = "SilentlyContinue"

try {
    $createdShutdownMutex = $false
    $shutdownMutex = [System.Threading.Mutex]::new(
        $false,
        "Local\FinalmouseBatteryTray",
        [ref] $createdShutdownMutex
    )
} catch {
    [Console]::Error.WriteLine("Could not reserve the tray shutdown lock; nothing was stopped.")
    exit 2
}

$profileDir = Join-Path $DataDir "chrome-isolated"
$normalizedProfileDir = [IO.Path]::GetFullPath($profileDir).TrimEnd("\")
$normalizedScriptPath = [IO.Path]::GetFullPath($ScriptPath)
$pidFile = Join-Path $DataDir "chrome.pids"
$lockFile = Join-Path $DataDir "tray.lock"

if (-not ("FinalmouseCommandLineParser" -as [type])) {
    $parserSource = @"
using System;
using System.Runtime.InteropServices;

public static class FinalmouseCommandLineParser
{
    [DllImport("shell32.dll", SetLastError = true)]
    private static extern IntPtr CommandLineToArgvW(
        [MarshalAs(UnmanagedType.LPWStr)] string commandLine,
        out int argumentCount
    );

    [DllImport("kernel32.dll")]
    private static extern IntPtr LocalFree(IntPtr memory);

    public static string[] Parse(string commandLine)
    {
        if (String.IsNullOrWhiteSpace(commandLine)) {
            return new string[0];
        }
        int argumentCount;
        IntPtr argumentVector = CommandLineToArgvW(commandLine, out argumentCount);
        if (argumentVector == IntPtr.Zero) {
            return new string[0];
        }
        try {
            string[] arguments = new string[argumentCount];
            for (int index = 0; index < argumentCount; index++) {
                IntPtr argument = Marshal.ReadIntPtr(
                    argumentVector,
                    index * IntPtr.Size
                );
                arguments[index] = Marshal.PtrToStringUni(argument);
            }
            return arguments;
        }
        finally {
            LocalFree(argumentVector);
        }
    }
}
"@
    try {
        Add-Type -TypeDefinition $parserSource -ErrorAction Stop
    } catch {
        [Console]::Error.WriteLine("Could not load the safe command-line parser; nothing was stopped.")
        exit 2
    }
}
if (-not ("FinalmouseCommandLineParser" -as [type])) {
    [Console]::Error.WriteLine("The safe command-line parser is unavailable; nothing was stopped.")
    exit 2
}

function Get-CommandArguments {
    param([string] $CommandLine)
    try {
        return @([FinalmouseCommandLineParser]::Parse($CommandLine))
    } catch {
        return @()
    }
}

function Get-NormalizedPath {
    param([string] $Value)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $null
    }
    try {
        return [IO.Path]::GetFullPath($Value).TrimEnd("\")
    } catch {
        return $null
    }
}

function Same-CreationDate {
    param($Process, [string] $Expected)
    if ([string]::IsNullOrWhiteSpace($Expected)) {
        return $false
    }
    if (([string] $Process.CreationDate) -eq $Expected) {
        return $true
    }
    try {
        $timestamp = ([DateTimeOffset] $Process.CreationDate).ToUnixTimeMilliseconds()
        return "/Date($timestamp)/" -eq $Expected
    } catch {
        return $false
    }
}

function Test-TrayProcess {
    param($Process)
    if (-not $Process -or $Process.Name -notin @("python.exe", "pythonw.exe")) {
        return $false
    }
    $arguments = @(Get-CommandArguments -CommandLine $Process.CommandLine)
    if ($arguments.Count -lt 2 -or -not [IO.Path]::IsPathRooted($arguments[1])) {
        return $false
    }
    $candidate = Get-NormalizedPath -Value ([string] $arguments[1])
    return (
        $candidate -and
        $candidate.Equals($normalizedScriptPath, [StringComparison]::OrdinalIgnoreCase)
    )
}

function Test-LegacyTrayArgument {
    param($Process)
    if (-not $Process -or $Process.Name -notin @("python.exe", "pythonw.exe")) {
        return $false
    }
    $scriptName = [IO.Path]::GetFileName($normalizedScriptPath)
    $arguments = @(Get-CommandArguments -CommandLine $Process.CommandLine)
    if ($arguments.Count -lt 2) {
        return $false
    }
    $scriptArgument = [string] $arguments[1]
    return (
        -not [IO.Path]::IsPathRooted($scriptArgument) -and
        -not [IO.Path]::GetDirectoryName($scriptArgument) -and
        $scriptArgument.Equals($scriptName, [StringComparison]::OrdinalIgnoreCase)
    )
}

function Test-OwnedBrowserProcess {
    param($Process)
    if (-not $Process -or $Process.Name -ne "chrome.exe") {
        return $false
    }
    $arguments = @(Get-CommandArguments -CommandLine $Process.CommandLine)
    for ($index = 1; $index -lt $arguments.Count; $index++) {
        $argument = [string] $arguments[$index]
        $profileArgument = $null
        if ($argument.Equals("--user-data-dir", [StringComparison]::OrdinalIgnoreCase)) {
            if ($index + 1 -lt $arguments.Count) {
                $profileArgument = [string] $arguments[$index + 1]
            }
        } elseif ($argument.StartsWith("--user-data-dir=", [StringComparison]::OrdinalIgnoreCase)) {
            $profileArgument = $argument.Substring("--user-data-dir=".Length)
        }
        $candidate = Get-NormalizedPath -Value $profileArgument
        if ($candidate -and $candidate.Equals($normalizedProfileDir, [StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

function Get-ProcessSnapshot {
    try {
        $items = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    } catch {
        [Console]::Error.WriteLine("Could not inspect running processes; nothing was stopped.")
        exit 2
    }
    if ($items.Count -eq 0) {
        [Console]::Error.WriteLine("Process inspection returned no results; nothing was stopped.")
        exit 2
    }
    return $items
}

function Get-LockIdentity {
    if (-not (Test-Path -LiteralPath $lockFile)) {
        return $null
    }
    $text = (Get-Content -LiteralPath $lockFile -Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($text)) {
        return $null
    }
    try {
        $identity = $text | ConvertFrom-Json
        if ($identity -and $identity.pid) {
            return $identity
        }
    } catch {
    }
    if ($text -match "^\d+$") {
        return [pscustomobject]@{ pid = [int] $text; creation_date = $null }
    }
    return $null
}

$processes = @(Get-ProcessSnapshot)
$trayProcesses = @($processes | Where-Object { Test-TrayProcess -Process $_ })
$lockIdentity = Get-LockIdentity
$unverifiedLegacyTray = $false
if ($lockIdentity) {
    $lockedProcess = $processes | Where-Object { [int] $_.ProcessId -eq [int] $lockIdentity.pid } | Select-Object -First 1
    $lockScriptMatches = $false
    if (-not [string]::IsNullOrWhiteSpace([string] $lockIdentity.script) -and $lockIdentity.creation_date) {
        $lockScriptPath = Get-NormalizedPath -Value ([string] $lockIdentity.script)
        $lockScriptMatches = (
            $lockScriptPath -and
            $lockScriptPath.Equals($normalizedScriptPath, [StringComparison]::OrdinalIgnoreCase)
        )
    }
    $lockedProcessMatches = (
        (Test-TrayProcess -Process $lockedProcess) -or
        (
            $lockScriptMatches -and
            (Test-LegacyTrayArgument -Process $lockedProcess)
        )
    )
    if (
        [string]::IsNullOrWhiteSpace([string] $lockIdentity.creation_date) -and
        (Test-LegacyTrayArgument -Process $lockedProcess)
    ) {
        $unverifiedLegacyTray = $true
    }
    if (
        $lockedProcessMatches -and
        (
            [string]::IsNullOrWhiteSpace([string] $lockIdentity.creation_date) -or
            (Same-CreationDate -Process $lockedProcess -Expected ([string] $lockIdentity.creation_date))
        )
    ) {
        $trayProcesses += $lockedProcess
    }
}
if ($unverifiedLegacyTray) {
    [Console]::Error.WriteLine(
        "A legacy relative tray launch is still running. Its identity cannot be verified safely; tracking files were preserved."
    )
    exit 3
}

$trayIdentities = @(
    $trayProcesses |
        Sort-Object ProcessId -Unique |
        ForEach-Object {
            [pscustomobject]@{
                pid = [int] $_.ProcessId
                creation_date = [string] $_.CreationDate
            }
        }
)
$trayPids = @($trayIdentities | Select-Object -ExpandProperty pid)
foreach ($trayPid in $trayPids) {
    Stop-Process -Id ([int] $trayPid) -Force
}
foreach ($trayPid in $trayPids) {
    Wait-Process -Id ([int] $trayPid) -Timeout 5
}

$processes = @(Get-ProcessSnapshot)
$traySurvivors = @(
    foreach ($identity in $trayIdentities) {
        $process = $processes | Where-Object { [int] $_.ProcessId -eq $identity.pid } | Select-Object -First 1
        if (
            $process -and
            (
                (Same-CreationDate -Process $process -Expected $identity.creation_date) -or
                (
                    [string]::IsNullOrWhiteSpace($identity.creation_date) -and
                    (Test-TrayProcess -Process $process)
                )
            )
        ) {
            $process
        }
    }
)
if ($traySurvivors.Count -gt 0) {
    [Console]::Error.WriteLine("Finalmouse tray process did not stop; browser cleanup was not attempted.")
    exit 1
}

$browserPids = @(
    $processes |
        Where-Object { Test-OwnedBrowserProcess -Process $_ } |
        Select-Object -ExpandProperty ProcessId -Unique
)

$driverPids = [System.Collections.Generic.HashSet[int]]::new()
$driverEntries = @()
if (Test-Path -LiteralPath $pidFile) {
    try {
        $pidEntries = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
    } catch {
        $pidEntries = $null
    }
    foreach ($entry in @($pidEntries)) {
        if ($entry.role -ne "driver" -or [string]::IsNullOrWhiteSpace([string] $entry.creation_date)) {
            continue
        }
        $process = $processes | Where-Object { [int] $_.ProcessId -eq [int] $entry.pid } | Select-Object -First 1
        if (
            $process -and
            $process.Name -eq "chromedriver.exe" -and
            (Same-CreationDate -Process $process -Expected ([string] $entry.creation_date))
        ) {
            [void] $driverPids.Add([int] $process.ProcessId)
            $driverEntries += [pscustomobject]@{
                pid = [int] $process.ProcessId
                creation_date = [string] $entry.creation_date
            }
        }
    }
}

foreach ($driverPid in $driverPids) {
    Stop-Process -Id $driverPid -Force
}
foreach ($browserPid in $browserPids) {
    Stop-Process -Id ([int] $browserPid) -Force
}

foreach ($stoppedPid in @($driverPids) + @($browserPids)) {
    Wait-Process -Id ([int] $stoppedPid) -Timeout 5
}

$processes = @(Get-ProcessSnapshot)
$browserSurvivors = @($processes | Where-Object { Test-OwnedBrowserProcess -Process $_ })
$driverSurvivors = @(
    foreach ($entry in $driverEntries) {
        $process = $processes | Where-Object { [int] $_.ProcessId -eq $entry.pid } | Select-Object -First 1
        if (
            $process -and
            $process.Name -eq "chromedriver.exe" -and
            (Same-CreationDate -Process $process -Expected $entry.creation_date)
        ) {
            $process
        }
    }
)
if ($browserSurvivors.Count -gt 0 -or $driverSurvivors.Count -gt 0) {
    [Console]::Error.WriteLine("Finalmouse browser processes did not stop; tracking files were preserved.")
    exit 1
}

Remove-Item -LiteralPath $pidFile -Force
Remove-Item -LiteralPath $lockFile -Force
exit 0
