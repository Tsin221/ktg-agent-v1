$ErrorActionPreference = 'Stop'

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$frontendRoot = Join-Path $workspaceRoot 'ktg-mes-ui'
$nodeModules = Join-Path $frontendRoot 'node_modules'

$host.UI.RawUI.WindowTitle = 'KTG MES - Frontend'

try {
    Set-Location $workspaceRoot
    . .\use-node16.ps1

    Set-Location $frontendRoot
    if (-not (Test-Path -LiteralPath $nodeModules)) {
        Write-Host ''
        Write-Host 'Installing frontend dependencies for the first run...' -ForegroundColor Cyan
        & npm install --legacy-peer-deps --registry=https://registry.npmmirror.com
        if ($LASTEXITCODE -ne 0) {
            throw "Frontend dependency installation failed with exit code $LASTEXITCODE."
        }
    }

    Write-Host ''
    Write-Host 'Starting frontend at http://localhost:81 ...' -ForegroundColor Green
    & npm run dev -- --port 81
    if ($LASTEXITCODE -ne 0) {
        throw "Frontend stopped with exit code $LASTEXITCODE."
    }
}
catch {
    Write-Host ''
    Write-Host "Frontend startup failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'Keep this window open and check the error above.' -ForegroundColor Yellow
}

