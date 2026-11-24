param([ValidateSet('backend','agent','gateway','showcase')][string]$Service)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$demoRuntime = Join-Path $projectRoot '.runtime/demo'
Set-Location $projectRoot
. ./use-local-secrets.ps1
$db = Get-Content (Join-Path $demoRuntime 'database.json') -Raw | ConvertFrom-Json
if ($db.database -ne 'ktg_demo') { throw 'Unexpected demo database' }
if ($Service -eq 'backend') {
    . ./use-java8.ps1
    $env:MYSQL_DATABASE = 'ktg_demo'
    $env:MYSQL_APP_USER = $db.username
    $env:MYSQL_APP_PASSWORD = $db.password
    $env:MYSQL_HOST = '127.0.0.1'
    $env:MYSQL_PORT = '3306'
    $env:TOKEN_SECRET = Get-Content (Join-Path $demoRuntime 'token-secret.txt') -Raw
    & java -jar (Join-Path $projectRoot 'ktg-mes-master/ktg-mes-master/ktg-admin/target/ktg-admin.jar') --server.address=127.0.0.1 --server.port=8082 --spring.redis.database=12 --token.expireTime=480 --spring.datasource.druid.statViewServlet.enabled=false
} elseif ($Service -eq 'agent') {
    $env:KTG_BASE_URL = 'http://127.0.0.1:8082'
    $env:AGENT_HOST = '127.0.0.1'
    $env:AGENT_PORT = '8002'
    $env:AGENT_SCOPE_BY_TOKEN = 'true'
    $env:AGENT_SESSION_DB = Join-Path $demoRuntime 'readonly-sessions.sqlite3'
    $env:AGENT_SPILL_DIR = Join-Path $demoRuntime 'spill'
    Set-Location (Join-Path $projectRoot 'ktg-agent')
    & uv run --locked python -m ktg_agent.server
} elseif ($Service -eq 'gateway') {
    & node (Join-Path $PSScriptRoot 'gateway.mjs')
} else {
    & node (Join-Path $projectRoot '../ktg-showcase/scripts/serve.mjs')
}
if ($LASTEXITCODE -ne 0) { throw "$Service exited with code $LASTEXITCODE" }
