$ErrorActionPreference = 'Stop'

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$backendRoot = Join-Path $workspaceRoot 'ktg-mes-master\ktg-mes-master'
$mavenSettings = Join-Path $workspaceRoot 'maven-settings.xml'
$backendJar = Join-Path $backendRoot 'ktg-admin\target\ktg-admin.jar'

$host.UI.RawUI.WindowTitle = 'KTG MES - Backend'

try {
    Set-Location $workspaceRoot
    . .\use-java8.ps1
    . .\use-local-secrets.ps1

    Set-Location $backendRoot
    Write-Host ''
    Write-Host 'Building backend...' -ForegroundColor Cyan
    # Clean first so the executable admin JAR always embeds the latest business modules.
    & mvn -s $mavenSettings clean package -DskipTests
    if ($LASTEXITCODE -ne 0) {
        throw "Backend build failed with exit code $LASTEXITCODE."
    }

    if (-not (Test-Path -LiteralPath $backendJar)) {
        throw "Backend JAR was not generated: $backendJar"
    }

    Write-Host ''
    Write-Host 'Starting backend at http://localhost:8080 ...' -ForegroundColor Green
    & java -jar $backendJar
    if ($LASTEXITCODE -ne 0) {
        throw "Backend stopped with exit code $LASTEXITCODE."
    }
}
catch {
    Write-Host ''
    Write-Host "Backend startup failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'Keep this window open and check the error above.' -ForegroundColor Yellow
}

