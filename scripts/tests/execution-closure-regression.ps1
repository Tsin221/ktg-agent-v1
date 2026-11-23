param([ValidateSet('Prepare','Run','Cleanup')] [string]$Action = 'Run')
$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$run = Join-Path $project '.runtime/mes-b06-regression'
$container = 'ktg-mes-b06-regression-mysql'
$source = 'ktg-mes-b03-disposable-mysql'
$port = '33311'
$db = 'ktg_mes_b01_b06_test'
$secret = Join-Path $run 'mysql-local-secret.txt'
$envFile = Join-Path $run 'mysql-local.env'
$master = Join-Path $project 'ktg-mes-master/ktg-mes-master'

function Check-Exit([string]$label) {
    if ($LASTEXITCODE -ne 0) { throw "$label failed (exit $LASTEXITCODE)" }
}

function Import-Local([string]$file, [string]$database) {
    docker cp $file "${container}:/tmp/b06-regression-import.sql" | Out-Null
    Check-Exit 'Copy isolated SQL fixture'
    docker exec $container sh -c ('mysql -uroot -p"$MYSQL_ROOT_PASSWORD" {0} < /tmp/b06-regression-import.sql 2>/dev/null' -f $database)
    Check-Exit "Import $database"
    docker exec $container rm -f /tmp/b06-regression-import.sql
    Check-Exit 'Remove transient import file'
}

