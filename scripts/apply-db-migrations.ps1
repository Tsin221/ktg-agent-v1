param([string]$DatabaseName = $env:MYSQL_DATABASE)

$ErrorActionPreference = 'Stop'

if ($DatabaseName -notmatch '^[A-Za-z][A-Za-z0-9_]*$') {
    throw 'Pass an explicit valid -DatabaseName or set MYSQL_DATABASE before applying migrations.'
}

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$sqlRoot = Join-Path $workspaceRoot 'ktg-mes-master\ktg-mes-master\sql'
$migrationFiles = @(
    '20261009_s2_01_workorder_contract.sql',
    '20261009_s2_04_workorder_draft_version.sql',
    '20261013_s3_02_inventory_operation.sql',
    '20261013_s3_03_inventory_document_version.sql',
    '20261013_s3_04_stock_atomicity.sql',
    '20261013_s3_05_stock_dimension_repair.sql'
)

foreach ($migrationFile in $migrationFiles) {
    $migrationPath = Join-Path $sqlRoot $migrationFile
    if (-not (Test-Path -LiteralPath $migrationPath)) {
        throw "Database migration not found: $migrationPath"
    }

    Write-Host "Applying database migration to ${DatabaseName}: $migrationFile" -ForegroundColor Cyan
    Get-Content -LiteralPath $migrationPath -Raw |
        & docker exec -i ktg-mes-mysql sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$1"' sh $DatabaseName
    if ($LASTEXITCODE -ne 0) {
        throw "Database migration failed: $migrationFile (exit code $LASTEXITCODE)"
    }
}

Write-Host 'Database migrations are up to date.' -ForegroundColor Green
