$secretsFile = Join-Path $PSScriptRoot '.env'

if (-not (Test-Path -LiteralPath $secretsFile)) {
    throw "Local credential vault not found: $secretsFile"
}

$loadedCount = 0
foreach ($line in Get-Content -LiteralPath $secretsFile) {
    $trimmed = $line.Trim()
    if (-not $trimmed -or $trimmed.StartsWith('#')) {
        continue
    }

    $parts = $trimmed -split '=', 2
    if ($parts.Count -ne 2 -or -not $parts[0].Trim()) {
        throw "Invalid entry in ${secretsFile}: $line"
    }

    [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1], 'Process')
    $loadedCount++
}

Write-Host "Loaded $loadedCount local settings from .env (values hidden)."
