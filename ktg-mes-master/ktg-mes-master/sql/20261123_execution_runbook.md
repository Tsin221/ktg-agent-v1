# MES-B01: migration and recovery runbook

Scope: explicitly approved isolated MySQL 5.7 only. Do not use the existing `ktg-mes-mysql` container, `ktg_s208_acceptance`, production, or demo databases. Application bootstrap does not automatically execute B01 migrations; menu/role grants are not included.

## Evidence and migration

1. Prepare a fresh isolated MySQL 5.7 container and named database `ktg_mes_b01_*`; restore a consistent read-only baseline export into that fresh database. Confirm that no application or other process writes it during migration. Store exports and evidence under ignored `.runtime` directories, not Git.
2. Run from the workspace root (PowerShell 7):

   ```powershell
   & .\ktg_agent_v1\scripts\apply-mes-b01-migrations.ps1 `
       -ContainerName ktg-mes-b01-mysql `
       -DatabaseName ktg_mes_b01_acceptance `
       -EvidenceDir E:\ktg_agent\ktg_agent_v1\.runtime\mes-b01-20261123\migration-evidence-new
   ```

   The runner requires a new evidence directory, validates the source schema and SN uniqueness/emptiness, checks that any existing execution schema matches the frozen metadata (including all keys and FK columns), saves its own consistent backup and hashes all old columns and table rows. The duplicate-SN probe uses an isolated rolled-back transaction before the SN unique index exists; it tests case/trailing-space conflict and the fail-closed preflight gate. It then applies 01→02→03 twice and compares old data/schema fingerprints and new schema structure.
3. If preflight fails, **do not** apply any DDL. Record the failing check and count. No automated deduplication, empty SN cleanup, index replacement, or history mutation is supported. Obtain a separate reviewed migration decision for any incompatible target. A partial existing execution schema is permitted only if every existing table exactly matches the frozen contract; the two explicitly deferred foreign keys may be missing before stage 02 ends.
4. Run `ProExecutionSchemaMySqlTest` and the operation tests against the approved isolated schema. Set `MES_B01_MYSQL_URL`, `MES_B01_MYSQL_USER`, `MES_B01_MYSQL_PASSWORD` via the approved isolated credentials; never paste secrets in logs. The JDBC tests are disabled without the explicit env and reject any database not named `ktg_mes_b01_*`.
5. To verify again without any SQL writes, repeat the runner with `-VerifyOnly` and a **new** evidence directory. This requires all eight execution tables and both late foreign keys; it verifies the manifest and obtains updated historical rows/schema hashes, but it does not claim to have run a migration.

## Failed DDL and recovery

MySQL 5.7 DDL auto-commits; transactional rollback does not undo a committed `ALTER` or `CREATE`. If the runner stops midstream, freeze writes to the isolated database and preserve the `isolated-before.sql`, `before.json`, preflight and completed stage logs. Do not execute later stages manually. Record the failing file, error, and actual present columns/keys/tables using `information_schema`. If the existing subset matches the frozen manifest, rerun the same runner with a new evidence directory; guarded ALTERs and CREATEs complete the compatible prefix without dropping the prior structures. Verify that the original legacy data/schema fingerprints match the first backup fingerprints, not just the second attempt's pre-state.

If the isolated partial state does not match the contract and contains no real execution history, keep it as evidence and test recovery into a **new empty** isolated database using the preserved preflight backup. Example, using the previously approved isolated container only (choose a database name not already present):

```powershell
$containerName = 'ktg-mes-b01-mysql'
$recoveryDb = 'ktg_mes_b01_recovery_20261123'
$backupPath = 'E:\ktg_agent\ktg_agent_v1\.runtime\mes-b01-20261123\migration-evidence\isolated-before.sql'
Get-FileHash -Algorithm SHA256 -LiteralPath $backupPath   # compare with before.json
# Deliberately no IF NOT EXISTS: abort if the name already exists.
& docker exec $containerName sh -c 'exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "CREATE DATABASE \`$1\` CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci"' sh $recoveryDb
if ($LASTEXITCODE -ne 0) { throw 'Recovery database not created; stop.' }
# Stream bytes without changing backup SQL encoding; its original DROP statements must
# only ever be restored into this explicitly new and empty database.
$info = [Diagnostics.ProcessStartInfo]::new('docker')
$info.UseShellExecute = $false
$info.RedirectStandardInput = $true
foreach ($argument in @('exec','-i',$containerName,'sh','-c','exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$1"','sh',$recoveryDb)) { $info.ArgumentList.Add($argument) }
$restore = [Diagnostics.Process]::Start($info)
$stream = [IO.File]::OpenRead($backupPath)
try { $stream.CopyTo($restore.StandardInput.BaseStream) }
finally { $stream.Dispose(); $restore.StandardInput.Close() }
$restore.WaitForExit()
if ($restore.ExitCode -ne 0) { throw 'Recovery restore failed; preserve both isolated databases.' }
$restore.Dispose()
```

Then rerun the B01 migration runner on `$recoveryDb` with a new evidence directory, and verify original `before.json` row/schema fingerprints and the B01 tests. Do not delete either database during verification. Do not restore an old dump into an existing database, because dump files may contain `DROP TABLE IF EXISTS` statements. Once real B02+ execution facts exist, the preceding empty-isolation recovery example is no longer safe for that live database; use reviewed forward fixes or a consistent backup/PITR recovery preserving business history, after separate approval. Reverting application code does not mean deleting durable execution/receipt records.
