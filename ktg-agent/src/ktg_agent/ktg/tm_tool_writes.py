"""Durable previews and trusted confirmation for tool type CRUD."""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Mapping
from typing import Any
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import Lock
from time import time
from typing import cast
from uuid import uuid4

from ktg_agent.core.messages import JsonObject, JsonValue
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.ktg.tm_tool import TOOL_LIST, TOOL_PATH, fetch_tool, project_tool, tool_page
from ktg_agent.ktg.workorders import PreviewError
from ktg_agent.tools.catalog import ToolFactory, ToolManifest
from ktg_agent.tools.permissions import PermissionAll, PermissionLeaf, PermissionPolicy
from ktg_agent.tools.registry import (
    ExecutionMode, RetryPolicy, ToolChannel, ToolContext, ToolDefinition, ToolEffect,
    ToolExecute, ToolResult, ToolStatus,
)


PREVIEW_CREATE="ktg_preview_tm_tool_create"; PREVIEW_UPDATE="ktg_preview_tm_tool_update"; PREVIEW_DELETE="ktg_preview_tm_tool_delete"
APPLY_CREATE="ktg_tm_tool_create"; APPLY_UPDATE="ktg_tm_tool_update"; APPLY_DELETE="ktg_tm_tool_delete"
MUTABLE=("toolName","brand","spec","maintenType","nextMaintenDate","nextMaintenPeriod","remark")
SNAPSHOT_FIELDS=("toolId","toolCode","toolName","brand","spec","toolTypeId","toolTypeCode","toolTypeName","codeFlag","quantity","quantityAvail","maintenType","nextMaintenPeriod","nextMaintenDate","status","remark")
CREATE_FIELDS=("toolCode","toolName","brand","spec","toolTypeId","toolTypeCode","toolTypeName","codeFlag","quantity","quantityAvail","maintenType","nextMaintenPeriod","nextMaintenDate","status","remark")
_ARG_TO_WIRE={"tool_name":"toolName","brand":"brand","spec":"spec","mainten_type":"maintenType","next_mainten_date":"nextMaintenDate","next_mainten_period":"nextMaintenPeriod","remark":"remark"}
_WIRE_TO_ARG={v:k for k,v in _ARG_TO_WIRE.items()}
_ACTIONS={"create":(PREVIEW_CREATE,APPLY_CREATE,"mes:tm:tool:add"),"update":(PREVIEW_UPDATE,APPLY_UPDATE,"mes:tm:tool:edit"),"delete":(PREVIEW_DELETE,APPLY_DELETE,"mes:tm:tool:remove")}
_UPDATE_SCHEMA_FIELDS: dict[str, JsonValue] = {"tool_name":{"type":"string","minLength":1,"maxLength":100},"brand":{"type":["string","null"],"maxLength":100},"spec":{"type":["string","null"],"maxLength":100},"mainten_type":{"type":"string","enum":["REGULAR","USAGE"]},"next_mainten_date":{"type":["string","null"]},"next_mainten_period":{"type":["integer","null"],"minimum":1,"maximum":2147483647},"remark":{"type":["string","null"],"maxLength":250}}
@dataclass(slots=True)
class _Preview:
    preview_id: str
    user_key: str
    action: str
    before: JsonObject | None
    after: JsonObject
    digest: str
    expires_at: float
    state: str
    result: ToolResult | None


