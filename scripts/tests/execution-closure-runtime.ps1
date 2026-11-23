param(
    [Parameter(Mandatory=$true)][ValidateSet('start','stop')][string]$Action,
    [string]$Jar,
    [switch]$ApproveIsolatedB06
)
$ErrorActionPreference='Stop'
if(!$ApproveIsolatedB06){throw 'Explicit approval of the isolated B06 target required.'}
$root06=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$run06=Join-Path $root06 '.runtime/mes-b06-closure'
$pidFile06=Join-Path $run06 'backend-pid.json'
$redis06='ktg-mes-b06-closure-redis'
if($Action -eq 'stop'){
    if(Test-Path -LiteralPath $pidFile06){
        $old06=Get-Content -Raw -LiteralPath $pidFile06|ConvertFrom-Json
        $proc06=Get-CimInstance Win32_Process -Filter "ProcessId=$($old06.pid)" -ErrorAction SilentlyContinue
        if($proc06 -and $proc06.CommandLine -match '--server.port=18088' -and $proc06.CommandLine -like "*$($old06.jar)*"){
            Stop-Process -Id $old06.pid
        } elseif($proc06){throw 'Recorded PID does not match B06 isolated backend.'}
        Remove-Item -LiteralPath $pidFile06 -Force
    }
    if((docker ps -a --filter "name=^/$redis06$" --format '{{.Names}}') -eq $redis06){
        docker rm -f $redis06 | Out-Null
        if($LASTEXITCODE -ne 0){throw 'B06 isolated Redis cleanup failed'}
    }
    return
}
if(!$Jar -or !(Test-Path -LiteralPath $Jar -PathType Leaf)){throw 'Supply exact isolated JAR path.'}
$Jar=(Resolve-Path -LiteralPath $Jar).Path
if(!$Jar.StartsWith((Join-Path $root06 '.runtime'),[StringComparison]::OrdinalIgnoreCase)){throw 'JAR must be in isolated runtime directory.'}
foreach($port06 in @(18088,36384)){
    if(Get-NetTCPConnection -LocalPort $port06 -State Listen -ErrorAction SilentlyContinue){throw "Port $port06 in use"}
}
if((docker port ktg-mes-b03-disposable-mysql 3306) -ne '127.0.0.1:33308'){throw 'Wrong isolated DB container/port'}
$secret06=Get-Content -Raw -LiteralPath (Join-Path $run06 'credentials.json')|ConvertFrom-Json
docker run --pull=never --name $redis06 -p 127.0.0.1:36384:6379 -d redis:7.4.11-alpine redis-server --requirepass $secret06.redisPassword | Out-Null
if($LASTEXITCODE -ne 0){throw 'Isolated Redis launch failed'}
$env:MYSQL_HOST='127.0.0.1';$env:MYSQL_PORT='33308';$env:MYSQL_DATABASE='ktg_mes_b06_live'
$env:MYSQL_APP_USER=$secret06.mysqlUser;$env:MYSQL_APP_PASSWORD=$secret06.mysqlPassword
$env:REDIS_HOST='127.0.0.1';$env:REDIS_PORT='36384';$env:REDIS_PASSWORD=$secret06.redisPassword
$env:APP_TOKEN_SECRET=$secret06.tokenSecret
$env:DRUID_ADMIN_USERNAME='b06_disabled';$env:DRUID_ADMIN_PASSWORD=[guid]::NewGuid().ToString('N')
$env:MINIO_URL='http://127.0.0.1:19000';$env:MINIO_ACCESS_KEY='b06-placeholder';$env:MINIO_SECRET_KEY=[guid]::NewGuid().ToString('N');$env:MINIO_BUCKET='b06-isolated'
$env:KTG_UPLOAD_DIR=Join-Path $run06 'upload';New-Item -ItemType Directory -Force $env:KTG_UPLOAD_DIR|Out-Null
Set-Location $root06; . .\use-java8.ps1
$proc06=Start-Process -FilePath (Get-Command java.exe).Source -ArgumentList @('-jar',$Jar,'--server.address=127.0.0.1','--server.port=18088','--spring.datasource.druid.statViewServlet.enabled=false','--ktg-mes.demoEnabled=false') -WorkingDirectory (Join-Path $root06 'ktg-mes-master/ktg-mes-master') -RedirectStandardOutput (Join-Path $run06 'backend.log') -RedirectStandardError (Join-Path $run06 'backend-error.log') -WindowStyle Hidden -PassThru
@{pid=$proc06.Id;jar=$Jar}|ConvertTo-Json|Set-Content -LiteralPath $pidFile06 -Encoding utf8
Write-Output 'B06 isolated backend started on 18088; verify health before requests.'
