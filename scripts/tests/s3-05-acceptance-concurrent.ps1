param(
    [long[]]$ReceiptIds = @(),
    [switch]$ExpectDuplicate,
    [string]$BaseUrl = 'http://127.0.0.1:18081'
)
$ErrorActionPreference = 'Stop'
if ($ReceiptIds.Count -ne 2 -or (($ReceiptIds[0] -eq $ReceiptIds[1]) -ne $ExpectDuplicate.IsPresent)) {
    throw 'Provide two different IDs for stock contention, or repeat one ID with -ExpectDuplicate'
}
$workspaceRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $workspaceRoot 'use-local-secrets.ps1')
$login = Invoke-RestMethod -Uri "$BaseUrl/login" -Method Post -ContentType 'application/json' -Body (@{
    username = $env:APP_ADMIN_USERNAME; password = $env:APP_ADMIN_PASSWORD; code = ''; uuid = ''
} | ConvertTo-Json -Compress)
if ($login.code -ne 200) { throw "Login failed: $($login.code)" }
$token = $login.token
$operations = @()
foreach ($receiptId in $ReceiptIds) {
    $preview = Invoke-RestMethod -Uri "$BaseUrl/mes/wm/itemrecpt/$receiptId/execution-preview" -Headers @{ Authorization = "Bearer $token" }
    if ($preview.code -ne 200 -or $preview.data.documentStatus -ne 'APPROVED' -or
        $preview.data.additions.Count -ne 2) { throw "Receipt $receiptId is not an approved two-line sample" }
    $operations += [pscustomobject]@{
        ReceiptId = $receiptId
        Version = $preview.data.documentVersion
        OperationId = [guid]::NewGuid().ToString()
    }
}
$barrier = [System.Threading.ManualResetEventSlim]::new($false)
$jobs = @()
try {
    foreach ($operation in $operations) {
        $jobs += Start-ThreadJob -ArgumentList $barrier, $operation, $BaseUrl, $token -ScriptBlock {
            param($gate, $op, $url, $authToken)
            $gate.Wait()
            $response = Invoke-RestMethod -Uri "$url/mes/wm/itemrecpt/$($op.ReceiptId)" -Method Put -SkipHttpErrorCheck -TimeoutSec 30 -Headers @{
                Authorization = "Bearer $authToken"
                'X-Operation-Id' = $op.OperationId
                'X-Document-Version' = [string]$op.Version
            }
            [pscustomobject]@{ ReceiptId = $op.ReceiptId; OperationId = $op.OperationId;
                                Code = $response.code; ErrorCode = $response.errorCode }
        }
    }
    $barrier.Set()
    $jobs | Wait-Job -Timeout 45 | Out-Null
    if (@($jobs | Where-Object State -ne 'Completed').Count -ne 0) { throw 'Concurrent requests did not complete' }
    $results = @($jobs | Receive-Job)
    $successful = @($results | Where-Object Code -eq 200)
    $duplicates = @($results | Where-Object ErrorCode -eq 'DOCUMENT_ALREADY_EXECUTED')
    if ($results.Count -ne 2 -or
        ($ExpectDuplicate -and ($successful.Count -ne 1 -or $duplicates.Count -ne 1)) -or
        (-not $ExpectDuplicate -and $successful.Count -ne 2)) {
        throw "Concurrent execution failed: $($results | ConvertTo-Json -Compress)"
    }
    foreach ($result in $successful) {
        $stored = Invoke-RestMethod -Uri "$BaseUrl/mes/wm/itemrecpt/$($result.ReceiptId)/operations/$($result.OperationId)" -Headers @{ Authorization = "Bearer $token" }
        if ($stored.code -ne 200 -or $stored.data.operationStatus -ne 'succeeded' -or
            $stored.data.documentStatus -ne 'FINISHED' -or $stored.data.transactions.Count -ne 2) {
            throw "Persisted result mismatch for receipt $($result.ReceiptId)"
        }
        Write-Output "RECEIPT_ID=$($result.ReceiptId) OPERATION_ID=$($result.OperationId) STATUS=succeeded TRANSACTIONS=2"
    }
    if ($ExpectDuplicate) {
        Write-Output "RECEIPT_ID=$($duplicates[0].ReceiptId) OPERATION_ID=$($duplicates[0].OperationId) STATUS=rejected ERROR=DOCUMENT_ALREADY_EXECUTED"
    }
}
finally {
    $barrier.Set()
    $jobs | Remove-Job -Force -ErrorAction SilentlyContinue
    $barrier.Dispose()
}
