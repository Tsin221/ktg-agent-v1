package com.ktg.mes.pro.execution;

/** Authoritative unit state machine. It never writes a database row on its own. */
public enum ExecutionState {
    WAIT_ASSEMBLY, WAIT_INSPECTION, WAIT_DISPOSITION, REWORK_IN_PROGRESS,
    WAIT_REINSPECTION, WAIT_PACKING, PACKED, SCRAPPED;

    public boolean isTerminal() {
        return this == PACKED || this == SCRAPPED;
    }

    public ExecutionState advance(ExecutionAction action) {
        switch (this) {
            case WAIT_ASSEMBLY:
                if (action == ExecutionAction.REPORT) return WAIT_INSPECTION;
                break;
            case WAIT_INSPECTION:
            case WAIT_REINSPECTION:
                if (action == ExecutionAction.INSPECT_ACCEPT) return WAIT_PACKING;
                if (action == ExecutionAction.INSPECT_REJECT) return WAIT_DISPOSITION;
                break;
            case WAIT_DISPOSITION:
                if (action == ExecutionAction.DISPOSITION_REWORK) return REWORK_IN_PROGRESS;
                if (action == ExecutionAction.DISPOSITION_SCRAP) return SCRAPPED;
                break;
            case REWORK_IN_PROGRESS:
                if (action == ExecutionAction.REWORK_COMPLETE) return WAIT_REINSPECTION;
                break;
            case WAIT_PACKING:
                if (action == ExecutionAction.PACK) return PACKED;
                break;
            default:
                break;
        }
        throw ExecutionException.stateConflict("当前单件状态不允许该操作");
    }
}
