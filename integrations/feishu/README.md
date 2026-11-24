# 飞书单聊接入

## 2026-11-22 会话与任务版（已部署，待真实飞书客户端验收）

- 普通消息复用主 Agent 的持久会话接口；通过 `page_context.response_style=brief` 请求主 Agent 直接生成短结论，不增加模型调用。飞书只显示回答和「打开 MES」，不展示业务数据表格、流程图或单独核验卡片；查询状态由 Agent 的回答说明。
- 每个飞书单聊在当前 MES 令牌有效期内自动复用一个会话，不提供新建或切换命令。网页和飞书的身份、会话及任务彼此独立；保持原有 7 小时飞书令牌缓存机制，重新登录后使用新会话，旧任务不会自动迁移。
- 发送 `任务列表` 查看当前会话最近 10 个任务（含已完成），显示编号、标题、已完成步数和当前步骤。发送 `继续任务 1` 按最近一次列表的编号续办；任务已完成时直接提示，不启动模型。正常问答不会自动附任务列表，列表请求与普通消息使用同一个顺序队列。
- Bot 仅通过 `/agent-api/v1/sessions`、会话运行、运行查询和任务读取接口交互；允许查询与生成预览，不发起任何业务确认写入接口。
- Bot 的 SQLite 兼容新增 `agent_sessions`、`agent_runs`、`agent_task_lists` 表，按飞书 owner 和当前令牌保存会话、运行及任务编号映射。已接纳的运行可在进程重启后查询结果；回复重试使用保存的卡片。
- 运行本地回归：从 `ktg_agent_v1` 执行 `python -m unittest discover -s integrations/feishu -v`。本版本新增的短回答参数需要对应 Agent 服务版本支持。
- 本地验收：22 项飞书测试、35 项 Agent 会话/HTTP/身份测试通过。扩展回归有两项原有小上下文窗口测试失败，已按本轮确认的范围留待单独修复。
- 云端发布 `feishu-session-20261122-143814`：只更新 Agent 与飞书容器，实际加载 `deepseek-flash`、上下文 `1048576`、单次输出及预留 `65536`。原身份隔离保持启用，网页和后端、数据库等其他容器 ID 不变。
- 云端离线验收：22 项飞书测试与真实 Agent HTTP 接口的模拟模型端到端流程均通过。发布后 Agent 与飞书容器 healthy，长连接 connected=true，网页 `/demo-health` ready=true；原有 6 条消息任务仍保留。未发送真实飞书消息、未调用真实模型。
- 服务器发布记录：`/opt/ktg/releases/feishu-session-20261122-143814/receipt.json`；回退配置及 SQLite 一致性备份：`/opt/ktg/backups/feishu-session-20261122-143814`。旧镜像保留为 `ktg-agent:task-save-fix-20261121` 与 `ktg-feishu:20261121`。

以下为原第一版的部署与历史验收说明。

## 第一版（历史）

第一版范围：企业自建应用通过官方 SDK 长连接接收单聊文本，复用 KTG Agent `/agent-api/v1/query`，返回结果卡片和“打开 MES”链接。仅使用 `ktg_demo` 的 demo 账号。此版本是单轮查询，不声称支持飞书审批、网页会话同步或跨端确认写入。

## 卡片展示

使用飞书原生分栏组成字段表格和箭头节点，不依赖聊天窗口渲染 Markdown 表格或 Mermaid。维修单优先读取工具返回的结构化 repair 与维修明细，展示设备、故障、处理、维修结果；已记录的报修/完工/验收日期组成时间线。未查询或缺失的字段明确标记，不从模型长文推断事实或补造流程节点。其他列表最多展示五条，保留实际总数与分页提示；其他回答转为简短字段表格或摘要。预览始终标记“未提交”，错误保留不完整提示。业务查询、权限与数据库逻辑保持原样。

原生组件依据：https://open.feishu.cn/document/common-capabilities/message-card/message-cards-content/column-set 。部署后新问题使用新排版，历史卡片保留原样。

实现顺序与验收：
1. 输入校验、按用户隔离、消息持久去重、回复重试与中断恢复；通过离线测试。
2. 官方 SDK 长连接、私密配置、后台队列和卡片回复；Linux 镜像离线安装通过。
3. 添加独立容器；验证应用凭据、WebSocket 连接和已有网页健康，不主动发送消息或调用真实模型。
4. 用户保存接收消息事件并发布应用后，亲自发起真实查询。

