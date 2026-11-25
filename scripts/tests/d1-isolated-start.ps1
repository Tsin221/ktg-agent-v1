param([Parameter(Mandatory=$true)][string]$Jar)
$ErrorActionPreference='Stop'
$root=(Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$run=Join-Path $root '.runtime/mes-d1-validation'
$jarPath=(Resolve-Path -LiteralPath $Jar).Path
if(!$jarPath.StartsWith((Join-Path $root '.runtime'),[StringComparison]::OrdinalIgnoreCase)){throw 'Use isolated runtime jar.'}
$s=Get-Content -Raw -LiteralPath (Join-Path $run 'runtime-secrets.json')|ConvertFrom-Json
if($s.mysqlDatabase -ne 'ktg_mes_d1_validation_20261124' -or $s.mysqlUser -ne 'mes_d1_validation_app' -or (docker port ktg-mes-b03-disposable-mysql 3306) -ne '127.0.0.1:33308'){throw 'Invalid D1 isolated target.'}
foreach($p in @(18092,18093,18855,36388)) {
    $tcp=New-Object System.Net.Sockets.TcpClient
    try{$tcp.Connect('127.0.0.1',$p);$occupied=$true}catch{$occupied=$false}finally{$tcp.Close()}
    if($occupied){throw "Port $p already in use."}
}
if(docker ps -a --filter 'name=^/ktg-mes-d1-validation-redis$' --format '{{.Names}}'){throw 'D1 Redis already exists.'}
docker run --pull=never --name ktg-mes-d1-validation-redis -p 127.0.0.1:36388:6379 -d redis:7.4.11-alpine redis-server --requirepass $s.redisPassword | Out-Null
if($LASTEXITCODE -ne 0){throw 'Redis start failed.'}
$env:MYSQL_HOST='127.0.0.1';$env:MYSQL_PORT='33308';$env:MYSQL_DATABASE=$s.mysqlDatabase;$env:MYSQL_APP_USER=$s.mysqlUser;$env:MYSQL_APP_PASSWORD=$s.mysqlPassword
$env:REDIS_HOST='127.0.0.1';$env:REDIS_PORT='36388';$env:REDIS_PASSWORD=$s.redisPassword;$env:APP_TOKEN_SECRET=$s.tokenSecret
$env:DRUID_ADMIN_USERNAME='isolated_disabled';$env:DRUID_ADMIN_PASSWORD=[guid]::NewGuid().ToString('N')
$env:MINIO_URL='http://127.0.0.1:19000';$env:MINIO_ACCESS_KEY='isolated-placeholder';$env:MINIO_SECRET_KEY=[guid]::NewGuid().ToString('N');$env:MINIO_BUCKET='ktg-mes-d1-validation'
$env:KTG_UPLOAD_DIR=Join-Path $run 'upload';New-Item -ItemType Directory -Force $env:KTG_UPLOAD_DIR|Out-Null
. (Join-Path $root 'use-java8.ps1')
$backend=Start-Process -FilePath (Get-Command java.exe).Source -ArgumentList @('-jar',$jarPath,'--server.address=127.0.0.1','--server.port=18092','--spring.datasource.druid.statViewServlet.enabled=false','--ktg-mes.demoEnabled=false') -WorkingDirectory (Join-Path $root 'ktg-mes-master/ktg-mes-master') -RedirectStandardOutput (Join-Path $run 'backend.log') -RedirectStandardError (Join-Path $run 'backend-error.log') -WindowStyle Hidden -PassThru
@{backendPid=$backend.Id;jar=$jarPath;database=$s.mysqlDatabase;backendPort=18092}|ConvertTo-Json|Set-Content -LiteralPath (Join-Path $run 'service-pids.json') -Encoding utf8
Write-Output 'D1 isolated backend started on 18092; verify health before requests.'
