$ErrorActionPreference = 'Stop'

$workspaceRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $workspaceRoot
. .\use-local-secrets.ps1

$baseUrl = 'http://127.0.0.1:18080'
$suffix = (Get-Date -Format 'MMddHHmmss') + ([guid]::NewGuid().ToString('N').Substring(0, 4))
$userName = "s301_read_$suffix"
$roleKey = "s301_read_$suffix"
$password = 'A9!' + [guid]::NewGuid().ToString('N')
$roleId = $null
$userId = $null

function Invoke-Api($method, $path, $headers, $body = $null) {
    $params = @{ Uri = "$baseUrl$path"; Method = $method; Headers = $headers; TimeoutSec = 15 }
    if ($null -ne $body) {
        $params.ContentType = 'application/json'
        $params.Body = ($body | ConvertTo-Json -Depth 8 -Compress)
    }
    return Invoke-RestMethod @params
}

function Assert-Code($result, $expected, $label) {
    if ($result.code -ne $expected) {
        throw "$label returned business code $($result.code), expected $expected"
    }
}

function Login($name, $secret) {
    $result = Invoke-Api 'Post' '/login' @{} @{ username = $name; password = $secret; code = ''; uuid = '' }
    Assert-Code $result 200 "login $name"
    if (-not $result.token) { throw "login $name did not return a token" }
    return @{ Authorization = "Bearer $($result.token)" }
}

try {
    $adminHeaders = Login $env:APP_ADMIN_USERNAME $env:APP_ADMIN_PASSWORD

    $role = Invoke-Api 'Post' '/system/role' $adminHeaders @{
        roleName = "S3-01 read $suffix"
        roleKey = $roleKey
        roleSort = '99'
        dataScope = '1'
        status = '0'
        menuIds = @(2153, 2200)
        remark = 'S3-01 isolated permission smoke test'
    }
    Assert-Code $role 200 'create temporary role'
    $roles = Invoke-Api 'Get' "/system/role/list?roleKey=$roleKey" $adminHeaders
    $roleId = @($roles.rows | Where-Object { $_.roleKey -eq $roleKey })[0].roleId
    if (-not $roleId) { throw 'temporary role ID not found' }

    $user = Invoke-Api 'Post' '/system/user' $adminHeaders @{
        deptId = 100
        userName = $userName
        nickName = 'S3-01 read test'
        password = $password
        status = '0'
        roleIds = @(2)
        remark = 'S3-01 isolated permission smoke test'
    }
    Assert-Code $user 200 'create temporary user'
    $users = Invoke-Api 'Get' "/system/user/list?userName=$userName" $adminHeaders
    $userId = @($users.rows | Where-Object { $_.userName -eq $userName })[0].userId
    if (-not $userId) { throw 'temporary user ID not found' }

    $ordinaryHeaders = Login $userName $password
    foreach ($path in @('/mes/wm/itemrecpt/930001', '/mes/wm/issueheader/930001')) {
        $result = Invoke-Api 'Get' $path $ordinaryHeaders
        Assert-Code $result 403 "ordinary user without inventory permission: $path"
        Write-Output "DENIED $path code=$($result.code)"
    }

    $grant = Invoke-RestMethod -Uri "$baseUrl/system/user/authRole" -Method Put -Headers $adminHeaders -ContentType 'application/x-www-form-urlencoded' -Body "userId=$userId&roleIds=$roleId" -TimeoutSec 15
    Assert-Code $grant 200 'grant temporary read role'
    $ordinaryHeaders = Login $userName $password
    $info = Invoke-Api 'Get' '/getInfo' $ordinaryHeaders
    Assert-Code $info 200 'ordinary user info'
    $permissions = @($info.permissions)
    foreach ($perm in @('mes:wm:itemrecpt:query', 'mes:wm:issueheader:query')) {
        if ($perm -notin $permissions) { throw "missing expected permission: $perm" }
    }
    foreach ($perm in @('mes:wm:itemrecpt:edit', 'mes:wm:issueheader:edit')) {
        if ($perm -in $permissions) { throw "unexpected write permission: $perm" }
    }
    foreach ($entry in @(
        @{ path = '/mes/wm/itemrecpt/930001'; idField = 'recptId' },
        @{ path = '/mes/wm/issueheader/930001'; idField = 'issueId' }
    )) {
        $result = Invoke-Api 'Get' $entry.path $ordinaryHeaders
        Assert-Code $result 200 "ordinary user with inventory query permission: $($entry.path)"
        if ($result.data.($entry.idField) -ne 930001 -or $result.data.status -ne 'APPROVED') {
            throw "unexpected document returned from $($entry.path)"
        }
        Write-Output "ALLOWED $($entry.path) code=$($result.code) id=930001 status=APPROVED"
    }
    Write-Output "PERMISSIONS query=present edit=absent"
    foreach ($path in @('/mes/wm/itemrecpt/999999999', '/mes/wm/issueheader/999999999')) {
        $result = Invoke-Api 'Put' $path $ordinaryHeaders
        Assert-Code $result 403 "query-only user cannot call execute: $path"
        Write-Output "EXECUTE_DENIED $path code=$($result.code)"
    }
}
finally {
    if ($adminHeaders) {
        if ($userId) {
            $deletedUser = Invoke-Api 'Delete' "/system/user/$userId" $adminHeaders
            Write-Output "CLEANUP user_id=$userId code=$($deletedUser.code)"
        }
        if ($roleId) {
            $deletedRole = Invoke-Api 'Delete' "/system/role/$roleId" $adminHeaders
            Write-Output "CLEANUP role_id=$roleId code=$($deletedRole.code)"
        }
    }
}
