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

$normalizedScriptPath = [IO.Path]::GetFullPath($ScriptPath)
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

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr OpenProcess(uint access, bool inherit, int pid);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetProcessTimes(IntPtr process, out long created,
        out long exited, out long kernel, out long user);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateProcess(IntPtr process, uint exitCode);

    [DllImport("kernel32.dll")]
    private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);

    [DllImport("kernel32.dll")]
    private static extern bool CloseHandle(IntPtr handle);

    public static bool StopVerified(int pid, long expectedCreated)
    {
        // Query and stop through one handle so a reused PID cannot be targeted.
        IntPtr process = OpenProcess(0x00101001, false, pid);
        if (process == IntPtr.Zero) {
            return Marshal.GetLastWin32Error() == 87;
        }
        try {
            long created, exited, kernel, user;
            if (!GetProcessTimes(process, out created, out exited, out kernel, out user)) {
                return false;
            }
            // CIM reports creation timestamps to microsecond precision.
            if (created / 10 != expectedCreated / 10) {
                return true;
            }
            if (WaitForSingleObject(process, 0) == 0) {
                return true;
            }
            if (!TerminateProcess(process, 0)) {
                return WaitForSingleObject(process, 0) == 0;
            }
            return WaitForSingleObject(process, 5000) == 0;
        }
        finally {
            CloseHandle(process);
        }
    }

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

function Get-ProcessSnapshot {
    try {
        $items = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction Stop)
    } catch {
        [Console]::Error.WriteLine("Could not inspect running processes; nothing was stopped.")
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
                creation_filetime = ([DateTime] $_.CreationDate).ToUniversalTime().ToFileTimeUtc()
            }
        }
)
foreach ($identity in $trayIdentities) {
    if (-not [FinalmouseCommandLineParser]::StopVerified($identity.pid, $identity.creation_filetime)) {
        [Console]::Error.WriteLine("The verified tray process could not be stopped; tracking was preserved.")
        exit 1
    }
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
    [Console]::Error.WriteLine("Finalmouse tray process did not stop; tracking was preserved.")
    exit 1
}
Remove-Item -LiteralPath $lockFile -Force
exit 0
