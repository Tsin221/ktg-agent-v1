$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$demo = Join-Path $root '.runtime/mes-demo'
$statePath = Join-Path $demo 'service-pids.json'
$state = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
$s = Get-Content -Raw -LiteralPath (Join-Path $demo 'runtime-secrets.json') | ConvertFrom-Json
$nextJar = Join-Path $root '.runtime/mes-b06-regression/ktg-admin-b06-d1-validation.jar'
$oldJar = [string]$state.jar
$listeners = @(Get-NetTCPConnection -LocalPort 18090 -State Listen -ErrorAction SilentlyContinue)
if ($s.mysqlDatabase -ne 'ktg_mes_demo' -or $state.database -ne 'ktg_mes_demo' -or
    $state.backendPort -ne 18090 -or $listeners.Count -ne 1 -or
    $listeners[0].LocalAddress -ne '127.0.0.1' -or $listeners[0].OwningProcess -ne $state.backendPid -or
    !(Test-Path -LiteralPath $nextJar) -or !(Test-Path -LiteralPath $oldJar) -or
    (docker port ktg-mes-b03-disposable-mysql 3306) -ne '127.0.0.1:33308') { throw 'Dedicated demo identity mismatch.' }
$old = Get-Process -Id $state.backendPid
$command = (Get-CimInstance Win32_Process -Filter "ProcessId = $($old.Id)").CommandLine
if ($old.ProcessName -ne 'java' -or !$command.Contains($oldJar) -or !$command.Contains('--server.port=18090')) { throw 'Unexpected demo backend process.' }
if ($oldJar -eq $nextJar) { Write-Output 'D1 backend already selected.'; return }
$env:MYSQL_HOST='127.0.0.1'; $env:MYSQL_PORT='33308'; $env:MYSQL_DATABASE=$s.mysqlDatabase; $env:MYSQL_APP_USER=$s.mysqlUser; $env:MYSQL_APP_PASSWORD=$s.mysqlPassword
$env:REDIS_HOST='127.0.0.1'; $env:REDIS_PORT='36387'; $env:REDIS_PASSWORD=$s.redisPassword; $env:APP_TOKEN_SECRET=$s.tokenSecret
$env:DRUID_ADMIN_USERNAME='isolated_disabled'; $env:DRUID_ADMIN_PASSWORD=[guid]::NewGuid().ToString('N'); $env:MINIO_URL='http://127.0.0.1:19000'; $env:MINIO_ACCESS_KEY='isolated-placeholder'; $env:MINIO_SECRET_KEY=[guid]::NewGuid().ToString('N'); $env:MINIO_BUCKET='ktg-mes-demo'
$env:KTG_UPLOAD_DIR=Join-Path $demo 'upload'
. (Join-Path $root 'use-java8.ps1')
function Start-Backend([string]$Jar) {
    $stamp = (Get-Date).ToString('yyyyMMdd-HHmmss-fff')
    Start-Process -FilePath (Get-Command java.exe).Source -ArgumentList @('-jar',('"'+$Jar+'"'),'--server.address=127.0.0.1','--server.port=18090','--spring.datasource.druid.statViewServlet.enabled=false','--ktg-mes.demoEnabled=false') -WorkingDirectory (Join-Path $root 'ktg-mes-master/ktg-mes-master') -RedirectStandardOutput (Join-Path $demo "backend-$stamp.out.log") -RedirectStandardError (Join-Path $demo "backend-$stamp.err.log") -WindowStyle Hidden -PassThru
}
function Wait-Backend($Process) {
    $deadline = (Get-Date).AddSeconds(90)
    do {
        if ($Process.HasExited) { return $false }
        try { $status = [int](Invoke-WebRequest 'http://127.0.0.1:18090/getInfo' -UseBasicParsing -TimeoutSec 2).StatusCode } catch { $status = 0; if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode } }
        if ($status -in @(200,401,403)) {
            $ls = @(Get-NetTCPConnection -LocalPort 18090 -State Listen -ErrorAction SilentlyContinue)
            if ($ls.Count -eq 1 -and $ls[0].OwningProcess -eq $Process.Id -and $ls[0].LocalAddress -eq '127.0.0.1') { return $true }
        }
        Start-Sleep -Milliseconds 300
    } while ((Get-Date) -lt $deadline)
    return $false
}
function Save-Backend($Process, [string]$Jar) {
    $state.backendPid = $Process.Id; $state.jar = $Jar
    $state | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
}
Stop-Process -Id $old.Id
if (!$old.WaitForExit(5000)) { throw 'Prior backend did not exit.' }
$replacement = $null
try {
    $replacement = Start-Backend $nextJar
    if (!(Wait-Backend $replacement)) { throw 'D1 backend did not become ready.' }
    Save-Backend $replacement $nextJar
    Write-Output "D1 demo backend ready (PID $($replacement.Id)); existing database and Redis retained."
} catch {
    if ($replacement -and !$replacement.HasExited) { Stop-Process -Id $replacement.Id; $replacement.WaitForExit(5000) | Out-Null }
    $rollback = Start-Backend $oldJar
    if (!(Wait-Backend $rollback)) { throw 'D1 backend and previous backend failed readiness.' }
    Save-Backend $rollback $oldJar
    throw 'D1 backend failed readiness; previous backend restored.'
}
