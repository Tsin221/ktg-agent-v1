$ErrorActionPreference = 'Stop'

$workspaceRoot = $PSScriptRoot
$composeFile = Join-Path $workspaceRoot 'docker-compose.mysql.yml'
$backendScript = Join-Path $workspaceRoot 'scripts\start-backend.ps1'
$frontendScript = Join-Path $workspaceRoot 'scripts\start-frontend.ps1'
$agentScript = Join-Path $workspaceRoot 'scripts\start-agent.ps1'
$migrationScript = Join-Path $workspaceRoot 'scripts\apply-db-migrations.ps1'
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

function Wait-LocalPort {
    param(
        [int]$Port,
        [int]$TimeoutSeconds
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-LocalPort -Port $Port) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

try {
    foreach ($requiredFile in @($composeFile, $backendScript, $frontendScript, $agentScript, $migrationScript, $windowsPowerShell)) {
        if (-not (Test-Path -LiteralPath $requiredFile)) {
            throw "Required file not found: $requiredFile"
        }
    }

    Set-Location $workspaceRoot
    . .\use-local-secrets.ps1

    if ([string]::IsNullOrWhiteSpace($env:MODEL_API_KEY) -or $env:MODEL_API_KEY -eq 'CHANGE_ME') {
        throw 'Configure MODEL_API_KEY in the root .env file before starting.'
    }
    if ([string]::IsNullOrWhiteSpace($env:MODEL_ID) -or $env:MODEL_ID -eq 'CHANGE_ME') {
        throw 'Configure MODEL_ID in the root .env file before starting.'
    }

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

    Write-Host 'Applying database migrations...' -ForegroundColor Cyan
    & $migrationScript

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
    Write-Host 'Waiting for the three services to listen...' -ForegroundColor Cyan
    if (-not (Wait-LocalPort -Port 8080 -TimeoutSeconds 180)) {
        throw 'Backend did not listen on port 8080 within 180 seconds. Check the backend terminal.'
    }
    if (-not (Wait-LocalPort -Port 81 -TimeoutSeconds 180)) {
        throw 'Frontend did not listen on port 81 within 180 seconds. Check the frontend terminal.'
    }
    if (-not (Wait-LocalPort -Port 8001 -TimeoutSeconds 30)) {
        throw 'Agent did not listen on port 8001 within 30 seconds. Check the Agent terminal.'
    }

    $agentHealth = Invoke-RestMethod -Method Get -Uri 'http://127.0.0.1:8001/health' -TimeoutSec 5
    if ($agentHealth.status -ne 'ok') {
        throw 'Agent health check did not return status=ok.'
    }

    Write-Host 'Backend, frontend and Agent are listening; Agent health check passed.' -ForegroundColor Green
    Write-Host 'Frontend: http://localhost:81'
    Write-Host 'Backend:  http://localhost:8080'
    Write-Host 'Agent:    http://localhost:8001'
}
catch {
    Write-Host ''
    Write-Host "Startup failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

