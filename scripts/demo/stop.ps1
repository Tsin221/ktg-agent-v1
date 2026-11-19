$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$processFile = Join-Path $projectRoot '.runtime/demo/processes.json'
if (!(Test-Path $processFile)) { return }
$expectedScript = Join-Path $PSScriptRoot 'run-service.ps1'
function Stop-DemoTree([int]$processId) {
    Get-CimInstance Win32_Process -Filter "ParentProcessId=$processId" | ForEach-Object { Stop-DemoTree $_.ProcessId }
    Stop-Process -Id $processId -ErrorAction SilentlyContinue
}
foreach ($record in (Get-Content $processFile -Raw | ConvertFrom-Json)) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($record.processId)"
    if (!$process) { continue }
    if (!$process.CommandLine.Contains($expectedScript) -or [Math]::Abs(($process.CreationDate.ToUniversalTime() - ([datetime]$record.started).ToUniversalTime()).TotalSeconds) -gt 2) { throw 'Process identity changed; refusing to stop an unrelated process.' }
    Stop-DemoTree $record.processId
}
Remove-Item -LiteralPath $processFile
Write-Host 'Demo services stopped.'
