param(
    [Parameter(Mandatory = $true)][long]$IssueId,
    [string]$BaseUrl = 'http://127.0.0.1:18081'
)
$ErrorActionPreference = 'Stop'
$workspaceRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $workspaceRoot 'use-local-secrets.ps1')

function Api($method, $path, $headers, $body = $null) {
    $args = @{ Uri = "$BaseUrl$path"; Method = $method; Headers = $headers;
               TimeoutSec = 25; SkipHttpErrorCheck = $true }
    if ($null -ne $body) {
        $args.ContentType = 'application/json'
        $args.Body = $body | ConvertTo-Json -Depth 8 -Compress
    }
    return Invoke-RestMethod @args
}
function RequireCode($result, $code, $label) {
    if ($result.code -ne $code) { throw "$label returned $($result.code), expected ${code}: $($result.msg)" }
}
function Login($name, $secret) {
    $result = Api 'Post' '/login' @{} @{ username = $name; password = $secret; code = ''; uuid = '' }
    RequireCode $result 200 "login $name"
    return @{ Authorization = "Bearer $($result.token)" }
}

$suffix = (Get-Date -Format 'MMddHHmmss') + ([guid]::NewGuid().ToString('N').Substring(0, 4))
$userName = "s306_exec_$suffix"
$roleKey = "s306_exec_$suffix"
$password = 'A9!' + [guid]::NewGuid().ToString('N')
$roleId = $null
$userId = $null
$admin = Login $env:APP_ADMIN_USERNAME $env:APP_ADMIN_PASSWORD
try {
    $role = Api 'Post' '/system/role' $admin @{
        roleName = "S3-06 issue $suffix"; roleKey = $roleKey
        roleSort = '99'; dataScope = '1'; status = '0'
        menuIds = @(2200, 2201); remark = 'S3-06 isolated issue acceptance'
    }
    RequireCode $role 200 'create role'
    $roles = Api 'Get' "/system/role/list?roleKey=$roleKey" $admin
    $roleId = @($roles.rows | Where-Object { $_.roleKey -eq $roleKey })[0].roleId
    if (-not $roleId) { throw 'Temporary role not found' }

    $created = Api 'Post' '/system/user' $admin @{
        deptId = 100; userName = $userName; nickName = 'S3-06 execute test'
        password = $password; status = '0'; roleIds = @(2)
        remark = 'S3-06 isolated issue acceptance'
    }
    RequireCode $created 200 'create user'
    $users = Api 'Get' "/system/user/list?userName=$userName" $admin
    $userId = @($users.rows | Where-Object { $_.userName -eq $userName })[0].userId
    if (-not $userId) { throw 'Temporary user not found' }

    $ordinary = Login $userName $password
    RequireCode (Api 'Get' "/mes/wm/issueheader/$IssueId/execution-preview" $ordinary) 403 'unprivileged preview'
    RequireCode (Api 'Put' "/mes/wm/issueheader/$IssueId" $ordinary) 403 'unprivileged execute'
    Write-Output 'UNPRIVILEGED_PREVIEW_AND_EXECUTE=403'

    $grantArgs = @{ Uri = "$BaseUrl/system/user/authRole"; Method = 'Put'; Headers = $admin;
                    ContentType = 'application/x-www-form-urlencoded';
                    Body = "userId=$userId&roleIds=$roleId"; TimeoutSec = 20 }
    $grant = Invoke-RestMethod @grantArgs
    RequireCode $grant 200 'grant issue role'
    $authorized = Login $userName $password
    $info = Api 'Get' '/getInfo' $authorized
    if ('mes:wm:issueheader:edit' -notin @($info.permissions)) { throw 'Edit permission missing' }
    $preview = Api 'Get' "/mes/wm/issueheader/$IssueId/execution-preview" $authorized
    RequireCode $preview 200 'authorized preview'
    if ($preview.data.movements.Count -ne 2 -or $preview.data.documentStatus -ne 'APPROVED') {
        throw 'Issue preview is not an approved two-line document'
    }
    $id = [guid]::NewGuid().ToString()
    $headers = @{
        Authorization = $authorized.Authorization
        'X-Operation-Id' = $id
        'X-Document-Version' = [string]$preview.data.documentVersion
    }
    $first = Api 'Put' "/mes/wm/issueheader/$IssueId" $headers
    RequireCode $first 200 'first execute'
    $result = Api 'Get' "/mes/wm/issueheader/$IssueId/operations/$id" $authorized
    RequireCode $result 200 'result query'
    if ($result.data.operationStatus -ne 'succeeded' -or $result.data.documentStatus -ne 'FINISHED' -or
        $result.data.transactions.Count -ne 4) { throw 'Persisted result is inconsistent' }

    $replay = Api 'Post' '/mes/wm/inventory-operations' $authorized @{
        actionType = 'PRODUCTION_ISSUE_EXECUTE'; documentId = $IssueId
        operationId = $id; expectedVersion = $preview.data.documentVersion
    }
    RequireCode $replay 200 'explicit route replay'
    $other = Api 'Post' '/mes/wm/inventory-operations' $authorized @{
        actionType = 'PRODUCTION_ISSUE_EXECUTE'; documentId = $IssueId
        operationId = [guid]::NewGuid().ToString()
    }
    RequireCode $other 409 'different-operation duplicate'
    if ($other.errorCode -ne 'DOCUMENT_ALREADY_EXECUTED' -or
        $other.data.existingOperationId -ne $id) { throw 'Duplicate did not name original operation' }
    Write-Output "ISSUE_ID=$IssueId OPERATION_ID=$id VERSION=$($preview.data.documentVersion)"
    Write-Output "FIRST=succeeded RESULT=succeeded REPLAY=succeeded DUPLICATE=$($other.errorCode)"
    foreach ($tx in $result.data.transactions) {
        Write-Output "TX=$($tx.transactionId) LINE=$($tx.sourceDocLineId) TYPE=$($tx.transactionType) QUANTITY=$($tx.transactionQuantity)"
    }
}
finally {
    if ($userId) {
        $deleted = Api 'Delete' "/system/user/$userId" $admin
        Write-Output "CLEANUP_USER=$userId CODE=$($deleted.code)"
    }
    if ($roleId) {
        $deleted = Api 'Delete' "/system/role/$roleId" $admin
        Write-Output "CLEANUP_ROLE=$roleId CODE=$($deleted.code)"
    }
}
