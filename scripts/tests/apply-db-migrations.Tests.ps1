$workspaceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$migrationScript = Join-Path $workspaceRoot 'scripts\apply-db-migrations.ps1'

Describe 'apply-db-migrations.ps1' {
    It 'applies the registered migrations idempotently' {
        Test-Path -LiteralPath $migrationScript | Should Be $true

        & $migrationScript
        & $migrationScript

        $columnCount = & docker exec ktg-mes-mysql sh -c 'mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" -e "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name=''pro_workorder'' AND column_name=''version'';"'
        $LASTEXITCODE | Should Be 0
        ($columnCount | Select-Object -Last 1).Trim() | Should Be '1'
    }
}
