package com.ktg.mes.pro.execution;

/** Stable operation action codes; endpoint-specific DTOs select these, not callers. */
public enum ExecutionAction {
    RELEASE("release"),
    ASSIGN("assign"),
    START("start"),
    REPORT("report"),
    INSPECT_ACCEPT("inspect"),
    INSPECT_REJECT("inspect"),
    DISPOSITION_REWORK("disposition"),
    DISPOSITION_SCRAP("disposition"),
    REWORK_COMPLETE("rework"),
    PACK("pack"),
    COMPLETE("complete"),
    TASK_COMPLETE("complete");

    private final String permission;

    ExecutionAction(String suffix) {
        this.permission = "mes:pro:execution:" + suffix;
    }

    public String getPermission() {
        return permission;
    }
}
