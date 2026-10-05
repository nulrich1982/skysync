<#
.SYNOPSIS
    Registers (or re-registers) the SkySync scheduled task.

.DESCRIPTION
    Creates a Windows Task Scheduler job that runs
        python -m skysync.main run --live
    every N minutes (read from [schedule].interval_minutes in config.toml,
    default 15).

    IMPORTANT: run this script as the SAME Windows account that seeded the
    DPAPI secrets (python -m skysync.secrets set ...) and performed
    'python -m skysync.main login'. DPAPI blobs only decrypt for that
    account, so the task must run as that account too.

    By default this registers with LogonType=Interactive (no password
    needed, no elevation needed): the task only fires while that account is
    logged on (locked screen is fine; a full logoff/reboot pauses it until
    next login), with -StartWhenAvailable so a missed window catches up at
    login instead of being silently skipped. This is deliberate, not a
    fallback: this account signs in via Windows Hello/PIN with no
    traditional password set ("only allow Windows Hello sign-in for
    Microsoft accounts" in Settings > Accounts > Sign-in options), so there
    is no password for Task Scheduler's "run whether logged on or not" mode
    to store - that mode needs the Microsoft account's online password,
    which must be entered via -RunWhenLoggedOff below if you ever set one.

.PARAMETER PythonExe
    Full path to python.exe. Defaults to the python on PATH.

.PARAMETER TaskName
    Defaults to "SkySync".

.PARAMETER RunWhenLoggedOff
    Opt into the old "run whether logged on or not" mode, which stores the
    account password (prompted here, in-console, not via a GUI dialog) and
    needs an *elevated* PowerShell. Only useful if this account has (or
    gets) a traditional password - see DESCRIPTION.

.EXAMPLE
    .\register-task.ps1
    .\register-task.ps1 -PythonExe "C:\Python313\python.exe"
    .\register-task.ps1 -RunWhenLoggedOff
#>
[CmdletBinding()]
param(
    [string]$PythonExe = "",
    [string]$TaskName = "SkySync",
    [switch]$RunWhenLoggedOff
)

$ErrorActionPreference = "Stop"
$RepoRoot = $PSScriptRoot

if (-not $PythonExe) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "python not found on PATH; pass -PythonExe" }
    $PythonExe = $cmd.Source
}
if (-not (Test-Path (Join-Path $RepoRoot "config.toml"))) {
    throw "config.toml not found in $RepoRoot - copy config.example.toml and fill it in first."
}

# Read cadence from config.toml ([schedule] interval_minutes), default 15.
$IntervalMinutes = 15
$inSchedule = $false
foreach ($line in Get-Content (Join-Path $RepoRoot "config.toml")) {
    $trim = $line.Trim()
    if ($trim -match '^\[(.+)\]$') { $inSchedule = ($Matches[1] -eq 'schedule'); continue }
    if ($inSchedule -and $trim -match '^interval_minutes\s*=\s*(\d+)') {
        $IntervalMinutes = [int]$Matches[1]
    }
}
Write-Host "Cadence: every $IntervalMinutes minute(s) (from config.toml [schedule].interval_minutes)"

$Action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "-m skysync.main --config `"$RepoRoot\config.toml`" run --live" `
    -WorkingDirectory $RepoRoot

# NOTE: no -RepetitionDuration. [TimeSpan]::MaxValue serializes to a value
# Task Scheduler rejects ("P99999999DT23H59M59S ... out of range"); an
# interval with no duration repeats indefinitely, which is what we want.
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)

$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -RestartCount 0 `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

$User = "$env:USERDOMAIN\$env:USERNAME"

if ($RunWhenLoggedOff) {
    # Needs the account password ONCE at registration (stored by the Task
    # Scheduler service, not by us). Prompted here, in-console
    # (Read-Host -AsSecureString), not via Get-Credential's separate GUI
    # dialog window - that dialog can spawn without focus and become
    # unreachable except via Alt+Tab in some terminal/window-manager setups.
    Write-Host "Registering task '$TaskName' to run as $User (run whether logged on or not)."
    $securePwd = Read-Host -AsSecureString -Prompt "Password for $User (stored by Task Scheduler, not shown/logged)"
    $plainPwd = [Runtime.InteropServices.Marshal]::PtrToStringUni(
        [Runtime.InteropServices.Marshal]::SecureStringToGlobalAllocUnicode($securePwd))

    try {
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $Action `
            -Trigger $Trigger `
            -Settings $Settings `
            -User $User `
            -Password $plainPwd `
            -RunLevel Limited `
            -Force | Out-Null
    } finally {
        # Scrub the plaintext copy from memory as soon as we're done with it.
        $plainPwd = $null
        [GC]::Collect()
    }
} else {
    # No password: fires only while $User is logged on (locked screen is
    # fine), -StartWhenAvailable catches up at next login if a window was
    # missed. No elevation needed either.
    Write-Host "Registering task '$TaskName' to run as $User (while logged on; no password needed)."
    $principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Principal $principal `
        -Force | Out-Null
}

# CIM errors from Register-ScheduledTask don't always honor
# ErrorActionPreference - verify the task actually exists before celebrating.
if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    throw "Task '$TaskName' was NOT registered - see the error above."
}

$mode = if ($RunWhenLoggedOff) { "whether logged on or not" } else { "while logged on; will catch up at next login if $User wasn't signed in" }
Write-Host "Registered ($mode). First run in ~1 minute. Useful commands:"
Write-Host "  Start-ScheduledTask  -TaskName $TaskName        # run now"
Write-Host "  Get-ScheduledTaskInfo -TaskName $TaskName       # last result"
Write-Host "  python -m skysync.main --config config.toml status"

# Export the task definition next to the script for inspection/backup.
Export-ScheduledTask -TaskName $TaskName | Out-File -Encoding utf8 (Join-Path $RepoRoot "task\SkySync.xml")
Write-Host "Task XML exported to task\SkySync.xml"
