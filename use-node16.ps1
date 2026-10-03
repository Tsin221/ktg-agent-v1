$nodeHome = Join-Path $PSScriptRoot '.runtime\node16\node-v16.20.2-win-x64'
$nodeExecutable = Join-Path $nodeHome 'node.exe'

if (-not (Test-Path -LiteralPath $nodeExecutable)) {
    throw "Project Node.js runtime not found: $nodeExecutable"
}

$pathEntries = @($env:Path -split ';' | Where-Object {
    $_ -and $_.TrimEnd('\') -ne $nodeHome.TrimEnd('\')
})
$env:Path = (@($nodeHome) + $pathEntries) -join ';'

Write-Host "Project Node.js environment enabled: $nodeHome"
& node --version
& npm --version
