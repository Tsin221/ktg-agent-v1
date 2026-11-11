"""Run-local budget for persisted task steps."""

from dataclasses import dataclass, field

from ktg_agent.core.messages import JsonValue, ToolCall

TASK_SAVE_LIMIT = 6
TASK_CLOSEOUT_TURNS = 2
TASK_TOOLS = frozenset({"create_task", "update_task"})
DIRECTORY_TOOLS = frozenset({"search_tools", "load_tools"})


@dataclass(slots=True)
class TaskBudget:
    task_id: str | None = None
    steps: tuple[tuple[str, str], ...] = ()
    work_calls: int = 0
    saves: int = 0
    closeout_turns: int = 0
    step_calls: dict[tuple[str, str], int] = field(default_factory=dict)

    def set_task(self, task_id: str, steps: JsonValue) -> None:
        self.task_id = task_id
        normalized: list[tuple[str, str]] = []
        for step in steps if isinstance(steps, list) else ():
            if isinstance(step, dict):
                step_id, status = step.get("step_id"), step.get("status")
                if isinstance(step_id, str) and isinstance(status, str):
                    normalized.append((step_id, status))
        self.steps = tuple(normalized)

    def step_quota(self, work_limit: int) -> tuple[tuple[str, str] | None, int]:
        remaining = max(0, work_limit - self.work_calls)
        if self.task_id is None:
            return None, remaining
        unfinished = [step for step, status in self.steps if status != "completed"]
        current = next((step for step, status in self.steps if status == "in_progress"),
                       unfinished[0] if unfinished else None)
        if current is None:
            return None, remaining
        key = (self.task_id, current)
        # Unspent calls stay in the shared pool. Give each later unfinished
        # step a chance to start, while the current step can use the surplus.
        later = sum((self.task_id, step) not in self.step_calls for step in unfinished if step != current)
        minimum = int(remaining > 0 and key not in self.step_calls)
        return key, max(minimum, remaining - later)

    def admit(self, calls: tuple[ToolCall, ...], work_limit: int) -> tuple[tuple[ToolCall, ...], dict[str, str]]:
        admitted: list[ToolCall] = []
        rejected: dict[str, str] = {}
        for call in calls:
            key, step_remaining = self.step_quota(work_limit)
            if self.closeout_turns and (
                self.closeout_turns != 1 or call.name != "update_task"
                or call.arguments.get("task_id") != self.task_id or admitted
            ):
                rejected[call.id] = "TASK_CLOSEOUT"
            elif call.name in TASK_TOOLS:
                save_limit = TASK_SAVE_LIMIT if self.closeout_turns else TASK_SAVE_LIMIT - 1
                if self.saves >= save_limit:
                    rejected[call.id] = "TASK_SAVE_LIMIT"
                else:
                    self.saves += 1
                    admitted.append(call)
            elif self.work_calls >= work_limit:
                rejected[call.id] = "RUN_LIMIT_REACHED"
            elif key is not None and call.name not in DIRECTORY_TOOLS and step_remaining <= 0:
                rejected[call.id] = "TASK_STEP_LIMIT"
            else:
                self.work_calls += 1
                admitted.append(call)
                if key is not None and call.name not in DIRECTORY_TOOLS:
                    self.step_calls[key] = self.step_calls.get(key, 0) + 1
        return tuple(admitted), rejected
