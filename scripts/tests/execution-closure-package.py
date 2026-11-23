"""Build a disposable Spring Boot runtime without overwriting the active admin jar."""
from pathlib import Path
import argparse
import hashlib
import json
import re
import zipfile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", default="", help="Optional isolated runtime filename suffix")
    revision = parser.parse_args().revision
    if revision and not re.fullmatch(r"[a-zA-Z0-9_-]+", revision):
        parser.error("revision must contain only letters, digits, underscores or hyphens")
    suffix = "-" + revision if revision else ""
    project = Path(__file__).resolve().parents[2]
    reactor = project / "ktg-mes-master" / "ktg-mes-master"
    baseline = project / ".runtime" / "mes-b05-20261123" / "ktg-admin-b05-final.jar"
    output_dir = project / ".runtime" / "mes-b06-regression"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"ktg-admin-b06{suffix}.jar"
    replacement_modules = ["ktg-common", "ktg-system", "ktg-framework", "ktg-mes"]
    replacements = {
        f"BOOT-INF/lib/{name}-3.8.2.jar": reactor / name / "target" / f"{name}-3.8.2.jar"
        for name in replacement_modules
    }
    if output.exists():
        raise RuntimeError("B06 package already exists; stop its service and remove it explicitly first")
    with zipfile.ZipFile(baseline) as source:
        if not set(replacements).issubset(source.namelist()):
            raise RuntimeError("Baseline jar is missing reactor dependencies")
        # Spring Boot requires nested jars to remain STORED for random access.
        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_STORED) as target:
            for entry in source.infolist():
                if entry.filename not in replacements:
                    target.writestr(entry, source.read(entry.filename))
            for name, local in replacements.items():
                target.writestr(name, local.read_bytes())
    with zipfile.ZipFile(output) as target, zipfile.ZipFile(baseline) as source:
        hashes = {}
        for name, local in replacements.items():
            data = target.read(name)
            assert data == local.read_bytes(), name
            assert target.getinfo(name).compress_type == zipfile.ZIP_STORED, name
            hashes[name] = hashlib.sha256(data).hexdigest()
        for name in source.namelist():
            if name not in replacements:
                assert target.read(name) == source.read(name), name
        with zipfile.ZipFile(replacements["BOOT-INF/lib/ktg-mes-3.8.2.jar"]) as mes:
            assert "com/ktg/mes/pro/execution/ExecutionTraceController.class" in mes.namelist()
            assert "mapper/pro/execution/ProExecutionTraceMapper.xml" in mes.namelist()
    (output_dir / f"jar-verification{suffix}.json").write_text(json.dumps({
        "baseline": baseline.name,
        "output": output.name,
        "reactorLibrariesMatch": True,
        "baselineOtherEntriesMatch": True,
        "traceControllerAndMapperPresent": True,
        "sha256": hashes,
    }, indent=2), encoding="utf-8")
    print("Built independent B06 application jar and verified all entries")


if __name__ == "__main__":
    main()