消息回调只将任务存入 SQLite，耗时查询在后台执行，以及时响应飞书事件。每个飞书租户、用户和单聊使用独立 MES 登录令牌。不会使用管理员账号，也不会调用 Agent 的任何 confirm 接口。模型只能通过现有已注册工具访问 MES。

输入边界：仅接收当前应用的用户单聊文本、2,000 字以内、24 小时内事件。消息 ID 为持久去重键；队列上限 100，每个用户每分钟最多接收 20 条。同一事件回复使用稳定 UUID。回复失败只重发已保存卡片；中断的查询不自动重放，提示用户重新提问。卡片答案使用 plain_text，不能生成额外按钮或 @所有人。

凭据：本机 `.runtime/feishu/.env`，服务器 `/opt/ktg/secrets/feishu.env`。环境变量 FEISHU_APP_ID、FEISHU_APP_SECRET。业务账号只读挂载现有 demo-gateway/account.json。日志不打印令牌、密钥、用户问题、模型原文或 WebSocket 地址。

本机测试：`python -m unittest discover -s integrations/feishu -v`。

部署采用补充 Compose 文件，不重建现有服务。回滚：使用两个 Compose 文件执行 `stop feishu`，网页和数据库保持运行。保留 SQLite 状态，避免重启后重复查询。真实模型验证由用户自行发起。

官方依据：
- https://open.feishu.cn/document/ukTMukTMukTM/uYDNxYjL2QTM24iN0EjN/event-subscription-configure-
- https://github.com/larksuite/oapi-sdk-python
- https://open.feishu.cn/document/server-docs/im-v1/message/reply

## 当前部署状态（2026-11-21）

Linux 与 Windows 的 10 项离线检查通过，官方 SDK 1.8.0 在 Linux Python 3.12 中导入成功。服务器已具备独立镜像与 Compose 补充配置。

旧个人身份应用曾返回 code=21001（平台策略拒绝），未直接判定为密钥错误。用户切换到组织身份并新建“KTG助手”（描述“MES助手”）后，更新凭据重新验证：飞书返回 code=0，服务器日志确认 credentials_validated、feishu_connection_established，容器 healthy，健康文件 connected=true。旧凭据已由新应用凭据替换。原网页演示服务健康。用户已在新应用完成机器人能力、消息权限、事件订阅与发布，并亲自发送首个真实问题。服务器日志确认 query_reply_completed，用户截图显示收到查询结果卡片与“打开 MES”按钮。

更新服务器代码后使用以下命令启动（应用凭据必须来自有权使用的平台组织）：

```sh
cd /opt/ktg
sudo docker compose -f compose.private.json -f compose.feishu.json up -d --no-deps --pull never --force-recreate feishu
sudo docker compose -f compose.private.json -f compose.feishu.json logs --tail 20 feishu
```

长连接就绪后，在飞书后台选择“使用长连接接收事件”，添加“接收消息 v2.0”事件（im.message.receive_v1），发布应用到本人可用范围。不要在连接未就绪时反复保存。此版本只使用跳转按钮，无需添加卡片确认回调。

## 首次真实验收

用户在飞书单聊发送：“查询维修单 REP-001 的设备、故障描述和当前状态。”

收到的结果已只读核对 ktg_demo.dv_repair 与 dv_repair_line：设备 DEV-001 一号装配机；故障“运行时连接件异响”；维修描述“检查并紧固连接件，试运行无异响”；状态 FINISHED；维修结果 SUCCESS；报修/完工/验收日期分别为 2026-11-11、12、13；共一条维修明细，维修/验收人员字段均为空。与飞书截图一致。

本次证明单轮自然语言查询、现有 Agent/MES 工具读取及飞书卡片回复链路实际可用。未验证飞书内多轮对话、确认写入或网页会话同步；“打开 MES”是已有网页入口，不携带飞书令牌或自动同步当前聊天。

排版更新验证：14 项离线检查在 Windows 与 Linux 上通过；只重建并重启飞书容器，长连接已重新建立，MES 网页健康。新请求使用字段表格与原生箭头时间线，旧卡片未批量修改。未主动发消息或额外调用模型，真实客户端外观由用户重发问题确认。旧镜像保留为 ktg-feishu:before-compact-20261121。
