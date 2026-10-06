$ErrorActionPreference = 'Stop'

$workspaceRoot = Split-Path -Parent $PSScriptRoot

Set-Location $workspaceRoot
. .\use-local-secrets.ps1

if ([string]::IsNullOrWhiteSpace($env:MODEL_API_KEY)) {
    throw 'MODEL_API_KEY is missing from the root .env file.'
}
if ([string]::IsNullOrWhiteSpace($env:MODEL_ID)) {
    throw 'MODEL_ID is missing from the root .env file.'
}

Set-Location .\ktg-agent
uv run --locked python -m ktg_agent.server

