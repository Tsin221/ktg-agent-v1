package com.ktg.mes.pro.execution;

/** The original task status field remains authoritative. */
public enum TaskExecutionState {
    WAITING, READY, STARTED, FINISHED;

    /** Caller must separately check predecessor/unit eligibility and actual time facts. */
    TaskExecutionState advance(TaskExecutionState next) {
        if ((this == WAITING && next == READY) || (this == READY && next == STARTED)
                || (this == STARTED && next == FINISHED)) return next;
        throw ExecutionException.stateConflict("当前任务状态不允许该操作");
    }

    public TaskExecutionState ready() { return advance(READY); }
    public TaskExecutionState start() { return advance(STARTED); }
    public TaskExecutionState finish() { return advance(FINISHED); }
}
