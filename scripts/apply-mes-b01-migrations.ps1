[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidatePattern('^ktg_mes_b01_[a-z0-9_]+$')][string]$DatabaseName,
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]+$')][string]$ContainerName,
    [Parameter(Mandatory)][string]$EvidenceDir,
    [switch]$VerifyOnly
)

$ErrorActionPreference = 'Stop'
if ($ContainerName -eq 'ktg-mes-mysql') { throw 'Use only an explicitly isolated B01 container.' }
$sqlRoot = Join-Path (Split-Path -Parent $PSScriptRoot) 'ktg-mes-master\ktg-mes-master\sql'
$preflight = Join-Path $sqlRoot '20261123_execution_preflight.sql'
$schemaContract = Join-Path $sqlRoot '20261123_execution_schema_contract.tsv'
. (Join-Path $PSScriptRoot 'mes-b01-schema-validation.ps1')
$migrations = @('20261123_execution_01_order_task_unit.sql', '20261123_execution_02_facts.sql', '20261123_execution_03_operation.sql')
if (Test-Path -LiteralPath $EvidenceDir) { throw 'Choose a new evidence directory; never overwrite a prior backup.' }
$null = New-Item -ItemType Directory -Path $EvidenceDir
$utf8 = [Text.UTF8Encoding]::new($false)

function Invoke-Container([string]$ShellCommand, [byte[]]$InputBytes, [string]$OutputFile) {
    $info = [Diagnostics.ProcessStartInfo]::new('docker')
    $info.UseShellExecute = $false
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = $utf8
    foreach ($argument in @('exec','-i',$ContainerName,'sh','-c',$ShellCommand,'sh',$DatabaseName)) { $info.ArgumentList.Add($argument) }
    $process = [Diagnostics.Process]::Start($info)
    try {
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if ($OutputFile) {
            $file = [IO.File]::Open($OutputFile,[IO.FileMode]::CreateNew)
            try {
                $stdoutTask = $process.StandardOutput.BaseStream.CopyToAsync($file)
                if ($InputBytes) { $process.StandardInput.BaseStream.Write($InputBytes,0,$InputBytes.Length) }
                $process.StandardInput.Close()
                $stdoutTask.GetAwaiter().GetResult()
            } finally { $file.Dispose() }
            $stdout = ''
        } else {
            $stdoutTask = $process.StandardOutput.ReadToEndAsync()
            if ($InputBytes) { $process.StandardInput.BaseStream.Write($InputBytes,0,$InputBytes.Length) }
            $process.StandardInput.Close()
            $stdout = $stdoutTask.GetAwaiter().GetResult()
        }
        $process.WaitForExit()
        $stderrText = $stderrTask.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) { throw "Isolated MySQL command failed ($($process.ExitCode)): $stderrText" }
        return $stdout
    } finally { $process.Dispose() }
}

function Invoke-Sql([string]$Sql) {
    Invoke-Container 'exec mysql --default-character-set=utf8mb4 --batch --skip-column-names -uroot -p"$MYSQL_ROOT_PASSWORD" "$1"' ($utf8.GetBytes($Sql)) ''
}

function Get-TextSha([string]$Content) {
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { ([BitConverter]::ToString($hasher.ComputeHash($utf8.GetBytes($Content)))).Replace('-','').ToLowerInvariant() }
    finally { $hasher.Dispose() }
}

function Test-Preflight([string]$Report) {
    $rows = @($Report.Trim() -split '\r?\n')
    if ($rows.Count -ne 13) { throw 'Incomplete B01 preflight output.' }
    foreach ($row in $rows) {
        $columns = $row -split "`t",3
        if ($columns.Count -ne 3 -or $columns[1] -ne '1') { throw "B01 preflight failed: $row" }
    }
}

