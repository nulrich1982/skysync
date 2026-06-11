<#
.SYNOPSIS
    Registers (or re-registers) the SkySync scheduled task.

.DESCRIPTION
    Creates a Windows Task Scheduler job that runs
        python -m skysync.main run --live
    every N minutes (read from [schedule].interval_minutes in config.toml,
    default 15), whether the user is logged on or not.

    IMPORTANT: run this script from an *elevated* PowerShell, as the SAME
    Windows account that seeded the DPAPI secrets (python -m skysync.secrets
    set ...) and performed 'python -m skysync.main login'. DPAPI blobs only
    decrypt for that account, so the task must run as that account too.

.PARAMETER PythonExe
    Full path to python.exe. Defaults to the python on PATH.

.PARAMETER TaskName
    Defaults to "SkySync".

.EXAMPLE
    .\register-task.ps1
    .\register-task.ps1 -PythonExe "C:\Python313\python.exe"
#>
[CmdletBinding()]
param(
    [string]$PythonExe = "",
    [string]$TaskName = "SkySync"
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

$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration ([TimeSpan]::MaxValue)

$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -RestartCount 0 `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

# Run whether logged on or not: needs the account password ONCE at
# registration (stored by the Task Scheduler service, not by us).
$User = "$env:USERDOMAIN\$env:USERNAME"
Write-Host "Registering task '$TaskName' to run as $User (run whether logged on or not)."
$cred = Get-Credential -UserName $User -Message "Password for $User (stored by Task Scheduler)"

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -User $cred.UserName `
    -Password $cred.GetNetworkCredential().Password `
    -RunLevel Limited `
    -Force | Out-Null

Write-Host "Registered. First run in ~1 minute. Useful commands:"
Write-Host "  Start-ScheduledTask  -TaskName $TaskName        # run now"
Write-Host "  Get-ScheduledTaskInfo -TaskName $TaskName       # last result"
Write-Host "  python -m skysync.main --config config.toml status"

# Export the task definition next to the script for inspection/backup.
Export-ScheduledTask -TaskName $TaskName | Out-File -Encoding utf8 (Join-Path $RepoRoot "task\SkySync.xml")
Write-Host "Task XML exported to task\SkySync.xml"
