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
    '20261013_s3_05_stock_dimension_repair.sql',
    '20261023_dv_repair_add_permission.sql',
    '20261023_dv_check_detail_permissions.sql',
    '20261026_dv_repair_autocode.sql'
)

foreach ($migrationFile in $migrationFiles) {
    $migrationPath = Join-Path $sqlRoot $migrationFile
    if (-not (Test-Path -LiteralPath $migrationPath)) {
        throw "Database migration not found: $migrationPath"
    }

    Write-Host "Applying database migration to ${DatabaseName}: $migrationFile" -ForegroundColor Cyan
    $migrationBytes = [System.IO.File]::ReadAllBytes($migrationPath)
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = 'docker'
    $startInfo.Arguments = "exec -i ktg-mes-mysql sh -c `"mysql -uroot -p\`"`$MYSQL_ROOT_PASSWORD\`" `\`"`$1\`"`" sh $DatabaseName"
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = [System.Diagnostics.Process]::Start($startInfo)
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.StandardInput.BaseStream.Write($migrationBytes, 0, $migrationBytes.Length)
    $process.StandardInput.Close()
    $process.WaitForExit()
    $migrationOutput = $stdoutTask.Result
    $migrationError = $stderrTask.Result
    if ($migrationOutput) { Write-Host $migrationOutput.TrimEnd() }
    if ($migrationError) { Write-Host $migrationError.TrimEnd() -ForegroundColor Red }
    if ($process.ExitCode -ne 0) {
        throw "Database migration failed: $migrationFile (exit code $($process.ExitCode))"
    }
}

Write-Host 'Database migrations are up to date.' -ForegroundColor Green
