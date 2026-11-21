"""Explicit preparation of showcase SQL views and a NEW SELECT-only account.

Examples (no credentials on command line):
  python prepare-query-views.py --database ktg_demo --permissions-file showcase-permissions.json --role-data-scope 1 --dry-run

Execution requires --execute and KTG_QUERY_ADMIN_PASSWORD / KTG_QUERY_PASSWORD.
The script never edits business rows, roles or menus. MySQL DDL is not atomic;
on failure inspect the redacted plan before retrying with a new runtime account.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ktg-agent" / "src"))

from ktg_agent.exploration.scope import SHOWCASE_ENTITIES, SHOWCASE_PROFILE
from ktg_agent.exploration.sql_views import SQLScopePolicy, available_views, validate_database, view_statements


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--permissions-file", type=Path, required=True)
    parser.add_argument("--role-data-scope", action="append", required=True,
                        help="Current verified role dataScope; repeat for every active role. Only all=1 is supported.")
    parser.add_argument("--runtime-user", default="ktg_query_ro")
    parser.add_argument("--account-host", default="localhost")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3306)
    parser.add_argument("--admin-user", default="root")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--dry-run", action="store_true")
    action.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        validate_database(args.database)
        if (not re.fullmatch(r"[a-z][a-z0-9_]{2,31}", args.runtime_user)
                or args.runtime_user in {"root", "admin", "mysql"}
                or args.runtime_user == args.admin_user):
            raise ValueError("Choose a new dedicated runtime account, never the administrator")
        if not re.fullmatch(r"[A-Za-z0-9_.:%-]{1,255}", args.account_host):
            raise ValueError("Invalid account host")
        value: object = json.loads(args.permissions_file.read_text(encoding="utf-8"))
        if not isinstance(value, list) or not value or not all(isinstance(p, str) and p for p in value):
            raise ValueError("Permissions file must contain a nonempty JSON array of permission strings")
        permissions = frozenset(str(p) for p in value)
        policy = SQLScopePolicy(SHOWCASE_PROFILE, tuple(args.role_data_scope), SHOWCASE_ENTITIES)
        views = available_views(permissions, policy)
        if not views:
            raise ValueError("No authorized views or no explicit current all-row scope policy")
        statements = view_statements(args.database, permissions, policy)
    except (OSError, ValueError) as exc:
        # Validation diagnostics are fixed policy messages, not credentials or DB errors.
        parser.error(str(exc) if isinstance(exc, ValueError) else "Permissions file could not be read")
    plan = {"database": args.database, "profile": SHOWCASE_PROFILE,
            "runtime_account": args.runtime_user + "@" + args.account_host,
            "views": [view.view_name for view in views], "view_ddl": list(statements),
            "privileges": ["SELECT on listed views only"],
            "row_scope": "all rows verified for supplied current roles; not inherited from Java",
            "account_policy": "CREATE NEW USER only; an existing account causes failure; no privilege merging",
            "view_security": "DEFINER is the preparation account; only fixed audited column projections",
            "credentials": "Environment only; never included in this plan"}
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=True, indent=2))
        return 0
    admin_password = os.environ.get("KTG_QUERY_ADMIN_PASSWORD")
    runtime_password = os.environ.get("KTG_QUERY_PASSWORD")
    if not admin_password or not runtime_password or len(runtime_password) < 16:
        parser.error("Execution requires both password environment variables and a runtime password of at least 16 characters")
    connection: Any = None
    try:
        driver = importlib.import_module("pymysql")
        connection = driver.connect(host=args.host, port=args.port, user=args.admin_user,
            password=admin_password, database=args.database, charset="utf8mb4", autocommit=True,
            connect_timeout=5, read_timeout=10, write_timeout=10)
        with connection.cursor() as cursor:
            # No IF NOT EXISTS: never reuse an account whose old grants could be broader.
            cursor.execute("CREATE USER %s@%s IDENTIFIED BY %s",
                           (args.runtime_user, args.account_host, runtime_password))
            for statement, view in zip(statements, views, strict=True):
                cursor.execute(statement)
                cursor.execute(f"GRANT SELECT ON `{args.database}`.`{view.view_name}` TO %s@%s",
                               (args.runtime_user, args.account_host))
    except Exception:
        print("Preparation failed; credentials and raw database errors are suppressed. "
              "DDL may have partially applied; inspect the dry-run plan before retrying.", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()
    print(json.dumps({"prepared": True, "database": args.database, "views": len(views),
                      "runtime_account": plan["runtime_account"]}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
