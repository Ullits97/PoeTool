<#
.SYNOPSIS
    Register poe-flip as an hourly Windows Task Scheduler job.

.DESCRIPTION
    Python does not register the task itself — a tool that edits the task
    scheduler behind your back is harder to reason about than one line of
    PowerShell you can read. Run this once from an elevated prompt.

.PARAMETER Hours
    Interval between runs. Default 1. The client enforces a hard 5-minute
    floor between requests to any one endpoint regardless of this value, so
    setting it lower than 1 mostly produces cache hits.

.PARAMETER Unregister
    Remove the task instead of creating it.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
    powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Hours 2
    powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Unregister
#>

[CmdletBinding()]
param(
    [int]$Hours = 1,
    [string]$TaskName = "poe-flip",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$runScript = Join-Path $projectRoot "run.py"
$configFile = Join-Path $projectRoot "config.yaml"

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task named '$TaskName' found."
    }
    return
}

if (-not (Test-Path $runScript)) {
    throw "Cannot find run.py at $runScript. Run this from the project's scripts directory."
}
if (-not (Test-Path $configFile)) {
    throw "Cannot find config.yaml at $configFile."
}

# Refuse to schedule a job that will fail on every fire.
$configText = Get-Content $configFile -Raw
if ($configText -match "CHANGEME" -or $configText -match "example\.com") {
    throw @"
config.yaml still contains the placeholder user_agent.
poe-flip will refuse to start, so scheduling it now would just fill run.log
with failures. Edit app.user_agent in config.yaml first:

  user_agent: "poe-flip/0.1 (contact: you@yourdomain.tld)"
"@
}

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
    $python = (Get-Command py -ErrorAction SilentlyContinue).Source
}
if (-not $python) {
    throw "Could not find python or py on PATH."
}

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Task '$TaskName' already exists; replacing it."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "`"$runScript`" run" `
    -WorkingDirectory $projectRoot

# Start at the next whole hour, then repeat indefinitely.
$startAt = (Get-Date).Date.AddHours((Get-Date).Hour + 1)
$trigger = New-ScheduledTaskTrigger -Once -At $startAt `
    -RepetitionInterval (New-TimeSpan -Hours $Hours)

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "poe-flip: fetch poe.ninja economy data, analyse, export poe_data.xlsx" | Out-Null

Write-Host "Registered '$TaskName': every $Hours hour(s), first run at $startAt."
Write-Host "  python:  $python"
Write-Host "  project: $projectRoot"
Write-Host ""
Write-Host "Check it is firing with:  Get-ScheduledTaskInfo -TaskName $TaskName"
Write-Host "Or read the log at:       $(Join-Path $projectRoot 'data\run.log')"
