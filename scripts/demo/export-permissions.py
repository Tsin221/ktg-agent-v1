"""Extract permissions from assembled tools, including generated declarations."""
import json
from dataclasses import fields
from pathlib import Path
from ktg_agent.server import Settings, create_service
from ktg_agent.tools.permissions import PermissionLeaf, PermissionAll, PermissionAny

runtime=Path(__file__).resolve().parents[2]/'.runtime/demo/permission-audit'
runtime.mkdir(parents=True,exist_ok=True)
options={f.name:str(runtime/(f.name+'.sqlite3')) for f in fields(Settings) if f.name.endswith('_db_path')}
options.update({f.name:True for f in fields(Settings) if f.name.startswith('enable_') and f.name!='enable_workorder_progress'})
service=create_service(Settings(model_api_key='offline-audit',model_base_url='http://127.0.0.1:1',model_id='offline',ktg_base_url='http://127.0.0.1:1',**options))
permissions=set()
def visit(policy):
    if isinstance(policy,PermissionLeaf):
        if policy.permission.startswith('mes:'): permissions.add(policy.permission)
    elif isinstance(policy,PermissionAll):
        for child in policy.all_of: visit(child)
    elif isinstance(policy,PermissionAny):
        for child in policy.any_of: visit(child)
for manifest in service._catalog._manifests.values(): visit(manifest.permission_policy)
print(json.dumps(sorted(permissions)))
