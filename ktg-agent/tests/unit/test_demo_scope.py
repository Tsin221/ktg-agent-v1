"""Public permission subsets from the 2026-11-21 showcase account audit.

The 56 count is employee permission eligibility under audited feature flags,
not evidence of a running employee Agent. Cloud demo's actual catalog was 67.
No account credentials or private runtime files are used by these fixtures.
"""

from collections.abc import Callable
import ast
import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import sys
import types
from typing import Any, cast
from unittest.mock import patch

import pytest

from ktg_agent.ktg.inventory import ISSUE, RECEIPT
from ktg_agent.ktg.client import KTGClient
from ktg_agent.providers.base import ModelConfig
from ktg_agent.providers.fake import FakeProvider
from ktg_agent.service import QueryService
from ktg_agent.tools.catalog import CatalogIdentity


ROOT = Path(__file__).resolve().parents[3]
EXPORT = ROOT / "scripts/demo/export-permissions.py"
PERMISSIONS = ROOT / "scripts/demo/showcase-permissions.json"
# Only the difference affecting the audited tool contracts is needed here.
EMPLOYEE_MISSING = frozenset({
    "mes:dv:checkmachinery:query", "mes:dv:checksubject:query",
    "mes:pro:feedback:list", "mes:pro:feedback:query",
    "mes:pro:protask:list", "mes:pro:protask:query",
    "mes:wm:issueheader:edit", "mes:wm:issueheader:list", "mes:wm:issueheader:query",
    "mes:wm:issueline:list", "mes:wm:itemrecpt:edit", "mes:wm:itemrecpt:list",
    "mes:wm:itemrecpt:query", "mes:wm:itemrecptline:list",
    "mes:wm:wmstock:list", "mes:wm:wmstock:query",
})


def load_export() -> Callable[[Path | None], tuple[str, ...]]:
    # Reintroducing service-driven export must fail before writing audit DBs.
    with patch.object(Path, "mkdir", side_effect=AssertionError("export must not create runtime stores")):
        namespace = runpy.run_path(str(EXPORT), run_name="demo_scope_export_test")
    return cast(Callable[[Path | None], tuple[str, ...]], namespace["load_showcase_permissions"])


def test_fixed_leaf_permissions_and_cli_are_pure_json() -> None:
    permissions = load_export()(None)
    assert len(permissions) == len(set(permissions)) == 70
    assert all(item.startswith("mes:") and len(item.split(":")) == 4 for item in permissions)
    assert not any(item.startswith(("mes:qc:", "mes:cal:", "system:")) for item in permissions)
    # Golden digest of the 70 public leaves in the authenticated cloud snapshot.
    canonical = json.dumps(sorted(permissions), separators=(",", ":")).encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == "21a95ce55337561afc99d1c5eef7ee2b6e2920bf7c58950aa326f8401771de61"
    cli = subprocess.run([sys.executable, str(EXPORT)], capture_output=True, text=True,
                         encoding="utf-8", check=True, timeout=15)
    assert cli.stderr == ""
    assert json.loads(cli.stdout) == list(permissions) == json.loads(PERMISSIONS.read_text(encoding="utf-8"))


def test_new_global_tool_manifest_cannot_expand_permission_export() -> None:
    module = types.ModuleType("ktg_agent.server")
    def forbidden_service(*args: object, **kwargs: object) -> None:
        raise AssertionError("do not assemble any manifest, including new qc/client tools")
    setattr(module, "Settings", forbidden_service)
    setattr(module, "create_service", forbidden_service)
    with patch.dict(sys.modules, {"ktg_agent.server": module}):
        before = load_export()(None)
        setattr(module, "new_global_permissions", ["mes:qc:ipqc:list", "mes:md:client:list"])
        assert load_export()(None) == before
    assert "mes:qc:ipqc:list" not in before and "mes:md:client:list" not in before


def test_existing_sync_supplements_are_already_inside_fixed_profile() -> None:
    # Inspect, never execute sync: it deletes/inserts role menus by design.
    source = ast.parse((ROOT / "scripts/demo/sync-permissions.py").read_text(encoding="utf-8"))
    profile = set(load_export()(None))
    found = False
    for node in ast.walk(source):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "permissions"
                and node.func.attr == "update"):
            values = ast.literal_eval(node.args[0])
            assert isinstance(values, list) and set(values) <= profile
            found = True
    assert found


@pytest.mark.parametrize("payload", [[], ["mes:md:mditem:list"] * 70,
                                     ["system:user:list"] * 70,
                                     {"permissions": ["mes:md:mditem:list"]}])
def test_malformed_fixed_config_fails_closed(tmp_path: Path, payload: object) -> None:
    path = tmp_path / "permissions.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        load_export()(path)


def test_audited_employee_and_demo_tool_eligibility_stays_distinct() -> None:
    # Construct definitions only. Dummy stores are never invoked or persisted.
    stores: dict[str, Any] = {name + "_store": object() for name in (
        "inventory", "workorder", "dv_machinerytype", "dv_machinery", "dv_subject",
        "dv_checkplan", "dv_repair", "tm_tooltype", "tm_tool",
    )}
    service = QueryService(
        provider=FakeProvider(()), model_config=ModelConfig("offline", 32),
        ktg_client_factory=lambda token: KTGClient(base_url="http://mes.invalid", authorization=token),
        inventory_actions=frozenset({ISSUE, RECEIPT}),
        **stores,
    )
    demo = frozenset(load_export()(None))
    employee = demo - EMPLOYEE_MISSING
    assert len(employee) == 54
    cloud_tools = service._catalog.candidate_snapshot(
        CatalogIdentity(demo, "cloud-permission-fixture"), service._switches_provider()).candidates
    employee_tools = service._catalog.candidate_snapshot(
        CatalogIdentity(employee, "employee-permission-fixture"), service._switches_provider()).candidates
    assert len(cloud_tools) == 67 and len(employee_tools) == 56
    assert set(employee_tools) < set(cloud_tools)
    assert "ktg_pro_task_list" not in employee_tools and "ktg_pro_task_list" in cloud_tools
    assert "ktg_query_stock" not in employee_tools and "ktg_query_stock" in cloud_tools
