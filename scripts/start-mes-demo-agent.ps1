param(
    [ValidateSet('Agent', 'SwitchUi', 'Status')]
    [string]$Action = 'Agent',
    [switch]$Restart,
    [string]$UiBuildPath = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$demoRoot = Join-Path $projectRoot '.runtime/mes-demo'
$agentRoot = Join-Path $demoRoot 'agent'
$serviceFile = Join-Path $demoRoot 'service-pids.json'
$agentFile = Join-Path $agentRoot 'process.json'
$uiFile = Join-Path $agentRoot 'ui-process.json'
$oldUiScript = Join-Path $demoRoot 'ui-server.cjs'
$gatewayScript = Join-Path $PSScriptRoot 'mes-demo-ui-gateway.cjs'
$agentPort = 18091
$backendPort = 18090
$uiPort = 18854

function ListenerPid([int]$LocalPort) {
    $listeners = @(Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue)
    if ($listeners.Count -eq 0) { return 0 }
    if ($listeners.Count -ne 1 -or $listeners[0].LocalAddress -ne '127.0.0.1') {
        throw "Unexpected listener on port $LocalPort; no process was changed."
    }
    return [int]$listeners[0].OwningProcess
}

function Read-State([string]$StatePath) {
    if (!(Test-Path -LiteralPath $StatePath)) { throw "Required runtime state not found: $StatePath" }
    return Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
}

function ProcessStamp($Process) {
    return [string]$Process.StartTime.ToUniversalTime().Ticks
}

function HttpStatus([string]$Uri) {
    try {
        return [int](Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec 3).StatusCode
    } catch {
        if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode }
        return 0
    }
}