class TmToolPreviewStore:
    """User-bound previews and one-shot results kept in a dedicated SQLite database."""

    def __init__(self, *, ttl_seconds: float = 600, path: str | Path = ":memory:") -> None:
        if ttl_seconds <= 0:
            raise ValueError("preview ttl must be positive")
        self._ttl_seconds, self.path = ttl_seconds, str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._memory_db = sqlite3.connect(":memory:", timeout=10, check_same_thread=False) if self.path == ":memory:" else None
        if self._memory_db is not None:
            self._memory_db.row_factory = sqlite3.Row
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS tm_tool_previews (
                preview_id TEXT PRIMARY KEY, user_key TEXT NOT NULL, action TEXT NOT NULL,
                before_json TEXT, after_json TEXT NOT NULL, digest TEXT NOT NULL,
                expires_at REAL NOT NULL, state TEXT NOT NULL, result_json TEXT
            )""")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = self._memory_db or sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            if self._memory_db is None:
                db.close()

    @staticmethod
    def _encode(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    @staticmethod
    def _digest(action: str, before: JsonObject | None, after: JsonObject) -> str:
        encoded = json.dumps({"action": action, "before": before, "after": after}, ensure_ascii=False,
                             sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def create(self, *, user_key: str, action: str, before: JsonObject | None, after: JsonObject) -> str:
        preview_id, digest = str(uuid4()), self._digest(action, before, after)
        with self._lock, self._connect() as db:
            now = time()
            db.execute("DELETE FROM tm_tool_previews WHERE expires_at<=? AND state='pending'", (now,))
            db.execute("""INSERT INTO tm_tool_previews
                (preview_id,user_key,action,before_json,after_json,digest,expires_at,state,result_json)
                VALUES (?,?,?,?,?,?,?,'pending',NULL)""",
                (preview_id, user_key, action, self._encode(before) if before is not None else None,
                 self._encode(after), digest, now + self._ttl_seconds))
        return preview_id

    def action_for(self, *, preview_id: str, user_key: str) -> str:
        record = self._read(preview_id)
        if record is None:
            raise PreviewError("PREVIEW_NOT_FOUND", "预览不存在或已过期，请重新生成。")
        if record.user_key != user_key:
            raise PreviewError("PREVIEW_IDENTITY_MISMATCH", "该预览不属于当前登录用户。")
        if record.state != "completed" and record.expires_at <= time():
            raise PreviewError("PREVIEW_NOT_FOUND", "预览已过期，请重新生成。")
        return record.action

    def begin(self, *, preview_id: str, user_key: str, action: str) -> tuple[_Preview, ToolResult | None]:
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tm_tool_previews WHERE preview_id=?", (preview_id,)).fetchone()
            if row is None:
                raise PreviewError("PREVIEW_NOT_FOUND", "预览不存在或已过期，请重新生成。")
            record = self._record(row)
            if record.user_key != user_key:
                raise PreviewError("PREVIEW_IDENTITY_MISMATCH", "该预览不属于当前登录用户。")
            if record.action != action:
                raise PreviewError("PREVIEW_ACTION_MISMATCH", "预览操作类型不匹配。")
            if record.state == "completed":
                return record, record.result
            if record.state == "executing":
                raise PreviewError("PREVIEW_IN_PROGRESS", "该操作正在执行，请勿重复提交。")
            if record.expires_at <= time():
                db.execute("DELETE FROM tm_tool_previews WHERE preview_id=?", (preview_id,))
                raise PreviewError("PREVIEW_NOT_FOUND", "预览已过期，请重新生成。")
            db.execute("UPDATE tm_tool_previews SET state='executing' WHERE preview_id=? AND state='pending'", (preview_id,))
            record.state = "executing"
            return record, None

    def finish(self, preview_id: str, result: ToolResult) -> None:
        encoded = self._encode({"content": result.content, "details": result.details,
            "status": result.status.value, "error_code": result.error_code,
            "operation_id": result.operation_id, "verification": result.verification})
        with self._lock, self._connect() as db:
            db.execute("UPDATE tm_tool_previews SET state='completed',result_json=? WHERE preview_id=?", (encoded, preview_id))

    def _read(self, preview_id: str) -> _Preview | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM tm_tool_previews WHERE preview_id=?", (preview_id,)).fetchone()
            return None if row is None else self._record(row)

    def _record(self, row: sqlite3.Row) -> _Preview:
        before = cast(JsonObject | None, json.loads(row["before_json"])) if row["before_json"] else None
        after = cast(JsonObject, json.loads(row["after_json"]))
        if self._digest(row["action"], before, after) != row["digest"]:
            raise PreviewError("PREVIEW_STALE", "预览内容已变化，请重新生成。")
        result = None
        if row["result_json"]:
            value = json.loads(row["result_json"])
            result = ToolResult(content=value["content"], details=value["details"], status=ToolStatus(value["status"]),
                error_code=value["error_code"], operation_id=value["operation_id"], verification=value["verification"])
        return _Preview(row["preview_id"], row["user_key"], row["action"], before, after,
                        row["digest"], row["expires_at"], row["state"], result)



def build_tm_tool_write_tools(store: TmToolPreviewStore) -> tuple[ToolDefinition,...]:
 return tuple(_definition(store,a,h) for a in ("create","update","delete") for h in (False,True))
def tm_tool_write_factories(definitions: tuple[ToolDefinition, ...]) -> dict[str, ToolFactory]:
  def factory(item: ToolDefinition) -> ToolFactory:
   return lambda: item
  return {f"tm_tool_writes:{item.name}": factory(item) for item in definitions}
def tm_tool_write_manifests(definitions: tuple[ToolDefinition, ...]) -> tuple[ToolManifest, ...]:
 policies={a:PermissionAll(tuple(PermissionLeaf(p) for p in _permissions(a))) for a in _ACTIONS}
 return tuple(ToolManifest(name=x.name,version=x.version,module="tm",domain="tool",label=x.label,description=x.description,permission_policy=policies[_action(x.name)],effect=x.effect,execution_mode=x.execution_mode,retry_policy=x.retry_policy,timeout_seconds=x.timeout_seconds,expose_to_model=x.expose_to_model,confirmation_policy="none" if x.expose_to_model else "trusted_confirmation",proposal_store_effect=x.name.startswith("ktg_preview_"),handler_ref=f"tm_tool_writes:{x.name}") for x in definitions)
def _action(name: str) -> str:
 for a,(p,e,_) in _ACTIONS.items():
  if name in (p,e):return a
 raise ValueError("unknown tool action")
def _definition(store: TmToolPreviewStore, action: str, hidden: bool) -> ToolDefinition:
 preview,apply,_=_ACTIONS[action]; name=apply if hidden else preview
 def run(context: ToolContext, arguments: Mapping[str, Any]) -> Awaitable[ToolResult]:
  return _apply(store,action,context,arguments) if hidden else _preview(store,action,context,arguments)
 props: dict[str, JsonValue]={"tool_id":{"type":"integer","minimum":1}} if action!="create" else {}
 if action!="delete":
  if action=="create":
   props.update({"tool_code":{"type":"string","minLength":1,"maxLength":64},"tool_name":{"type":"string","minLength":1,"maxLength":100},"tool_type_id":{"type":"integer","minimum":1},"quantity":{"type":"integer","minimum":0,"maximum":99999999}})
   props.update({k:v for k,v in _UPDATE_SCHEMA_FIELDS.items() if k not in {"tool_name"}})
  else: props.update(_UPDATE_SCHEMA_FIELDS)
 if hidden: props={"operation_id":{"type":"string","minLength":1,"maxLength":100}}
 schema: JsonObject={"type":"object","properties":props,"required":["operation_id"] if hidden else (["tool_code","tool_name","tool_type_id","quantity"] if action=="create" else ["tool_id"]),"additionalProperties":False}
 guidance={
  "create":"新增必传tool_code、tool_name、真实tool_type_id、quantity；可从本会话已有选项匹配类型名称，不把名称当ID。缺失、变化或明确重查才读取选项。codeFlag=Y数量必须1，冲突时解释并追问，不擅自改变用户数量。",
  "update":"仅支持名称、品牌、型号、维护方式、下次保养日期/次数周期和备注；不支持状态、数量、可用数量、类型或编码修改，拒绝这些请求无需查询基础选项。只传tool_id及用户要求改变的字段，工具会读取并保留其余字段，无需重复列表或选项查询。",
  "delete":"按准确tool_id生成删除预览，工具会实时核对记录及类型条件，无需先查询基础选项。"
 }[action]
 return ToolDefinition(name=name,version="1.0",label=f"{'确认' if hidden else '预览'}工装夹具{_label(action)}",description=(f"可信确认专用且对模型隐藏的{_label(action)}执行。" if hidden else f"生成单条工装夹具{_label(action)}预览，用户必须通过网页确认后才写入。"+guidance),input_schema=schema,output_schema={"type":"object","properties":{},"additionalProperties":True},required_permissions=_permissions(action),effect=ToolEffect.WRITE if hidden else ToolEffect.READ,execution_mode=ExecutionMode.SEQUENTIAL,retry_policy=RetryPolicy.NEVER,timeout_seconds=30,execute=run,expose_to_model=not hidden)
def _label(a: str) -> str:return {"create":"新增","update":"修改","delete":"删除"}[a]
def _permissions(a: str) -> tuple[str, ...]:
 extra={"create":("mes:tm:tool:list","mes:tm:tooltype:query"),"update":("mes:tm:tool:query","mes:tm:tooltype:query"),"delete":("mes:tm:tool:query","mes:tm:tooltype:query")}
 return (_ACTIONS[a][2],*extra[a])
def _user(ctx: ToolContext) -> str:
 i=ctx.identity
 if not isinstance(i,Mapping):raise PreviewError("AUTH_INVALID","未取得有效MES用户身份。")
 if isinstance(i.get("userId"),int) and not isinstance(i.get("userId"),bool):return f"id:{i['userId']}"
 if isinstance(i.get("userName"),str) and i["userName"].strip():return f"name:{i['userName'].strip()}"
 raise PreviewError("AUTH_INVALID","未取得有效MES用户身份。")
def _client(ctx: ToolContext) -> KTGClient | None:return ctx.ktg_client if isinstance(ctx.ktg_client,KTGClient) else None
def _fail(m: str,c: str) -> ToolResult:return ToolResult(content=m,status=ToolStatus.FAILED,error_code=c)
async def _type(client: KTGClient, tid: int) -> JsonObject:
 p=await client.get(f"/mes/tm/tooltype/{tid}");v=p.get("data")
 if not isinstance(v,Mapping) or v.get("toolTypeId")!=tid:raise KTGError("TOOLTYPE_NOT_FOUND","工装夹具类型不存在或返回不一致。")
 code,name,flag=v.get("toolTypeCode"),v.get("toolTypeName"),v.get("codeFlag")
 if not isinstance(code,str) or not isinstance(name,str) or flag not in ("Y","N"):raise KTGError("KTG_INVALID_RESPONSE","关联工装夹具类型数据无效。")
 return {"toolTypeId":tid,"toolTypeCode":code,"toolTypeName":name,"codeFlag":flag}
def _txt(v: object,key: str,n: int,required: bool=False) -> str:
 if v is None and not required:return ""
 if not isinstance(v,str) or len(v)>n or (required and not v.strip()):raise KTGError("VALIDATION_ERROR",f"{key}必须是{n}字符以内{'非空' if required else ''}文本。")
 return v.strip()
def _date(v: object) -> str | None:
 if v is None:return None
 if not isinstance(v,str):raise KTGError("VALIDATION_ERROR","next_mainten_date须为yyyy-MM-dd或null。")
 from datetime import date
 try:d=date.fromisoformat(v)
 except ValueError as e:raise KTGError("VALIDATION_ERROR","next_mainten_date须为合法yyyy-MM-dd。") from e
 if d.isoformat()!=v:raise KTGError("VALIDATION_ERROR","next_mainten_date须为yyyy-MM-dd。")
 return v
def _period(v: object) -> int | None:
 if v is None:return None
 if not isinstance(v,int) or isinstance(v,bool) or not 1<=v<=2147483647:raise KTGError("VALIDATION_ERROR","维护周期须为1至2147483647的正整数。")
 return v
def _maintenance(record: Mapping[str, object]) -> None:
 kind=record.get("maintenType");date=record.get("nextMaintenDate");period=record.get("nextMaintenPeriod")
 if kind not in ("REGULAR","USAGE"):raise KTGError("VALIDATION_ERROR","维护方式无效。")
 if kind=="REGULAR":
  if period is not None:raise KTGError("VALIDATION_ERROR","定期维护记录含不适用周期，请先显式修正。")
  if date is not None:_date(date)
 else:
  if date is not None:raise KTGError("VALIDATION_ERROR","按使用次数维护记录含不适用日期，请先显式修正。")
  if period is not None:_period(period)
async def _unique(client: KTGClient,code: str,exclude: int | None=None) -> None:
 rows,total=tool_page(await client.get(TOOL_LIST,{"toolCode":code,"pageNum":1,"pageSize":100}))
 matches=[r for r in rows if cast(str, r["toolCode"]).casefold()==code.casefold() and r["toolId"]!=exclude]
 if matches:raise KTGError("TOOL_DUPLICATE","工装夹具编码已存在。")
 if total>100:raise KTGError("TOOL_DUPLICATE_CHECK_LIMIT","编码命中超过100条，无法安全确认唯一性。")
async def _build_create(client: KTGClient,args: Mapping[str, object]) -> tuple[JsonObject, JsonObject]:
 allowed={"tool_code","tool_name","tool_type_id","code_flag","quantity","brand","spec","mainten_type","next_mainten_date","next_mainten_period","remark"}
 if set(args)-allowed:raise KTGError("VALIDATION_ERROR","含不允许的新增字段。")
 code=_txt(args.get("tool_code"),"tool_code",64,True);name=_txt(args.get("tool_name"),"tool_name",100,True);tid=args.get("tool_type_id")
 if not isinstance(tid,int) or isinstance(tid,bool) or tid<=0:raise KTGError("VALIDATION_ERROR","必须提供有效tool_type_id。")
 t=await _type(client,tid);flag=t["codeFlag"]
 if "code_flag" in args:raise KTGError("VALIDATION_ERROR","新增不接受模型提供的code_flag，必须使用可信类型数据。")
 q=args.get("quantity",1 if flag=="Y" else 0)
 if not isinstance(q,int) or isinstance(q,bool) or not 0<=q<=99999999:raise KTGError("VALIDATION_ERROR","quantity须为0至99999999整数。")
 if flag=="Y" and q!=1:raise KTGError("VALIDATION_ERROR","独立编码类型quantity必须为1。")
 if flag=="N" and q==0 and "quantity" not in args: q=0
 kind=args.get("mainten_type");nd=_date(args.get("next_mainten_date"));np=_period(args.get("next_mainten_period"))
 if kind is None:kind="REGULAR"
 if kind=="REGULAR":
  if np is not None:raise KTGError("VALIDATION_ERROR","定期维护不能提供周期字段。")
 elif kind=="USAGE":
  if nd is not None:raise KTGError("VALIDATION_ERROR","按使用次数维护不能提供日期字段。")
 else:raise KTGError("VALIDATION_ERROR","mainten_type只允许REGULAR或USAGE。")
 after={"toolCode":code,"toolName":name,"toolTypeId":tid,"toolTypeCode":t["toolTypeCode"],"toolTypeName":t["toolTypeName"],"codeFlag":flag,"quantity":q,"quantityAvail":q,"status":"STORE","maintenType":kind,"nextMaintenDate":nd,"nextMaintenPeriod":np,"brand":_txt(args.get("brand",""),"brand",100),"spec":_txt(args.get("spec",""),"spec",100),"remark":_txt(args.get("remark",""),"remark",250)}
 await _unique(client,code);return t,cast(JsonObject,after)
async def _build_update(client: KTGClient,args: Mapping[str, object]) -> tuple[JsonObject, JsonObject]:
 if "tool_id" not in args or set(args)-({"tool_id"}|set(_ARG_TO_WIRE)):raise KTGError("VALIDATION_ERROR","修改只接受tool_id和白名单业务字段。")
 before=await fetch_tool(client,args["tool_id"]);after=dict(before)
 for arg,wire in _ARG_TO_WIRE.items():
  if arg not in args:continue
  v=args[arg]
  if arg in ("tool_name","brand","spec","remark"):after[wire]=_txt(v,arg,{"tool_name":100,"brand":100,"spec":100,"remark":250}[arg],arg=="tool_name")
  elif arg=="mainten_type":
   if v not in ("REGULAR","USAGE"):raise KTGError("VALIDATION_ERROR","mainten_type只允许REGULAR或USAGE。")
   after[wire]=cast(JsonValue,v)
  elif arg=="next_mainten_date":after[wire]=_date(v)
  else:after[wire]=_period(v)
 # A requested maintenance-mode change clears the incompatible field; omitted mode preserves legacy state.
 if "mainten_type" in args:
  if after["maintenType"]=="REGULAR":after["nextMaintenPeriod"]=None
  else:after["nextMaintenDate"]=None
 # Always validate snapshot shape, but retain untouched optional legacy values.
 _maintenance(after)
 changed=[f for f in MUTABLE if after.get(f)!=before.get(f)]
 if not changed:raise KTGError("NO_CHANGE","没有字段发生变化。")
 t=await _type(client,cast(int,before["toolTypeId"]))
 if t["toolTypeCode"]!=before.get("toolTypeCode") or t["toolTypeName"]!=before.get("toolTypeName") or t["codeFlag"]!=before.get("codeFlag"):raise KTGError("PREVIEW_STALE","类型关联已变化，请重新读取。")
 after["changedFields"]=cast(JsonValue,changed);return before,after
async def _safe_delete(client: KTGClient,before: Mapping[str, object]) -> None:
 quantity, available = before.get("quantity"), before.get("quantityAvail")
 if (
  before.get("status") != "STORE"
  or not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0
  or not isinstance(available, int) or isinstance(available, bool) or available < 0
  or quantity != available
 ):
  raise KTGError("TOOL_NOT_DELETABLE","仅允许删除在库且全部可用的工装夹具。")
 return None
async def _preview(store: TmToolPreviewStore,action: str,ctx: ToolContext,args: Mapping[str, object]) -> ToolResult:
 client=_client(ctx)
 if client is None:return _fail("KTG 查询客户端未配置。","KTG_CLIENT_MISSING")
 try:
  if action=="create":before,after=await _build_create(client,args)
  elif action=="update":before,after=await _build_update(client,args)
  else:
   if set(args)!={"tool_id"}:raise KTGError("VALIDATION_ERROR","删除仅接受tool_id。")
   before=await fetch_tool(client,args["tool_id"]);await _safe_delete(client,before);after={k:before[k] for k in ("toolId","toolCode","toolName","status","quantity","quantityAvail")}
  pid=store.create(user_key=_user(ctx),action=action,before=before,after=after)
  return ToolResult(content=_preview_text(action,pid,before,after),details={"kind":"tm_tool_preview","action":action,"preview_id":pid,"before":before,"after":after,"warnings":[]},operation_id=pid)
 except (KTGError,PreviewError) as e:return _fail(str(e),e.code)
def _preview_text(a: str,pid: str,before: JsonObject | None,after: JsonObject) -> str:
 if a=="delete":return f"删除预览 {pid}：{after['toolCode']} {after['toolName']}；在库{after['quantity']}，可用{after['quantityAvail']}。"
 if a=="create":return f"新增预览 {pid}：{after['toolCode']} {after['toolName']}；类型{after['toolTypeName']}；数量{after['quantity']}，状态在库。"
 if before is None:
  raise PreviewError("PREVIEW_STALE", "修改预览缺少可信快照。")
 changed_fields = cast(list[str], after.get("changedFields", []))
 changes="；".join(f"{field}: {before.get(field)} → {after.get(field)}" for field in changed_fields)
 return f"修改预览 {pid}：{after['toolCode']} {after['toolName']}；变更 {changes}。"
async def _apply(store: TmToolPreviewStore,action: str,ctx: ToolContext,args: Mapping[str, object]) -> ToolResult:
 client=_client(ctx)
 if client is None:return _fail("KTG 查询客户端未配置。","KTG_CLIENT_MISSING")
 if ctx.channel is not ToolChannel.TRUSTED_CONFIRMATION:return _fail("仅可通过网页确认写入。","CONFIRMATION_REQUIRED")
 pid=args.get("operation_id")
 if not isinstance(pid,str) or ctx.operation_id!=pid or not ctx.confirmation_id:return _fail("确认上下文不匹配。","CONFIRMATION_MISMATCH")
 begun=False
 try:
  rec,cached=store.begin(preview_id=pid,user_key=_user(ctx),action=action)
  if cached:return cached
  begun=True
  if action=="create":
    if rec.before is None:
     raise PreviewError("PREVIEW_STALE","新增预览缺少关联工装类型快照。")
    current_type=await _type(client,cast(int,rec.before["toolTypeId"]))
    if current_type!=rec.before:raise PreviewError("PREVIEW_STALE","关联工装类型已变化，请重新预览。")
  elif rec.before:
   cur=await fetch_tool(client,rec.before["toolId"])
   if any(cur.get(k)!=rec.before.get(k) for k in SNAPSHOT_FIELDS):raise PreviewError("PREVIEW_STALE","工装夹具发生变化，请重新预览。")
   t=await _type(client,cast(int,rec.before["toolTypeId"]))
   if any(t.get(k)!=rec.before.get(k) for k in ("toolTypeCode","toolTypeName","codeFlag")):raise PreviewError("PREVIEW_STALE","关联工装类型已变化，请重新预览。")
  if action=="create":res=await _apply_create(client,pid,rec.after)
  elif action=="update":
   if rec.before is None:raise PreviewError("PREVIEW_STALE","修改预览缺少可信快照。")
   res=await _apply_update(client,pid,rec.before,rec.after)
  else:
   if rec.before is None:raise PreviewError("PREVIEW_STALE","删除预览缺少可信快照。")
   await _safe_delete(client,rec.before)
   res=await _apply_delete(client,pid,rec.before)
  store.finish(pid,res);return res
 except (KTGError,PreviewError) as e:
  outcome="unknown" if isinstance(e,KTGError) and e.code in {"KTG_CONNECTION_ERROR","KTG_HTTP_ERROR"} else "failed"
  res=_result(action,outcome,str(e),pid,e.code if isinstance(e,(KTGError,PreviewError)) else "FAILED")
  if begun:store.finish(pid,res)
  return res
async def _apply_create(client: KTGClient,pid: str,after: JsonObject) -> ToolResult:
 tool_code = cast(str, after["toolCode"])
 await _unique(client,tool_code)
 try:await client.post(TOOL_PATH,{k:after[k] for k in ("toolCode","toolName","toolTypeId","toolTypeCode","toolTypeName","codeFlag","quantity","quantityAvail","maintenType","nextMaintenDate","nextMaintenPeriod","status","brand","spec","remark")})
 except KTGError as e:
  if e.code in {"KTG_INVALID_RESPONSE", "KTG_HTTP_ERROR", "KTG_CONNECTION_ERROR"}:return _result("create","unknown","新增已发送但结果未核实，请勿重试。",pid,"WRITE_RESULT_UNVERIFIED")
  raise
 try:
  rows,total=tool_page(await client.get(TOOL_LIST,{"toolCode":after["toolCode"],"pageNum":1,"pageSize":100}))
 except KTGError:
  return _result("create","unknown","新增已发送但回读未核实，请勿重试。",pid,"WRITE_RESULT_UNVERIFIED")
 matches=[r for r in rows if cast(str, r["toolCode"]).casefold()==cast(str, after["toolCode"]).casefold() and all(r.get(k)==after.get(k) for k in ("toolCode","toolName","toolTypeId","toolTypeCode","toolTypeName","codeFlag","quantity","quantityAvail","maintenType","nextMaintenDate","nextMaintenPeriod","status","brand","spec","remark"))]
 return _result("create","succeeded" if total==1 and len(matches)==1 else "unknown","新增已回读核验。" if total==1 and len(matches)==1 else "新增后无法唯一核实，请勿重试。",pid,None if len(matches)==1 else "WRITE_RESULT_UNVERIFIED",matches[0] if len(matches)==1 else None)
async def _apply_update(client: KTGClient,pid: str,before: JsonObject | None,after: JsonObject) -> ToolResult:
 if before is None:
  raise PreviewError("PREVIEW_STALE", "修改预览缺少可信快照。")
 body={k:after[k] for k in MUTABLE};body["toolId"]=before["toolId"]
 body.update({k:before[k] for k in ("toolTypeId","toolTypeCode","toolTypeName","codeFlag","quantity","quantityAvail","status")})
 if after["maintenType"]=="REGULAR":body["nextMaintenPeriod"]=None
 else:body["nextMaintenDate"]=None
 try:await client.put(TOOL_PATH,body)
 except KTGError as e:
  if e.code in {"KTG_INVALID_RESPONSE", "KTG_HTTP_ERROR", "KTG_CONNECTION_ERROR"}:return _result("update","unknown","修改已发送但结果未核实，请勿重试。",pid,"WRITE_RESULT_UNVERIFIED")
  raise
 try:cur=await fetch_tool(client,before["toolId"])
 except KTGError:return _result("update","unknown","修改已发送但回读未核实，请勿重试。",pid,"WRITE_RESULT_UNVERIFIED")
 ok=all(cur.get(k)==after.get(k) for k in SNAPSHOT_FIELDS)
 return _result("update","succeeded" if ok else "unknown","修改已回读核验。" if ok else "修改后回读不符，请先核对。",pid,None if ok else "WRITE_RESULT_UNVERIFIED",cur if ok else None)
async def _apply_delete(client: KTGClient,pid: str,before: JsonObject) -> ToolResult:
 try:await client.delete(f"{TOOL_PATH}/{before['toolId']}")
 except KTGError as e:
  if e.code in {"KTG_INVALID_RESPONSE", "KTG_HTTP_ERROR", "KTG_CONNECTION_ERROR"}:return _result("delete","unknown","删除已发送但结果未核实，请勿重试。",pid,"WRITE_RESULT_UNVERIFIED")
  raise
 try:await fetch_tool(client,before["toolId"])
 except KTGError as e:
  if e.code=="TOOL_NOT_FOUND":return _result("delete","succeeded","已删除并回读确认不存在。",pid,deleted={k:before[k] for k in ("toolId","toolCode","toolName")})
  return _result("delete","unknown","删除已发送但回读未核实，请勿重试。",pid,"WRITE_RESULT_UNVERIFIED")
 return _result("delete","unknown","删除后仍能读取记录，请核对状态。",pid,"WRITE_RESULT_UNVERIFIED")
def _result(action: str,outcome: str,label: str,pid: str,error: str | None=None,record: JsonObject | None=None,deleted: JsonObject | None=None) -> ToolResult:
 details: JsonObject={"kind":f"tm_tool_{action}_result","action":action,"outcome":outcome,"label":label,"preview_id":pid}
 details["verified"] = outcome == "succeeded"
 if record is not None:details["verified_record"]=record
 if deleted is not None:details["deleted"]=deleted
 status=ToolStatus.SUCCEEDED if outcome=="succeeded" else ToolStatus.UNKNOWN if outcome=="unknown" else ToolStatus.FAILED
 return ToolResult(content=label,details=details,status=status,error_code=None if status is ToolStatus.SUCCEEDED else error or "WRITE_FAILED",operation_id=pid)
