"""Apply the T29 status handoff to task documents outside this checkout."""

from pathlib import Path


TASKS = Path(r"E:\ktg_agent\docs\ktg_agent\任务")
for number, title in (
    (24, "上下文装配边界与新消息集合"),
    (25, "token计量与工具结果spill"),
    (26, "窗口装配与确定性切分"),
    (27, "压缩执行与手动压缩命令"),
    (28, "摘要持久化与恢复"),
):
    path = TASKS / f"T{number}-{title}.md"
    with path.open("r", encoding="utf-8", newline="") as source:
        text = source.read()
    lines = text.splitlines(keepends=True)
    assert lines[2].startswith("状态：")
    ending = "\r\n" if lines[2].endswith("\r\n") else "\n"
    detail = lines[2].split("。", 1)[1] if "。" in lines[2] else ""
    lines[2] = f"状态：已完成（2026-10-21）。{detail.rstrip()}{ending}"
    assert "前一任务：" in text and "后一任务：" in text
    with path.open("w", encoding="utf-8", newline="") as target:
        target.write("".join(lines))

overview = TASKS / "05-上下文管理与会话摘要.md"
with overview.open("r", encoding="utf-8", newline="") as source:
    text = source.read()
lines = text.splitlines(keepends=True)
assert lines[2].startswith("状态：")
ending = "\r\n" if lines[2].endswith("\r\n") else "\n"
lines[2] = ("状态：本批已完成，T29 浏览器主路径验收通过（2026-10-21）。"
            "任务表链接保持原样。" + ending)
with overview.open("w", encoding="utf-8", newline="") as target:
    target.write("".join(lines))

t29 = TASKS / "T29-上下文可观察与整包验收.md"
with t29.open("r", encoding="utf-8", newline="") as source:
    text = source.read()
lines = text.splitlines(keepends=True)
assert lines[2].startswith("状态：")
ending = "\r\n" if lines[2].endswith("\r\n") else "\n"
lines[2] = "状态：已完成（2026-10-21）；浏览器主路径通过，after_seq 报文未单独抓包。" + ending
text = "".join(lines)
lines = text.splitlines(keepends=True)
record = next(index for index, line in enumerate(lines)
              if line.startswith("**交付记录**："))
ending = "\r\n" if lines[record].endswith("\r\n") else "\n"
lines[record] = ("**交付记录**：详见工作区 `ktg-agent/docs/T24-T29-context-acceptance.md`；"
                 "含浏览器压缩、刷新和运行中重连证据。" + ending)
text = "".join(lines)
with t29.open("w", encoding="utf-8", newline="") as target:
    target.write(text)
