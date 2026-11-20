# 飞书单聊接入

第一版范围：企业自建应用通过官方 SDK 长连接接收单聊文本，复用 KTG Agent `/agent-api/v1/query`，返回结果卡片和“打开 MES”链接。仅使用 `ktg_demo` 的 demo 账号。此版本是单轮查询，不声称支持飞书审批、网页会话同步或跨端确认写入。

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

实际鉴权返回 code=21001，msg="Access denied due to platform policy. If you have questions, contact customer support for help."。这只能确认平台拒绝签发访问令牌，不能直接判定为密钥错误。用户截图中的应用归属显示“豆包账号”，待核对个人/组织身份及组织访问策略。长连接尚未建立，飞书容器已暂停，原网页演示服务健康。没有发送外部聊天消息或执行真实模型请求。

更新服务器代码后使用以下命令启动（应用凭据必须来自有权使用的平台组织）：

```sh
cd /opt/ktg
sudo docker compose -f compose.private.json -f compose.feishu.json up -d --no-deps --pull never --force-recreate feishu
sudo docker compose -f compose.private.json -f compose.feishu.json logs --tail 20 feishu
```

长连接就绪后，在飞书后台选择“使用长连接接收事件”，添加“接收消息 v2.0”事件（im.message.receive_v1），发布应用到本人可用范围。不要在连接未就绪时反复保存。此版本只使用跳转按钮，无需添加卡片确认回调。
