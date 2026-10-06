$ErrorActionPreference = 'Stop'

$workspaceRoot = $PSScriptRoot
$composeFile = Join-Path $workspaceRoot 'docker-compose.mysql.yml'
$backendScript = Join-Path $workspaceRoot 'scripts\start-backend.ps1'
$frontendScript = Join-Path $workspaceRoot 'scripts\start-frontend.ps1'
$agentScript = Join-Path $workspaceRoot 'scripts\start-agent.ps1'
$windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

function Test-LocalPort {
    param([int]$Port)

    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $connection = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $connection.AsyncWaitHandle.WaitOne(300)) {
            return $false
        }
        $client.EndConnect($connection)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Close()
    }
}

try {
    foreach ($requiredFile in @($composeFile, $backendScript, $frontendScript, $agentScript, $windowsPowerShell)) {
        if (-not (Test-Path -LiteralPath $requiredFile)) {
            throw "Required file not found: $requiredFile"
        }
    }

    Set-Location $workspaceRoot

    Write-Host 'Checking Docker Desktop...' -ForegroundColor Cyan
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & docker info *> $null
    $dockerInfoExitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousErrorAction
    if ($dockerInfoExitCode -ne 0) {
        throw 'Docker Desktop is not running. Start Docker Desktop and try again.'
    }

    Write-Host 'Starting MySQL and Redis...' -ForegroundColor Cyan
    & docker compose -f $composeFile up -d --wait
    if ($LASTEXITCODE -ne 0) {
        throw "Docker services failed to start with exit code $LASTEXITCODE."
    }

    if (Test-LocalPort -Port 8080) {
        throw 'Port 8080 is already in use. Stop the existing backend and try again.'
    }
    if (Test-LocalPort -Port 81) {
        throw 'Port 81 is already in use. Stop the existing frontend and try again.'
    }
    if (Test-LocalPort -Port 8001) {
        throw 'Port 8001 is already in use. Stop the existing Agent service and try again.'
    }

    Write-Host 'Opening backend terminal...' -ForegroundColor Cyan
    Start-Process -FilePath $windowsPowerShell `
        -WorkingDirectory $workspaceRoot `
        -ArgumentList @('-NoExit', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$backendScript`"")

    Write-Host 'Opening frontend terminal...' -ForegroundColor Cyan
    Start-Process -FilePath $windowsPowerShell `
        -WorkingDirectory $workspaceRoot `
        -ArgumentList @('-NoExit', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$frontendScript`"")

    Write-Host 'Opening Agent terminal...' -ForegroundColor Cyan
    Start-Process -FilePath $windowsPowerShell `
        -WorkingDirectory $workspaceRoot `
        -ArgumentList @('-NoExit', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$agentScript`"")

    Write-Host ''
    Write-Host 'Three terminals have been opened.' -ForegroundColor Green
    Write-Host 'Frontend: http://localhost:81'
    Write-Host 'Backend:  http://localhost:8080'
    Write-Host 'Agent:    http://localhost:8001'
}
catch {
    Write-Host ''
    Write-Host "Startup failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