if ($Action -eq 'Prepare') {
    if (Test-Path -LiteralPath $secret) { throw 'Regression secret already exists; refusing overwrite.' }
    $existing = docker ps -a --filter "name=^/$container`$" --format '{{.Names}}'
    Check-Exit 'Check exact disposable container identity'
    if ($existing) { throw 'Regression container already exists; refusing overwrite.' }
    New-Item -ItemType Directory -Path $run -Force | Out-Null
    $password = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
    Set-Content -LiteralPath $secret -Value $password -NoNewline
    try {
        Set-Content -LiteralPath $envFile -Value @("MYSQL_ROOT_PASSWORD=$password", "MYSQL_DATABASE=$db", 'TZ=Asia/Shanghai')
        docker run -d --name $container -p "127.0.0.1:${port}:3306" --env-file $envFile mysql:5.7.44 --default-time-zone=+08:00 | Out-Null
        Check-Exit 'Launch isolated MySQL'
    } finally { if (Test-Path -LiteralPath $envFile) { Remove-Item -LiteralPath $envFile } }
    $ready = $false
    for ($attempt = 0; $attempt -lt 50; $attempt++) {
        docker exec $container sh -c 'mysqladmin -h127.0.0.1 -uroot -p"$MYSQL_ROOT_PASSWORD" ping >/dev/null 2>&1'
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Seconds 1
    }
    if (!$ready) { throw 'Isolated MySQL not ready.' }
    docker exec $container sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "CREATE DATABASE ktg_s304_atomicity_test; CREATE DATABASE ktg_s302_java_test;" 2>/dev/null'
    Check-Exit 'Create fresh regression schemas'
    foreach ($pair in @(@('ktg_mes_b01_b03_disposable',$db,''),@('ktg_s304_atomicity_test','ktg_s304_atomicity_test',''),@('ktg_s303_state_test','ktg_s302_java_test','--no-data'))) {
        $sourceDb = $pair[0]; $targetDb = $pair[1]
        $dumpOption = $pair[2]
        $dump = Join-Path $run "$targetDb-copy.sql"
        $temporary = "/tmp/mes-b06-regression-$targetDb.sql"
        # Source schema is read-only, and the snapshot is imported only into the new container.
        docker exec $source sh -c ('mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --skip-lock-tables {0} {1} > {2} 2>/dev/null' -f $dumpOption,$sourceDb,$temporary)
        Check-Exit "Read-only dump $sourceDb"
        try {
            docker cp "${source}:$temporary" $dump | Out-Null
            Check-Exit "Copy $sourceDb snapshot"
            Import-Local $dump $targetDb
        } finally {
            docker exec $source rm -f $temporary | Out-Null
            if(Test-Path -LiteralPath $dump) { Remove-Item -LiteralPath $dump }
        }
    }
    docker exec $container sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -Nse "SELECT VERSION(); SHOW DATABASES" 2>/dev/null'
    Check-Exit 'Verify isolated schemas'
} elseif ($Action -eq 'Run') {
    if (!(Test-Path -LiteralPath $secret)) { throw 'Prepare the isolated regression instance first.' }
    . (Join-Path $project 'use-java8.ps1')
    $password = (Get-Content -Raw -LiteralPath $secret).Trim()
    $env:MES_B01_MYSQL_URL = "jdbc:mysql://127.0.0.1:$port/$db`?useUnicode=true&characterEncoding=utf8&serverTimezone=Asia/Shanghai&useSSL=false"
    $env:MES_B01_MYSQL_USER = 'root'
    $env:MES_B01_MYSQL_PASSWORD = $password
    $env:MYSQL_ROOT_PASSWORD = $password
    $env:KTG_TEST_MYSQL_PORT = $port
    $env:KTG_S302_MYSQL_DB = 'ktg_s302_java_test'
    $env:KTG_S304_MYSQL_DB = 'ktg_s304_atomicity_test'
    Push-Location $master
    try {
        $tests = 'ProExecution*Test,ProWorkorderServiceImplTest,ProWorkorderControllerTest,ProductionQueryMapperTest,ProFeedbackQueryControllerTest,InventoryOperationServiceTest,WmTransactionAtomicityTest'
        & mvn -pl ktg-mes -am "-Dtest=$tests" '-Dsurefire.failIfNoSpecifiedTests=false' test *> (Join-Path $run 'backend-regression.log')
        $first = $LASTEXITCODE
        Get-Content -LiteralPath (Join-Path $run 'backend-regression.log') -Tail 22
        # Other inventory tests reset their fixture. Seed the exact receipt in its original
        # service test, then verify the replay in a separately launched Maven/JVM process.
        & mvn -pl ktg-mes -am '-Dtest=InventoryOperationServiceTest#twoJavaExecutorsShareOneMySqlDocumentClaim' '-Dsurefire.failIfNoSpecifiedTests=false' test *> (Join-Path $run 'inventory-restart-prepare.log')
        $seed = $LASTEXITCODE
        Get-Content -LiteralPath (Join-Path $run 'inventory-restart-prepare.log') -Tail 20
        & mvn -pl ktg-mes -am '-Dtest=InventoryOperationRestartReplayTest' '-Dsurefire.failIfNoSpecifiedTests=false' test *> (Join-Path $run 'inventory-restart-replay.log')
        $second = $LASTEXITCODE
        Get-Content -LiteralPath (Join-Path $run 'inventory-restart-replay.log') -Tail 20
        if($first -ne 0 -or $seed -ne 0 -or $second -ne 0){ throw "Regression failed (main=$first, prepare-replay=$seed, replay=$second)" }
    } finally {
        Pop-Location
        foreach ($name in @('MES_B01_MYSQL_URL','MES_B01_MYSQL_USER','MES_B01_MYSQL_PASSWORD','MYSQL_ROOT_PASSWORD','KTG_TEST_MYSQL_PORT','KTG_S302_MYSQL_DB','KTG_S304_MYSQL_DB')) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
    }
} else {
    # Restrict cleanup to our exact new container; never remove any predecessor.
    if(!(Test-Path -LiteralPath $secret)){throw 'No B06 regression secret; refusing cleanup without ownership marker.'}
    docker rm -f $container | Out-Null
    Check-Exit 'Remove only the new regression container'
    Remove-Item -LiteralPath $secret
    Write-Output 'B06 regression container and generated local secret removed.'
}