function Get-ExecutionStructure {
    Invoke-Sql @'
SELECT table_name,column_name,column_type,is_nullable,IFNULL(collation_name,''),extra,generation_expression
FROM information_schema.columns WHERE table_schema=DATABASE() AND (table_name LIKE 'pro\_execution\_%'
 OR (table_name='pro_route_process' AND column_name='execution_kind')) ORDER BY table_name,ordinal_position;
SELECT table_name,index_name,non_unique,seq_in_index,column_name FROM information_schema.statistics
WHERE table_schema=DATABASE() AND (table_name LIKE 'pro\_execution\_%' OR (table_name='wm_sn' AND index_name='uk_wm_sn_code'))
ORDER BY table_name,index_name,seq_in_index;
SELECT table_name,constraint_name,referenced_table_name,update_rule,delete_rule FROM information_schema.referential_constraints
WHERE constraint_schema=DATABASE() AND table_name LIKE 'pro\_execution\_%' ORDER BY table_name,constraint_name;
SELECT table_name,'table_properties',engine,table_collation,table_type FROM information_schema.tables
WHERE table_schema=DATABASE() AND table_name LIKE 'pro\_execution\_%' ORDER BY table_name;
SELECT table_name,'default',column_name,column_default FROM information_schema.columns
WHERE table_schema=DATABASE() AND table_name LIKE 'pro\_execution\_%' AND column_default IS NOT NULL ORDER BY table_name,column_name;
SELECT table_name,'fk_column',constraint_name,ordinal_position,column_name,referenced_column_name FROM information_schema.key_column_usage
WHERE table_schema=DATABASE() AND table_name LIKE 'pro\_execution\_%' AND referenced_table_name IS NOT NULL ORDER BY table_name,constraint_name,ordinal_position;
SELECT table_name,'invalid_index',index_name FROM information_schema.statistics WHERE table_schema=DATABASE()
AND table_name LIKE 'pro\_execution\_%' AND (sub_part IS NOT NULL OR index_type <> 'BTREE');
SELECT table_name,'invalid_fk_schema',constraint_name FROM information_schema.key_column_usage
WHERE table_schema=DATABASE() AND table_name LIKE 'pro\_execution\_%'
AND referenced_table_schema IS NOT NULL AND referenced_table_schema<>DATABASE();
'@
}

function Get-OldSnapshot {
    # All legacy columns, including BLOBs, are hashed individually per row and sorted by hash.
    # NULL is distinct from empty bytes. Output contains only aggregate evidence, never raw rows.
    $metadata = Invoke-Sql @'
SELECT table_name,column_name FROM information_schema.columns
WHERE table_schema=DATABASE() AND table_name NOT LIKE 'pro\_execution\_%'
 AND NOT (table_name='pro_route_process' AND column_name='execution_kind')
ORDER BY table_name,ordinal_position;
'@
    $tableColumns = [ordered]@{}
    foreach ($row in ($metadata.Trim() -split '\r?\n')) {
        $table,$column = $row -split "`t",2
        if ($table -notmatch '^[A-Za-z0-9_]+$' -or $column -notmatch '^[A-Za-z0-9_]+$') { throw 'Unexpected legacy schema identifier.' }
        if (-not $tableColumns.Contains($table)) { $tableColumns[$table] = @() }
        $tableColumns[$table] += $column
    }
    $sql = @()
    foreach ($table in $tableColumns.Keys) {
        # Quote identifiers to support MySQL keywords appearing as old column/table names.
        $expressions = foreach ($column in $tableColumns[$table]) { "IF(``$column`` IS NULL,'N',CONCAT('V',HEX(CAST(``$column`` AS BINARY))))" }
        $sql += "SELECT 'TABLE:$table',COUNT(*) FROM ``$table``; SELECT SHA2(CONCAT_WS('|',$( $expressions -join ',' )),256) FROM ``$table`` ORDER BY 1;"
    }
    $allRows = Invoke-Sql ($sql -join "`n")
    $counts = @($allRows -split '\r?\n' | Where-Object { $_ -like 'TABLE:*' })
    if ($counts.Count -ne $tableColumns.Count) { throw 'Incomplete legacy data snapshot.' }
    $schema = Invoke-Sql @'
SELECT table_name,ordinal_position,column_name,column_type,is_nullable,IFNULL(column_default,'<NULL>'),IFNULL(collation_name,''),extra,generation_expression
FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name NOT LIKE 'pro\_execution\_%'
 AND NOT (table_name='pro_route_process' AND column_name='execution_kind') ORDER BY table_name,ordinal_position;
SELECT table_name,index_name,non_unique,seq_in_index,column_name,IFNULL(sub_part,0),index_type
FROM information_schema.statistics WHERE table_schema=DATABASE() AND table_name NOT LIKE 'pro\_execution\_%'
 AND NOT (table_name='wm_sn' AND index_name='uk_wm_sn_code') ORDER BY table_name,index_name,seq_in_index;
SELECT table_name,engine,table_collation FROM information_schema.tables WHERE table_schema=DATABASE()
 AND table_name NOT LIKE 'pro\_execution\_%' ORDER BY table_name;
SELECT table_name,constraint_name,referenced_table_name,update_rule,delete_rule FROM information_schema.referential_constraints
 WHERE constraint_schema=DATABASE() AND table_name NOT LIKE 'pro\_execution\_%' ORDER BY table_name,constraint_name;
'@
    [ordered]@{ legacyTables=$tableColumns.Count; rowCounts=$counts; dataSha256=(Get-TextSha $allRows); schemaSha256=(Get-TextSha $schema) }
}

