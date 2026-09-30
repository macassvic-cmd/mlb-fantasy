$ErrorActionPreference = "Stop"

# Registers ONE Windows Scheduled Task for soccer_daily_digest.py's
# "morning_board" slot (see that module's DAILY_DIGEST_SCHEDULE) - the
# only slot enabled by default. Adding the "midday_update" slot later is
# a second, separate `Register-ScheduledTask` block using this same
# pattern with its own -At time, NOT a code change to soccer_daily_
# digest.py itself.
#
# TIMEZONE NOTE: -At below is interpreted in THIS MACHINE'S LOCAL
# timezone by Windows Task Scheduler. "7:00 AM Pacific" only actually
# fires at 7am Pacific if this machine's own local timezone IS Pacific -
# if it's set to something else, adjust $TriggerTime accordingly (e.g.
# this machine on Eastern time wanting 7am Pacific would use "10:00").
# This script does not attempt to auto-detect/convert - matching soccer_
# daily_digest.py's own documented "host wall clock" contract.

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName = "Soccer DNS Daily Digest - Morning Board"
$TriggerTime = "07:00"

Write-Host "Project: $ProjectDir"

$required = @(
    (Join-Path $ProjectDir "soccer_daily_digest.py"),
    (Join-Path $ProjectDir "soccer_dns.py"),
    (Join-Path $ProjectDir "run_soccer_daily_digest.bat")
)
foreach ($path in $required) {
    if (-not (Test-Path $path)) {
        throw "Required file missing: $path"
    }
}

$action = New-ScheduledTaskAction -Execute (Join-Path $ProjectDir "run_soccer_daily_digest.bat") `
    -WorkingDirectory $ProjectDir
$trigger = New-ScheduledTaskTrigger -Daily -At $TriggerTime
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Registered scheduled task '$TaskName' - daily at $TriggerTime (host local time)."
Write-Host "Verify/adjust with: Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
