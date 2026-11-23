package com.ktg.mes.pro.execution;

/** States applicable to the controlled workflow, not a migration of historical values. */
public enum WorkorderExecutionState {
    PREPARE, CONFIRMED, FINISHED;

    public WorkorderExecutionState advance(ExecutionAction action) {
        if (this == PREPARE && action == ExecutionAction.RELEASE) return CONFIRMED;
        if (this == CONFIRMED && action == ExecutionAction.COMPLETE) return FINISHED;
        throw ExecutionException.stateConflict("当前工单状态不允许该操作");
    }
}
