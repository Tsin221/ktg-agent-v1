function Test-MesB01ExecutionStructure([string]$Structure,[string]$SchemaContract,[switch]$RequireFull) {
    # A failed migration may legitimately have created only a prefix of the tables.
    # An existing table must still match its complete DDL exactly; only the two late
    # circular FKs may be pending. Never let CREATE IF NOT EXISTS hide schema drift.
    $expected = @(Get-Content -Encoding utf8 -LiteralPath $SchemaContract | Where-Object { $_ })
    if ($expected.Count -eq 0) { throw 'Missing B01 schema contract metadata.' }
    $actual = @(($Structure.Replace("`t",'|') -split '\r?\n') | Where-Object { $_ })
    foreach ($row in $actual) {
        if ($expected -cnotcontains $row) { throw "Unexpected B01 schema metadata: $row" }
    }
    $presentTables = @($actual | ForEach-Object { ($_ -split '\|',2)[0] } | Select-Object -Unique)
    foreach ($row in $expected) {
        $table = ($row -split '\|',2)[0]
        if (-not $RequireFull -and ($presentTables -notcontains $table -or
            $row -match '\|fk_execution_(task|inspection)_disposition\|')) { continue }
        if ($actual -cnotcontains $row) { throw "Missing required B01 schema metadata: $row" }
    }
}