function Wait-Http([string]$Uri, [int]$ExpectedStatus) {
    $deadline = (Get-Date).AddSeconds(30)
    do {
        if ((HttpStatus $Uri) -eq $ExpectedStatus) { return $true }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Write-State([string]$StatePath, $StateValue) {
    $StateValue | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $StatePath -Encoding utf8
}

function Assert-RecordedProcess($State, [int]$ExpectedPid, [string]$Name) {
    $process = Get-Process -Id $ExpectedPid -ErrorAction Stop
    if ($process.ProcessName -ne $Name -or [int]$State.pid -ne $ExpectedPid -or
        [string]$State.startTicks -ne (ProcessStamp $process)) {
        throw "Runtime process identity did not match for PID $ExpectedPid."
    }
    return $process
}

function Get-LaunchedListener($Launcher, [int]$LocalPort, [string]$Name) {
    $listenerId = ListenerPid $LocalPort
    if (!$listenerId) { return $null }
    $listener = Get-Process -Id $listenerId -ErrorAction Stop
    if ($listener.ProcessName -ne $Name) { return $null }
    if ($listener.Id -eq $Launcher.Id) { return $listener }
    # The Windows Python venv launcher may start the actual Python child process.
    $child = Get-CimInstance Win32_Process -Filter "ProcessId = $listenerId"
    if ($child.ParentProcessId -eq $Launcher.Id -and $listener.StartTime -ge $Launcher.StartTime) {
        return $listener
    }
    return $null
}

function Start-Ui([string]$ScriptPath, [string]$BuildPath, [string]$LogTag) {
    $launchArgs = @('"' + $ScriptPath + '"')
    if ($BuildPath) { $launchArgs += '"' + $BuildPath + '"' }
    $stamp = (Get-Date).ToString('yyyyMMdd-HHmmss-fff')
    $options = @{
        FilePath = $nodeExe; ArgumentList = $launchArgs; WorkingDirectory = $demoRoot
        RedirectStandardOutput = Join-Path $agentRoot "$LogTag-$stamp.out.log"
        RedirectStandardError = Join-Path $agentRoot "$LogTag-$stamp.err.log"
        WindowStyle = 'Hidden'; PassThru = $true
    }
    return Start-Process @options
}

$demoState = Read-State $serviceFile
if ($demoState.database -ne 'ktg_mes_demo' -or [int]$demoState.backendPort -ne $backendPort -or
    [int]$demoState.uiPort -ne $uiPort -or (ListenerPid $backendPort) -ne [int]$demoState.backendPid -or
    (Get-Process -Id ([int]$demoState.backendPid)).ProcessName -ne 'java') {
    throw 'The formal MES demo backend identity did not match; no process was changed.'
}
if ((HttpStatus "http://127.0.0.1:$backendPort/getInfo") -notin @(200, 401, 403)) {
    throw 'Formal MES demo backend is unavailable.'
}

if ($Action -eq 'Status') {
    [pscustomobject]@{
        database = $demoState.database
        backendPid = ListenerPid $backendPort
        uiPid = ListenerPid $uiPort
        agentPid = ListenerPid $agentPort
        agentHealth = HttpStatus "http://127.0.0.1:$agentPort/health"
        uiStatus = HttpStatus "http://127.0.0.1:$uiPort/"
    }
    return
}

New-Item -ItemType Directory -Path $agentRoot -Force | Out-Null

if ($Action -eq 'Agent') {
    $currentPid = ListenerPid $agentPort
    if ($currentPid) {
        $record = Read-State $agentFile
        $existing = Assert-RecordedProcess $record $currentPid 'python'
        if (!$Restart) {
            if ((HttpStatus "http://127.0.0.1:$agentPort/health") -ne 200) {
                throw 'Recorded Agent is listening but unhealthy. Inspect its isolated logs.'
            }
            Write-Output "Formal MES demo Agent already healthy: 18091 (PID $currentPid)."
            return
        }
        Stop-Process -Id $existing.Id
        $existing.WaitForExit(5000) | Out-Null
        if (ListenerPid $agentPort) { throw 'Agent port did not become available.' }
    }

    # Load only model and resource-limit settings, never unrelated database,
    # token, or historical Agent settings. Never print configured values.
    $allowed = @(
        'MODEL_API_KEY','MODEL_BASE_URL','MODEL_ID',
        'AGENT_MAX_OUTPUT_TOKENS','AGENT_MAX_TOTAL_TOKENS','AGENT_MAX_TOOL_CALLS',
        'AGENT_MAX_MODEL_TURNS','AGENT_CONTEXT_WINDOW','AGENT_CONTEXT_RESERVE_TOKENS',
        'AGENT_CONTEXT_THRESHOLD_RATIO','AGENT_CONTEXT_RETAIN_RATIO',
        'AGENT_CONTEXT_SUMMARY_MAX_TOKENS','AGENT_SPILL_MAX_INLINE_TOKENS',
        'AGENT_MAX_ACTIVE_TOOLS','AGENT_MAX_TOOL_SCHEMA_TOKENS'
    )
    $envFile = Join-Path $projectRoot '.env'
    if (!(Test-Path -LiteralPath $envFile)) { throw 'Local model configuration file was not found.' }
    foreach ($line in Get-Content -LiteralPath $envFile) {
        if ($line.TrimStart().StartsWith('#') -or !$line.Contains('=')) { continue }
        $parts = $line -split '=', 2
        $name = $parts[0].Trim()
        if ($name -in $allowed) {
            [Environment]::SetEnvironmentVariable($name, $parts[1].Trim(), 'Process')
        }
    }
    foreach ($name in @('MODEL_API_KEY', 'MODEL_ID', 'MODEL_BASE_URL')) {
        $value = [Environment]::GetEnvironmentVariable($name, 'Process')
        if ([string]::IsNullOrWhiteSpace($value) -or $value -eq 'CHANGE_ME') {
            throw "$name is not configured."
        }
    }
    $env:KTG_BASE_URL = "http://127.0.0.1:$backendPort"
    $env:AGENT_HOST = '127.0.0.1'
    $env:AGENT_PORT = [string]$agentPort
    $env:AGENT_SCOPE_BY_TOKEN = 'false'
    $env:AGENT_SESSION_DB = Join-Path $agentRoot 'sessions.sqlite3'
    $env:AGENT_SPILL_DIR = Join-Path $agentRoot 'spill'
    $pythonExe = Join-Path $projectRoot 'ktg-agent/.venv/Scripts/python.exe'
    if (!(Test-Path -LiteralPath $pythonExe)) { throw 'The local Agent Python environment is missing.' }
    $stamp = (Get-Date).ToString('yyyyMMdd-HHmmss-fff')
    $outPath = Join-Path $agentRoot "agent-$stamp.out.log"
    $errPath = Join-Path $agentRoot "agent-$stamp.err.log"
    $options = @{
        FilePath = $pythonExe; ArgumentList = @('-B','-m','ktg_agent.server')
        WorkingDirectory = Join-Path $projectRoot 'ktg-agent'
        RedirectStandardOutput = $outPath; RedirectStandardError = $errPath
        WindowStyle = 'Hidden'; PassThru = $true
    }
    $launcher = Start-Process @options
    $healthy = Wait-Http "http://127.0.0.1:$agentPort/health" 200
    $process = Get-LaunchedListener $launcher $agentPort 'python'
    if (!$healthy -or !$process) {
        if ($process -and !$process.HasExited) { Stop-Process -Id $process.Id }
        if (!$launcher.HasExited) { Stop-Process -Id $launcher.Id }
        throw 'Dedicated Agent did not become healthy. See its isolated runtime logs.'
    }
    Write-State $agentFile @{
        pid = $process.Id; startTicks = ProcessStamp $process; port = $agentPort
        database = 'ktg_mes_demo'; backendPort = $backendPort
        stateDb = $env:AGENT_SESSION_DB; outLog = $outPath; errorLog = $errPath
    }
    Write-Output "Formal MES demo Agent healthy: 18091 (PID $($process.Id))."
    return
}

if (!$UiBuildPath) { throw 'SwitchUi requires an explicit independent UI build path.' }
$buildPath = (Resolve-Path -LiteralPath $UiBuildPath).Path
$resolvedAgentRoot = (Resolve-Path -LiteralPath $agentRoot).Path
if (!$buildPath.StartsWith($resolvedAgentRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or
    !(Split-Path -Leaf $buildPath).StartsWith('ui-build-') -or
    !(Test-Path -LiteralPath (Join-Path $buildPath 'index.html'))) {
    throw 'UI build must be a verified independent ui-build-* directory inside the dedicated Agent runtime.'
}
if ((HttpStatus "http://127.0.0.1:$agentPort/health") -ne 200) { throw 'Agent is not healthy.' }
$agentProcess = Assert-RecordedProcess (Read-State $agentFile) (ListenerPid $agentPort) 'python'
$nodeExe = Join-Path $projectRoot '.runtime/node16/node-v16.20.2-win-x64/node.exe'
if (!(Test-Path -LiteralPath $nodeExe) -or !(Test-Path -LiteralPath $oldUiScript) -or
    !(Test-Path -LiteralPath $gatewayScript)) { throw 'A required UI runtime file is missing.' }
& $nodeExe --check $gatewayScript
if ($LASTEXITCODE -ne 0) { throw 'UI gateway validation failed.' }

$oldPid = ListenerPid $uiPort
if (!$oldPid -or $oldPid -ne [int]$demoState.uiPid) { throw 'Current UI listener does not match the demo PID.' }
$oldProcess = Get-Process -Id $oldPid -ErrorAction Stop
if ($oldProcess.ProcessName -ne 'node') { throw 'Current UI process is not the expected Node process.' }
$oldCommand = (Get-CimInstance Win32_Process -Filter "ProcessId = $oldPid").CommandLine
$rollbackScript = $oldUiScript
$rollbackBuild = ''
if (Test-Path -LiteralPath $uiFile) {
    $priorUi = Read-State $uiFile
    $oldProcess = Assert-RecordedProcess $priorUi $oldPid 'node'
    $rollbackScript = $gatewayScript
    $rollbackBuild = [string]$priorUi.buildPath
}
if (!$oldCommand -or !$oldCommand.Contains($rollbackScript)) {
    throw 'Current UI command does not match the expected dedicated gateway.'
}
Stop-Process -Id $oldProcess.Id
$oldProcess.WaitForExit(5000) | Out-Null
if (ListenerPid $uiPort) { throw 'Dedicated UI port did not become available.' }
$newProcess = $null
try {
    $newLauncher = Start-Ui $gatewayScript $buildPath 'ui'
    $healthy = Wait-Http "http://127.0.0.1:$uiPort/" 200
    $newProcess = Get-LaunchedListener $newLauncher $uiPort 'node'
    if (!$healthy -or !$newProcess -or
        (HttpStatus "http://127.0.0.1:$uiPort/agent-api/v1/tools") -ne 401 -or
        (HttpStatus "http://127.0.0.1:$uiPort/prod-api/getInfo") -notin @(200,401,403)) {
        throw 'New UI gateway failed its local proxy checks.'
    }
    $demoState.uiPid = $newProcess.Id
    Write-State $serviceFile $demoState
    Write-State $uiFile @{pid=$newProcess.Id; startTicks=ProcessStamp $newProcess; buildPath=$buildPath}
    Write-Output "Formal MES demo UI switched: 18854 (PID $($newProcess.Id)); agent PID $($agentProcess.Id), backend PID $($demoState.backendPid) unchanged."
} catch {
    if ($newProcess -and !$newProcess.HasExited) { Stop-Process -Id $newProcess.Id }
    if ($newLauncher -and !$newLauncher.HasExited) { Stop-Process -Id $newLauncher.Id }
    if (ListenerPid $uiPort) { throw 'UI gateway failed and the port is unexpectedly occupied. No other process was stopped.' }
    $restored = Start-Ui $rollbackScript $rollbackBuild 'ui-rollback'
    if (!(Wait-Http "http://127.0.0.1:$uiPort/" 200) -or (ListenerPid $uiPort) -ne $restored.Id) {
        throw 'New UI gateway failed and the previous gateway could not be restored.'
    }
    $demoState.uiPid = $restored.Id
    Write-State $serviceFile $demoState
    if ($rollbackBuild) {
        Write-State $uiFile @{pid=$restored.Id; startTicks=ProcessStamp $restored; buildPath=$rollbackBuild}
    }
    throw 'New gateway failed; the previous UI has been restored.'
}
