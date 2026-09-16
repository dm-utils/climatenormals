# Scheduled-task entry point: processes the next batch of not-yet-done
# climate-normal regions, commits+pushes the result, then exits.
# Safe to run repeatedly (skips regions already in normals.json) and safe
# to interrupt (checkpoints every 5 regions within a run).
#
# Task Scheduler setup:
#   Program/script:  powershell.exe
#   Arguments:        -ExecutionPolicy Bypass -File "C:\Datamodder\ClimateNormals\dev\run_next_batch.ps1"
#   Start in:          C:\Datamodder\ClimateNormals
#   Trigger:           e.g. every 2 hours

Set-Location "C:\Datamodder\ClimateNormals"
$logFile = "dev\batch_log.txt"
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $logFile -Value "`n=== Run at $timestamp ==="
python dev\build_normals.py dev\regions.json --max 15 *>> $logFile
