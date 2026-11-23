# No database is contacted: exercise the actual fail-closed preflight validator.
$ErrorActionPreference = 'Stop'
$scripts = Split-Path -Parent $PSScriptRoot
$root = Split-Path -Parent $scripts
. (Join-Path $scripts 'mes-b01-schema-validation.ps1')
$contract = Join-Path $root 'ktg-mes-master\ktg-mes-master\sql\20261123_execution_schema_contract.tsv'
$rows = @(Get-Content -LiteralPath $contract)
function As-Metadata([string[]]$Rows) { ($Rows -join "`n").Replace('|',"`t") }
function Needs-Rejection([string]$Metadata,[switch]$RequireFull) {
    $rejected = $false
    try { Test-MesB01ExecutionStructure $Metadata $contract -RequireFull:$RequireFull }
    catch { $rejected = $true; Write-Host $_.Exception.Message }
    if (-not $rejected) { throw 'Schema preflight accepted invalid metadata.' }
}

$full = As-Metadata $rows
Test-MesB01ExecutionStructure '' $contract
Test-MesB01ExecutionStructure $full $contract -RequireFull
Test-MesB01ExecutionStructure (As-Metadata ($rows | Where-Object { $_ -like 'pro_execution_order|*' })) $contract
$withoutLateFk = As-Metadata ($rows | Where-Object { $_ -notmatch '\|fk_execution_(task|inspection)_disposition\|' })
Test-MesB01ExecutionStructure $withoutLateFk $contract
Needs-Rejection $withoutLateFk -RequireFull
Needs-Rejection (As-Metadata ($rows | Where-Object { $_ -notlike 'pro_execution_task|normal_step_no|*' }))
Needs-Rejection ($full.Replace('decimal(18,2)','decimal(18,4)'))
Needs-Rejection ($full.Replace("normal_step_no`tint(11)`tYES", "normal_step_no`tint(11)`tNO"))
Needs-Rejection ($full + "`npro_execution_bad`tbogus")
Needs-Rejection (As-Metadata ($rows | Where-Object { $_ -notlike 'pro_execution_unit|uk_execution_unit_order_sn|*' }))
Needs-Rejection ($full.Replace("RESTRICT`tRESTRICT","RESTRICT`tCASCADE"))
Needs-Rejection ($full.Replace("fk_execution_unit_task`t2`tcurrent_task_id`ttask_id", "fk_execution_unit_task`t2`tcurrent_task_id`tworkorder_id"))
Needs-Rejection ($full.Replace("table_properties`tInnoDB", "table_properties`tMyISAM"))
Needs-Rejection ($full.Replace("default`tversion`t0", "default`tversion`t1"))
Write-Host 'MES-B01 schema validator: all complete/partial/drift cases passed.'
