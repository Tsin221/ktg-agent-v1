package com.ktg.mes.pro.execution;

import java.util.EnumMap;
import java.util.Map;

/** Derived counts only, not an additional source of production facts. */
public final class ExecutionStatistics {
    private final Map<ExecutionState, Long> counts = new EnumMap<>(ExecutionState.class);
    private long allocated;

    private ExecutionStatistics() {}

    public static ExecutionStatistics fromStates(Iterable<ExecutionState> unitStates) {
        ExecutionStatistics result = new ExecutionStatistics();
        for (ExecutionState state : unitStates) {
            if (state == null) throw ExecutionException.stateConflict("单件状态缺失");
            result.counts.merge(state, 1L, Long::sum);
            result.allocated++;
        }
        return result;
    }

    private long count(ExecutionState state) { return counts.getOrDefault(state, 0L); }
    public long getAllocated() { return allocated; }
    public long getAssembled() { return allocated - count(ExecutionState.WAIT_ASSEMBLY); }
    public long getQualified() { return getPackable() + getPacked(); }
    public long getPackable() { return count(ExecutionState.WAIT_PACKING); }
    public long getPacked() { return count(ExecutionState.PACKED); }
    public long getScrapped() { return count(ExecutionState.SCRAPPED); }
    public long getPendingDisposition() { return count(ExecutionState.WAIT_DISPOSITION); }
    public long getReworkInProgress() { return count(ExecutionState.REWORK_IN_PROGRESS); }
    public long getWaitingReinspection() { return count(ExecutionState.WAIT_REINSPECTION); }
    public long getWaitingInspection() { return count(ExecutionState.WAIT_INSPECTION); }
    public long getWaitingAssembly() { return count(ExecutionState.WAIT_ASSEMBLY); }
    /** Other workorder completion requirements (task facts, planned length) are checked separately. */
    public boolean allTerminal() { return allocated > 0 && allocated == getPacked() + getScrapped(); }
}
