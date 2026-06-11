<#
.SYNOPSIS
    Dead-man's-switch check: alerts if SkySync hasn't completed a successful
    run recently.

.DESCRIPTION
    Reads state\heartbeat.json (written only on SUCCESSFUL live runs) and
    exits non-zero — with a popup if -Popup — when the heartbeat is older
    than [heartbeat].max_age_minutes (default 45). Schedule it hourly via
    Task Scheduler, or run ad hoc.
#>
[CmdletBinding()]
param(
    [string]$HeartbeatFile = "$PSScriptRoot\state\heartbeat.json",
    [int]$MaxAgeMinutes = 45,
    [switch]$Popup
)

function Alert([string]$msg) {
    Write-Warning $msg
    if ($Popup) {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show($msg, "SkySync heartbeat", 0, 48) | Out-Null
    }
    exit 1
}

if (-not (Test-Path $HeartbeatFile)) {
    Alert "No heartbeat file at $HeartbeatFile - SkySync has never completed a live run."
}

$hb = Get-Content $HeartbeatFile -Raw | ConvertFrom-Json
$ts = [DateTimeOffset]::Parse($hb.timestamp_utc)
$age = [DateTimeOffset]::UtcNow - $ts

if ($age.TotalMinutes -gt $MaxAgeMinutes) {
    Alert ("SkySync heartbeat is STALE: last success {0:F0} min ago (limit {1}). Check logs\skysync.log." -f $age.TotalMinutes, $MaxAgeMinutes)
}

Write-Host ("OK: last successful run {0:F1} min ago." -f $age.TotalMinutes)
exit 0
