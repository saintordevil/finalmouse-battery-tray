$ErrorActionPreference = "SilentlyContinue"

$dataDir = Join-Path $env:LOCALAPPDATA "finalmouse-tray"
$profileDir = Join-Path $dataDir "chrome-isolated"
$profileNeedle = $profileDir.Replace("\", "/").ToLowerInvariant()
$pidFile = Join-Path $dataDir "chrome.pids"
$lockFile = Join-Path $dataDir "tray.lock"
$targetPids = [System.Collections.Generic.HashSet[int]]::new()

function Add-TargetPid {
    param([int] $ProcessId)
    if ($ProcessId -gt 0) {
        [void] $targetPids.Add($ProcessId)
    }
}

function Same-CreationDate {
    param($Process, [string] $Expected)
    if ([string]::IsNullOrWhiteSpace($Expected)) {
        return $false
    }
    return ([string] $Process.CreationDate) -eq $Expected
}

if (Test-Path -LiteralPath $lockFile) {
    $lockedPid = Get-Content -LiteralPath $lockFile -TotalCount 1
    if ($lockedPid -match "^\d+$") {
        Add-TargetPid -ProcessId ([int] $lockedPid)
    }
}

Get-CimInstance Win32_Process |
    Where-Object {
        $_.Name -in @("python.exe", "pythonw.exe") -and
        $_.CommandLine -match "finalmouse_tray\.py"
    } |
    ForEach-Object { Add-TargetPid -ProcessId ([int] $_.ProcessId) }

Get-CimInstance Win32_Process |
    Where-Object {
        $_.Name -eq "chrome.exe" -and
        $_.CommandLine -and
        $_.CommandLine.Replace("\", "/").ToLowerInvariant().Contains($profileNeedle)
    } |
    ForEach-Object { Add-TargetPid -ProcessId ([int] $_.ProcessId) }

if (Test-Path -LiteralPath $pidFile) {
    $rawPidFile = Get-Content -LiteralPath $pidFile -Raw
    $pidEntries = $null
    try {
        $pidEntries = $rawPidFile | ConvertFrom-Json
    } catch {
        $pidEntries = $null
    }

    if ($pidEntries) {
        foreach ($entry in @($pidEntries)) {
            if ($entry.role -ne "driver") {
                continue
            }
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($entry.pid)"
            if (
                $proc -and
                $proc.Name -eq "chromedriver.exe" -and
                (Same-CreationDate -Process $proc -Expected $entry.creation_date)
            ) {
                Add-TargetPid -ProcessId ([int] $proc.ProcessId)
            }
        }
    }
}

foreach ($targetPid in $targetPids) {
    Stop-Process -Id $targetPid -Force
}

Remove-Item -LiteralPath $pidFile -Force
Remove-Item -LiteralPath $lockFile -Force
