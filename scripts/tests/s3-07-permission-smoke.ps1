param(
    [long]$ReceiptId,
    [string]$ExistingOperationId = 'ba567d67-d524-479a-abe2-f3f9c51175c2',
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
    if ($result.code -ne $code) { throw "$label returned $($result.code), expected $code" }
}
function Login($name, $secret) {
    $result = Api 'Post' '/login' @{} @{ username=$name; password=$secret; code=''; uuid='' }
    RequireCode $result 200 'login'
    return @{ Authorization = "Bearer $($result.token)" }
}

$suffix = (Get-Date -Format 'MMddHHmmss') + [guid]::NewGuid().ToString('N').Substring(0, 4)
$userName = "s307_check_$suffix"
$roleKey = "s307_check_$suffix"
$password = 'A9!' + [guid]::NewGuid().ToString('N')
$roleId = $null
$userId = $null
$admin = Login $env:APP_ADMIN_USERNAME $env:APP_ADMIN_PASSWORD
try {
    $role = Api 'Post' '/system/role' $admin @{
        roleName="S3-07 query $suffix"; roleKey=$roleKey; roleSort='99';
        dataScope='1'; status='0'; menuIds=@(2152,2153,2155,2157,2158);
        remark='S3-07 isolated permission smoke'
    }
    RequireCode $role 200 'create role'
    $roles = Api 'Get' "/system/role/list?roleKey=$roleKey" $admin
    $roleId = @($roles.rows | Where-Object { $_.roleKey -eq $roleKey })[0].roleId
    if (-not $roleId) { throw 'Temporary role not found' }

    $created = Api 'Post' '/system/user' $admin @{
        deptId=100; userName=$userName; nickName='S3-07 query test';
        password=$password; status='0'; roleIds=@(2);
        remark='S3-07 isolated permission smoke'
    }
    RequireCode $created 200 'create user'
    $users = Api 'Get' "/system/user/list?userName=$userName" $admin
    $userId = @($users.rows | Where-Object { $_.userName -eq $userName })[0].userId
    if (-not $userId) { throw 'Temporary user not found' }

    $unprivileged = Login $userName $password
    RequireCode (Api 'Get' "/mes/wm/inventory-operations/$ExistingOperationId" $unprivileged) 403 'no read permission'
    Write-Output 'NO_READ_PERMISSION=403'

    $grant = Invoke-RestMethod -Uri "$BaseUrl/system/user/authRole" -Method Put -Headers $admin `
        -ContentType 'application/x-www-form-urlencoded' -Body "userId=$userId&roleIds=$roleId"
    RequireCode $grant 200 'grant role'
    $authorized = Login $userName $password
    $info = Api 'Get' '/getInfo' $authorized
    foreach ($permission in @('mes:wm:itemrecpt:query','mes:wm:itemrecpt:edit','mes:wm:wmstock:list')) {
        if ($permission -notin @($info.permissions)) { throw "Missing $permission" }
    }
    RequireCode (Api 'Get' "/mes/wm/inventory-operations/$ExistingOperationId" $authorized) 404 'other actor receipt'
    Write-Output 'OTHER_ACTOR_RECEIPT=404'

    $fact = Api 'Get' "/mes/wm/inventory-operations/documents/ITEM_RECEIPT_EXECUTE/$ReceiptId" $authorized
    RequireCode $fact 200 'document fact'
    if ($fact.data.operationStatus -ne 'unknown' -or $fact.data.safeToRetry -ne $false) {
        throw 'Unclaimed document was not conservatively unknown'
    }
    Write-Output 'UNCLAIMED_DOCUMENT=unknown'

    $preview = Api 'Get' "/mes/wm/itemrecpt/$ReceiptId/execution-preview" $authorized
    RequireCode $preview 200 'preview'
    $operationId = [guid]::NewGuid().ToString()
    $command = @{
        actionType='ITEM_RECEIPT_EXECUTE'; documentId=$ReceiptId;
        operationId=$operationId; expectedStatus='APPROVED';
        expectedVersion=$preview.data.documentVersion
    }
    $executed = Api 'Post' '/mes/wm/inventory-operations' $authorized $command
    RequireCode $executed 200 'execute'
    $result = Api 'Get' "/mes/wm/inventory-operations/$operationId" $authorized
    RequireCode $result 200 'own receipt'
    if ($result.data.operationStatus -ne 'succeeded' -or $result.data.documentStatus -ne 'FINISHED' `
        -or @($result.data.transactions).Count -ne 2) { throw 'Own receipt verification failed' }
    Write-Output "OWN_OPERATION_ID=$operationId"
    Write-Output 'OWN_RECEIPT=succeeded'
} finally {
    if ($userId) { Api 'Delete' "/system/user/$userId" $admin | Out-Null }
    if ($roleId) { Api 'Delete' "/system/role/$roleId" $admin | Out-Null }
}
