$ErrorActionPreference = 'Stop'

$workspaceRoot = Split-Path -Parent $PSScriptRoot

Set-Location $workspaceRoot
. .\use-local-secrets.ps1

if ([string]::IsNullOrWhiteSpace($env:MODEL_API_KEY) -or $env:MODEL_API_KEY -eq 'CHANGE_ME') {
    throw 'MODEL_API_KEY is missing from the root .env file.'
}
if ([string]::IsNullOrWhiteSpace($env:MODEL_ID) -or $env:MODEL_ID -eq 'CHANGE_ME') {
    throw 'MODEL_ID is missing from the root .env file.'
}

Set-Location .\ktg-agent
uv run --locked python -m ktg_agent.server

