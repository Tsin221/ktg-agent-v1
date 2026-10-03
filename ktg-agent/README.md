# KTG Agent

KTG Agent 的 Python 核心包。当前版本只提供可安装、可导入的最小项目骨架；导入包不会连接网络、模型服务或 KTG。

## 环境要求

- Python 3.12
- uv 0.11.21

运行依赖为空。开发依赖通过 `uv.lock` 固定，其中测试入口使用 pytest 9.0.3、类型检查使用 mypy 1.18.2；构建后端固定为 hatchling 1.27.0。Node.js 和 Java 环境不是此 Python 包的依赖。

## 安装

进入本目录后，使用锁文件创建隔离环境并安装项目：

```powershell
uv sync --locked
```

该命令会创建 `.venv`，安装当前项目及固定版本的开发依赖。若只需要运行包、不需要测试工具：

```powershell
uv sync --locked --no-dev
```

## 验证导入

```powershell
uv run --locked python -c "import ktg_agent; print(ktg_agent.__version__)"
```

预期输出：

```text
0.1.0
```

## 运行测试

```powershell
uv run --locked pytest
```

测试覆盖包导入、消息转换与投影、工具调用关联、Provider 事件流及终止边界；包导入过程不会尝试建立网络连接。

## 核心消息与 Provider 协议

`ktg_agent.core.messages` 定义四类消息：`SystemMessage`、`UserMessage`、`AssistantMessage` 和 `ToolResultMessage`。消息内容由 `TextBlock` 与完整 `ToolCall` 组成；工具结果通过 `tool_call_id` 对应调用。`project_model_messages` 会过滤 UI 专用文本和工具结果详情，并校验调用 ID 的唯一性、顺序和完整性。

`ktg_agent.providers.base` 定义供应商无关的 `Provider` 流接口。输入包含模型消息、工具 schema、模型配置与可选取消信号；输出事件为 `TextDelta`、`ToolCallCompleted` 和最终的 `ResponseCompleted`。`StopReason` 明确区分正常结束、工具调用、长度截断、错误和取消。

运行严格类型检查：

```powershell
uv run --locked mypy
```

## 最小示例

```python
import ktg_agent

print(ktg_agent.__version__)
```
