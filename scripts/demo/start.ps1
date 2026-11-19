$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$demoRuntime = Join-Path $projectRoot '.runtime/demo'
if (!(Test-Path (Join-Path $demoRuntime 'initialized.json'))) { throw 'Run initialize.py first.' }
if (!(Test-Path (Join-Path $projectRoot 'ktg-mes-ui/dist-demo/index.html'))) { throw 'Build MES with --mode demo first.' }
foreach ($demoPort in @(8082,8002,4180)) {
    if (Get-NetTCPConnection -LocalPort $demoPort -State Listen -ErrorAction SilentlyContinue) { throw "Port $demoPort is already in use. Stop the existing demo before restarting." }
}
if (!(Test-Path (Join-Path $demoRuntime 'token-secret.txt'))) {
    $secretBytes = New-Object byte[] 48
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($secretBytes)
    [IO.File]::WriteAllText((Join-Path $demoRuntime 'token-secret.txt'),[Convert]::ToBase64String($secretBytes))
}
$demoProcesses = @()
$services = @('backend','agent','gateway')
if (!(Get-NetTCPConnection -LocalPort 4173 -State Listen -ErrorAction SilentlyContinue)) { $services += 'showcase' }
foreach ($serviceName in $services) {
    $serviceArgs = @('-NoProfile','-ExecutionPolicy','Bypass','-File',('"' + (Join-Path $PSScriptRoot 'run-service.ps1') + '"'),'-Service',$serviceName)
    $process = Start-Process powershell.exe -ArgumentList $serviceArgs -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $demoRuntime "$serviceName.out.log") -RedirectStandardError (Join-Path $demoRuntime "$serviceName.err.log")
    $demoProcesses += @{ service=$serviceName; processId=$process.Id; started=$process.StartTime.ToUniversalTime().ToString('o') }
}
$demoProcesses | ConvertTo-Json | Set-Content (Join-Path $demoRuntime 'processes.json') -Encoding UTF8
Write-Host 'Demo services starting. MES: http://127.0.0.1:4180/experience'
