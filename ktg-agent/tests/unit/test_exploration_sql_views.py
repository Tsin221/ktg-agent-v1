from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ktg_agent.exploration.catalog import ENTITIES
from ktg_agent.exploration.scope import SHOWCASE_ENTITIES, SHOWCASE_PROFILE
from ktg_agent.exploration.sql_views import SQLScopePolicy, available_views, view_statements


def policy(scopes: tuple[str, ...] = ("1",)) -> SQLScopePolicy:
    return SQLScopePolicy(SHOWCASE_PROFILE, scopes, SHOWCASE_ENTITIES)


def test_no_sql_views_without_an_explicit_current_unrestricted_row_policy() -> None:
    permissions = frozenset({"*:*:*"})
    assert available_views(permissions, None) == ()
    for scopes in ((), ("2",), ("1", "4")):
        assert available_views(permissions, policy(scopes)) == ()
    assert available_views(permissions, SQLScopePolicy("other_profile", ("1",), SHOWCASE_ENTITIES)) == ()


def test_view_visibility_intersects_scope_and_complete_operation_permissions() -> None:
    views = available_views(frozenset({"mes:md:mditem:list"}), policy())
    assert len(views) == 1 and views[0].view_name == "agent_md_item"
    assert views[0].entity == "md.item" and {"id", "code", "name"} <= set(views[0].columns)
    assert not ({"create_by", "password", "token", "remark"} & set(views[0].columns))
    narrowed = SQLScopePolicy(SHOWCASE_PROFILE, ("1",), frozenset({"dv.machinery"}))
    assert available_views(frozenset({"mes:md:mditem:list"}), narrowed) == ()
    assert available_views(frozenset({"mes:dv:repairline:list"}), policy()) == ()
    line = available_views(frozenset({"mes:dv:repair:query", "mes:dv:repairline:list"}), policy())
    assert [view.entity for view in line] == ["dv.repair_line"]
    assert set(line[0].requirements) == {"mes:dv:repair:query", "mes:dv:repairline:list"}


def test_only_the_twenty_two_reviewed_list_projections_are_available() -> None:
    views = available_views(frozenset({"*:*:*"}), policy())
    assert len(views) == 22 and {view.entity for view in views} == SHOWCASE_ENTITIES
    for view in views:
        assert view.requirements == ENTITIES[view.entity].permissions["list"]
        assert set(view.columns) == set(ENTITIES[view.entity].fields)
    incomplete = frozenset({"mes:dv:checkmachinery:list", "mes:dv:checkplan:query"})
    assert "dv.check_machinery" not in {view.entity for view in available_views(incomplete, policy())}


def test_ddl_uses_audited_physical_columns_not_camelcase_guessing() -> None:
    ddl = view_statements("ktg_query_test_views", frozenset({"*:*:*"}), policy())
    assert len(ddl) == 22
    machinery = next(statement for statement in ddl if "`agent_dv_machinery`" in statement)
    assert "`machinery_spec` AS `specification`" in machinery
    assert "`machinery_id` AS `id`" in machinery
    bom = next(statement for statement in ddl if "`agent_md_item_bom`" in statement)
    assert "FROM `ktg_query_test_views`.`md_product_bom`" in bom
    assert "SQL SECURITY DEFINER" in machinery
    assert not any(word in " ".join(ddl).upper() for word in ("SYS_ROLE", "SYS_MENU", "PASSWORD", "UPDATE ", "DELETE "))


def test_ddl_rejects_unknown_databases_and_missing_policy() -> None:
    for database in ("mysql", "production", "ktg_demo; DROP TABLE x", "ktg_query_test_"):
        with pytest.raises(ValueError):
            view_statements(database, frozenset({"*:*:*"}), policy())
    assert view_statements("ktg_demo", frozenset({"*:*:*"}), None) == ()


def test_prepare_dry_run_is_explicit_and_does_not_print_environment_passwords(tmp_path: Path) -> None:
    permissions = tmp_path / "permissions.json"
    permissions.write_text(json.dumps(["mes:md:mditem:list"]), encoding="utf-8")
    script = Path(__file__).resolve().parents[3] / "scripts/demo/prepare-query-views.py"
    environment = {**os.environ, "KTG_QUERY_PASSWORD": "secret-test-not-logged", "KTG_QUERY_ADMIN_PASSWORD": "admin-test-not-logged"}
    completed = subprocess.run([sys.executable, str(script), "--database", "ktg_query_test_dryrun",
        "--permissions-file", str(permissions), "--role-data-scope", "1", "--dry-run"],
        capture_output=True, text=True, timeout=15, env=environment)
    assert completed.returncode == 0, completed.stderr
    plan = json.loads(completed.stdout)
    assert plan["database"] == "ktg_query_test_dryrun"
    assert plan["views"] == ["agent_md_item"]
    assert plan["runtime_account"] == "ktg_query_ro@localhost"
    assert plan["privileges"] == ["SELECT on listed views only"]
    assert "secret-test-not-logged" not in completed.stdout + completed.stderr
    assert "admin-test-not-logged" not in completed.stdout + completed.stderr
