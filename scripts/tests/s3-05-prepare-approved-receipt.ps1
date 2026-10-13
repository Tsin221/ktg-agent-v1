param(
    [string]$BaseUrl = 'http://127.0.0.1:18081',
    [string]$SharedBatchCode = '',
    [switch]$NoLines
)
$ErrorActionPreference = 'Stop'
$workspaceRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $workspaceRoot 'use-local-secrets.ps1')

function Api($method, $path, $headers, $body = $null) {
    $args = @{ Uri = "$BaseUrl$path"; Method = $method; Headers = $headers; TimeoutSec = 20 }
    if ($null -ne $body) {
        $args.ContentType = 'application/json'
        $args.Body = $body | ConvertTo-Json -Depth 8 -Compress
    }
    $result = Invoke-RestMethod @args
    if ($result.code -ne 200) { throw "$method $path returned business code $($result.code): $($result.msg)" }
    return $result
}

$login = Api 'Post' '/login' @{} @{
    username = $env:APP_ADMIN_USERNAME; password = $env:APP_ADMIN_PASSWORD; code = ''; uuid = ''
}
$headers = @{ Authorization = "Bearer $($login.token)" }
$source = (Api 'Get' '/mes/wm/itemrecpt/930001' $headers).data
$sourceLine = (Api 'Get' '/mes/wm/itemrecptline/930001' $headers).data
if ($source.status -ne 'APPROVED' -or $sourceLine.itemId -ne 3) { throw 'Baseline fixture changed' }

$suffix = (Get-Date -Format 'MMddHHmmss') + ([guid]::NewGuid().ToString('N').Substring(0, 4))
$code = "S305-FLOW-IR-$suffix"
$header = @{
    recptCode = $code; recptName = "S3-05 flow receipt $suffix"
    recptDate = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'); status = 'PREPARE'
    vendorId = $source.vendorId; vendorCode = $source.vendorCode
    vendorName = $source.vendorName; vendorNick = $source.vendorNick
    warehouseId = 930001; locationId = 930001; areaId = 930001
    remark = 'S3-05 isolated acceptance flow sample'
}
Api 'Post' '/mes/wm/itemrecpt' $headers $header | Out-Null
$matches = @((Api 'Get' "/mes/wm/itemrecpt/list?recptCode=$code" $headers).rows | Where-Object { $_.recptCode -eq $code })
if ($matches.Count -ne 1) { throw 'Created receipt cannot be found uniquely' }
$id = $matches[0].recptId

$firstBatch = if ($SharedBatchCode) { $SharedBatchCode } else { "S305-R1-$suffix" }
$secondBatch = if ($SharedBatchCode) { $SharedBatchCode } else { "S305-R2-$suffix" }
$entries = @()
if (-not $NoLines) {
    $entries = @(@{ batch = $firstBatch; quantity = 2.00 },
                 @{ batch = $secondBatch; quantity = 1.50 })
}
foreach ($entry in $entries) {
    Api 'Post' '/mes/wm/itemrecptline' $headers @{
        recptId = $id; itemId = $sourceLine.itemId; itemCode = $sourceLine.itemCode
        itemName = $sourceLine.itemName; specification = $sourceLine.specification
        unitOfMeasure = $sourceLine.unitOfMeasure; unitName = $sourceLine.unitName
        quantityRecived = $entry.quantity; batchCode = $entry.batch
        warehouseId = 930001; locationId = 930001; areaId = 930001
        iqcCheck = 'N'
    } | Out-Null
}

foreach ($nextStatus in @('APPROVING', 'APPROVED')) {
    $current = (Api 'Get' "/mes/wm/itemrecpt/$id" $headers).data
    Api 'Put' '/mes/wm/itemrecpt' $headers @{
        recptId = $id; version = $current.version; status = $nextStatus
    } | Out-Null
}
$approved = (Api 'Get' "/mes/wm/itemrecpt/$id" $headers).data
if ($NoLines) {
    if ($approved.status -ne 'APPROVED') { throw 'Empty receipt did not reach approved status' }
    Write-Output "RECEIPT_ID=$id"
    Write-Output "RECEIPT_CODE=$code"
    Write-Output "STATUS=$($approved.status) VERSION=$($approved.version) LINES=0"
    return
}
$preview = (Api 'Get' "/mes/wm/itemrecpt/$id/execution-preview" $headers).data
if ($approved.status -ne 'APPROVED' -or $preview.additions.Count -ne 2 -or
    $preview.documentVersion -ne $approved.version) { throw 'Approved preview mismatch' }
Write-Output "RECEIPT_ID=$id"
Write-Output "RECEIPT_CODE=$code"
Write-Output "STATUS=$($approved.status) VERSION=$($approved.version) LINES=$($preview.additions.Count)"
foreach ($line in $preview.additions) {
    Write-Output "LINE=$($line.sourceDocLineId) BATCH=$($line.batchCode) QUANTITY=$($line.quantity) LOCATION=$($line.warehouseId)/$($line.locationId)/$($line.areaId)"
}
