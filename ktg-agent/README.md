# KTG Agent

KTG Agent 的 Python 核心包。当前版本只提供可安装、可导入的最小项目骨架；导入包不会连接网络、模型服务或 KTG。

## 环境要求

- Python 3.12
- uv 0.11.21

运行依赖为空。开发依赖通过 `uv.lock` 固定，其中测试入口使用 pytest 9.0.3；构建后端固定为 hatchling 1.27.0。Node.js 和 Java 环境不是此 Python 包的依赖。

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

测试验证 `ktg_agent` 可以导入，并且导入过程不会尝试建立网络连接。

## 最小示例

```python
import ktg_agent

print(ktg_agent.__version__)
```
