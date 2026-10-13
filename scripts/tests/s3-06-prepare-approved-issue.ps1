param(
    [string]$BaseUrl = 'http://127.0.0.1:18081',
    [decimal]$FirstQuantity = 2.00,
    [decimal]$SecondQuantity = 1.00,
    [Parameter(Mandatory = $true)][long[]]$StockIds
)
$ErrorActionPreference = 'Stop'
if ($StockIds.Count -ne 2) { throw 'Provide exactly two source stock IDs' }
$workspaceRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $workspaceRoot 'use-local-secrets.ps1')

function Api($method, $path, $headers, $body = $null) {
    $args = @{ Uri = "$BaseUrl$path"; Method = $method; Headers = $headers;
               TimeoutSec = 25; SkipHttpErrorCheck = $true }
    if ($null -ne $body) {
        $args.ContentType = 'application/json'
        $args.Body = $body | ConvertTo-Json -Depth 8 -Compress
    }
    $result = Invoke-RestMethod @args
    if ($result.code -ne 200) { throw "$method $path returned $($result.code): $($result.msg)" }
    return $result
}

$login = Api 'Post' '/login' @{} @{
    username = $env:APP_ADMIN_USERNAME; password = $env:APP_ADMIN_PASSWORD; code = ''; uuid = ''
}
$headers = @{ Authorization = "Bearer $($login.token)" }
$source = (Api 'Get' '/mes/wm/issueheader/930001' $headers).data
$sourceLines = @($StockIds | ForEach-Object { (Api 'Get' "/mes/wm/wmstock/$_" $headers).data })
if ($source.status -ne 'APPROVED' -or $sourceLines.Count -ne 2) { throw 'Baseline issue fixture changed' }
$suffix = (Get-Date -Format 'MMddHHmmss') + ([guid]::NewGuid().ToString('N').Substring(0, 4))
$code = "S306-FLOW-ISSUE-$suffix"
Api 'Post' '/mes/wm/issueheader' $headers @{
    issueCode = $code; issueName = "S3-06 flow issue $suffix"
    issueDate = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'); status = 'PREPARE'
    workorderId = $source.workorderId; workorderCode = $source.workorderCode
    warehouseId = $source.warehouseId; locationId = $source.locationId; areaId = $source.areaId
    remark = 'S3-06 isolated acceptance flow sample'
} | Out-Null
$matches = @((Api 'Get' "/mes/wm/issueheader/list?issueCode=$code" $headers).rows | Where-Object { $_.issueCode -eq $code })
if ($matches.Count -ne 1) { throw 'Created issue cannot be found uniquely' }
$issueId = $matches[0].issueId
$quantities = @($FirstQuantity, $SecondQuantity)
for ($i = 0; $i -lt 2; $i++) {
    $line = $sourceLines[$i]
    Api 'Post' '/mes/wm/issueline' $headers @{
        issueId = $issueId; materialStockId = $line.materialStockId
        itemId = $line.itemId; itemCode = $line.itemCode; itemName = $line.itemName
        specification = $line.specification; unitOfMeasure = $line.unitOfMeasure
        batchCode = $line.batchCode; quantityIssued = $quantities[$i]
        warehouseId = $line.warehouseId; locationId = $line.locationId; areaId = $line.areaId
    } | Out-Null
}
foreach ($nextStatus in @('APPROVING', 'APPROVED')) {
    $current = (Api 'Get' "/mes/wm/issueheader/$issueId" $headers).data
    Api 'Put' '/mes/wm/issueheader' $headers @{
        issueId = $issueId; version = $current.version; status = $nextStatus
    } | Out-Null
}
$approved = (Api 'Get' "/mes/wm/issueheader/$issueId" $headers).data
Write-Output "ISSUE_ID=$issueId"
Write-Output "ISSUE_CODE=$code"
Write-Output "STATUS=$($approved.status) VERSION=$($approved.version)"
$preview = Invoke-RestMethod -Uri "$BaseUrl/mes/wm/issueheader/$issueId/execution-preview" -Headers $headers -SkipHttpErrorCheck
Write-Output "PREVIEW_CODE=$($preview.code) ERROR=$($preview.errorCode) MOVEMENTS=$($preview.data.movements.Count)"
