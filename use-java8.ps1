$runtimeDirectory = Join-Path $PSScriptRoot '.runtime\java8'
$jdkHomes = @(Get-ChildItem -LiteralPath $runtimeDirectory -Directory | Where-Object {
    Test-Path -LiteralPath (Join-Path $_.FullName 'bin\java.exe')
})

if ($jdkHomes.Count -ne 1) {
    throw "Expected exactly one JDK under $runtimeDirectory, found $($jdkHomes.Count)."
}

$env:JAVA_HOME = $jdkHomes[0].FullName
$javaBin = Join-Path $env:JAVA_HOME 'bin'
$pathEntries = @($env:Path -split ';' | Where-Object {
    $_ -and $_.TrimEnd('\') -ne $javaBin.TrimEnd('\')
})
$env:Path = (@($javaBin) + $pathEntries) -join ';'

Write-Host "Project Java environment enabled: $env:JAVA_HOME"
& java -version