function Save-Json($Data,[string]$Name) {
    $Data | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 -LiteralPath (Join-Path $EvidenceDir $Name)
}

$preflightSql = [IO.File]::ReadAllText($preflight,$utf8)
$report = Invoke-Sql $preflightSql
Set-Content -Encoding utf8 -LiteralPath (Join-Path $EvidenceDir 'preflight.tsv') -Value $report
Test-Preflight $report
$initialStructure = Get-ExecutionStructure
Test-MesB01ExecutionStructure $initialStructure $schemaContract -RequireFull:$VerifyOnly
Set-Content -Encoding utf8 -LiteralPath (Join-Path $EvidenceDir 'execution-schema-preflight.tsv') -Value $initialStructure

if ($VerifyOnly) {
    Save-Json ([ordered]@{status='verified'; database=$DatabaseName; container=$ContainerName; executionSchemaSha256=(Get-TextSha $initialStructure); legacy=(Get-OldSnapshot)}) 'result.json'
    Write-Host "B01 read-only full schema verification completed: $EvidenceDir"
    return
}

$backup = Join-Path $EvidenceDir 'isolated-before.sql'
# Single transaction and no locks on source; this is the isolated copy only.
Invoke-Container 'exec mysqldump --single-transaction --quick --hex-blob --routines --triggers --default-character-set=utf8mb4 -uroot -p"$MYSQL_ROOT_PASSWORD" "$1"' $null $backup | Out-Null
$before = Get-OldSnapshot
Save-Json ([ordered]@{database=$DatabaseName; container=$ContainerName; backupSha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $backup).Hash; legacy=$before}) 'before.json'

# Demonstrate rejected preflight in the same rolled-back isolated transaction before adding uniqueness.
$indexCount = (Invoke-Sql "SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=DATABASE() AND table_name='wm_sn' AND index_name='uk_wm_sn_code';").Trim()
if ($indexCount -eq '0') {
    $conflict = Invoke-Sql ("START TRANSACTION; INSERT INTO wm_sn(sn_id,sn_code,item_id) VALUES (-900000001,'B01-PREFLIGHT-PROBE',1),(-900000002,'b01-preflight-probe ',1);`n" + $preflightSql + "`nROLLBACK;")
    Set-Content -Encoding utf8 -LiteralPath (Join-Path $EvidenceDir 'preflight-conflict.tsv') -Value $conflict
    if ($conflict -notmatch '(?m)^sn_duplicate_groups\t0\t[1-9]') { throw 'Duplicate SN preflight did not report the conflict.' }
    $rejected = $false
    try { Test-Preflight $conflict } catch { $rejected=$true }
    if (-not $rejected) { throw 'Migration gate accepted a conflicting SN report.' }
    Test-Preflight (Invoke-Sql $preflightSql)
}

for ($pass = 1; $pass -le 2; $pass++) {
    Test-Preflight (Invoke-Sql $preflightSql)
    Test-MesB01ExecutionStructure (Get-ExecutionStructure) $schemaContract
    foreach ($migration in $migrations) {
        Write-Host "B01 pass ${pass}: $migration"
        $response = Invoke-Sql ([IO.File]::ReadAllText((Join-Path $sqlRoot $migration),$utf8))
        Set-Content -Encoding utf8 -LiteralPath (Join-Path $EvidenceDir "pass-$pass-$migration.log") -Value $response
    }
    $after = Get-OldSnapshot
    Save-Json $after "legacy-after-$pass.json"
    if ($before.schemaSha256 -ne $after.schemaSha256 -or $before.dataSha256 -ne $after.dataSha256) {
        throw "Legacy schema or data changed on pass $pass. Stop; use the isolated snapshot and audit the partial migration."
    }
    $newSchema = Get-ExecutionStructure
    Test-MesB01ExecutionStructure $newSchema $schemaContract -RequireFull
    Set-Content -Encoding utf8 -LiteralPath (Join-Path $EvidenceDir "execution-schema-$pass.tsv") -Value $newSchema
    $structureHash = Get-TextSha $newSchema
    if ($pass -eq 1) { $firstStructureHash = $structureHash }
    elseif ($firstStructureHash -ne $structureHash) { throw 'B01 schema differs after reapplication.' }
}
Test-Preflight (Invoke-Sql $preflightSql)
Save-Json ([ordered]@{status='passed'; database=$DatabaseName; container=$ContainerName; migrationPasses=2; legacySchemaSha256=$before.schemaSha256; legacyDataSha256=$before.dataSha256; executionSchemaSha256=$firstStructureHash}) 'result.json'
Write-Host "B01 migration and two-pass historical integrity verification completed: $EvidenceDir"
