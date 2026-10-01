<#
.SYNOPSIS
    Registers (or re-registers) the SkySync-MenuMonthEnd scheduled task.

.DESCRIPTION
    Creates a Windows Task Scheduler job that runs
        python -m skysync.menu.cli --config config.toml
    once, late in the evening of the LAST day of every month from August
    through May (no run in June/July — no school, nothing to import).

    This is a belt-and-suspenders trigger on top of the once/day menu sync
    that already runs inside the main 15-min SkySync task (see
    register-task.ps1): it guarantees an attempt to pull next month's menu
    lands right at the month boundary, even if the main task is disabled,
    missed its daily throttle window, or hasn't been (re)installed yet.
    [menu].months_ahead in config.toml already covers "current + next
    month" per run, so no extra CLI flag is needed here.

    Registered with LogonType=InteractiveToken (no password prompt, no
    elevation needed) + StartWhenAvailable, so if the PC is off or the user
    isn't logged on at 21:00 that day, it fires at the next logon instead of
    silently skipping the month.

.PARAMETER PythonExe
    Full path to python.exe. Defaults to the python on PATH.

.PARAMETER TaskName
    Defaults to "SkySync-MenuMonthEnd".

.PARAMETER StartTime
    Time of day (HH:mm) to run on the last day of the month. Default 21:00,
    to give the district time to publish before the attempt.

.EXAMPLE
    .\register-menu-monthend-task.ps1
#>
[CmdletBinding()]
param(
    [string]$PythonExe = "",
    [string]$TaskName = "SkySync-MenuMonthEnd",
    [string]$StartTime = "21:00"
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

$Months = "AUG,SEP,OCT,NOV,DEC,JAN,FEB,MAR,APR,MAY"
$User = "$env:USERDOMAIN\$env:USERNAME"
$TaskRun = "`"$PythonExe`" -m skysync.menu.cli --config `"$RepoRoot\config.toml`""

Write-Host "Registering '$TaskName': last day of month, $Months, at $StartTime, as $User."

# schtasks (not New-ScheduledTaskTrigger -Monthly) because it's the only
# supported way to express "the actual last day of the month" - /MO LASTDAY -
# rather than a fixed day-of-month that skips short months.
# /RU <self> with no /RP: InteractiveToken logon, no password stored/needed.
& schtasks /create /tn $TaskName /sc MONTHLY /mo LASTDAY /m $Months `
    /tr $TaskRun /st $StartTime /ru $User /rl LIMITED /f
if ($LASTEXITCODE -ne 0) { throw "schtasks /create failed (exit $LASTEXITCODE)" }

# schtasks has no flag for WorkingDirectory/StartWhenAvailable - patch those
# in via the ScheduledTasks module, same principal, no elevation needed.
$task = Get-ScheduledTask -TaskName $TaskName
$task.Actions[0].WorkingDirectory = $RepoRoot
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -RestartCount 0 `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Set-ScheduledTask -TaskName $TaskName -Action $task.Actions[0] -Settings $settings | Out-Null

if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    throw "Task '$TaskName' was NOT registered - see the error above."
}

Write-Host "Registered. Useful commands:"
Write-Host "  Start-ScheduledTask   -TaskName $TaskName   # run now"
Write-Host "  Get-ScheduledTaskInfo -TaskName $TaskName   # last result"
Write-Host "  python -m skysync.menu.cli --config config.toml --show   # preview, no writes"

Export-ScheduledTask -TaskName $TaskName | Out-File -Encoding utf8 (Join-Path $RepoRoot "task\SkySync-MenuMonthEnd.xml")
Write-Host "Task XML exported to task\SkySync-MenuMonthEnd.xml"
