$ErrorActionPreference = 'Stop'

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$sqlRoot = Join-Path $workspaceRoot 'ktg-mes-master\ktg-mes-master\sql'
$migrationFiles = @(
    '20261009_s2_01_workorder_contract.sql',
    '20261009_s2_04_workorder_draft_version.sql',
    '20261013_s3_02_inventory_operation.sql'
)

foreach ($migrationFile in $migrationFiles) {
    $migrationPath = Join-Path $sqlRoot $migrationFile
    if (-not (Test-Path -LiteralPath $migrationPath)) {
        throw "Database migration not found: $migrationPath"
    }

    Write-Host "Applying database migration: $migrationFile" -ForegroundColor Cyan
    Get-Content -LiteralPath $migrationPath -Raw |
        & docker exec -i ktg-mes-mysql sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"'
    if ($LASTEXITCODE -ne 0) {
        throw "Database migration failed: $migrationFile (exit code $LASTEXITCODE)"
    }
}

Write-Host 'Database migrations are up to date.' -ForegroundColor Green
