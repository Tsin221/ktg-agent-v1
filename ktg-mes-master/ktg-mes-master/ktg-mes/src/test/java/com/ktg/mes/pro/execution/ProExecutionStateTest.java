package com.ktg.mes.pro.execution;

import org.junit.jupiter.api.Test;
import java.util.Arrays;

import static com.ktg.mes.pro.execution.ExecutionAction.*;
import static com.ktg.mes.pro.execution.ExecutionState.*;
import static org.junit.jupiter.api.Assertions.*;

class ProExecutionStateTest {
    @Test
    void legalUnitTransitionsFollowInspectionAndRework() {
        assertEquals(WAIT_INSPECTION, WAIT_ASSEMBLY.advance(REPORT));
        assertEquals(WAIT_PACKING, WAIT_INSPECTION.advance(INSPECT_ACCEPT));
        assertEquals(WAIT_DISPOSITION, WAIT_INSPECTION.advance(INSPECT_REJECT));
        assertEquals(REWORK_IN_PROGRESS, WAIT_DISPOSITION.advance(DISPOSITION_REWORK));
        assertEquals(WAIT_REINSPECTION, REWORK_IN_PROGRESS.advance(REWORK_COMPLETE));
        assertEquals(WAIT_DISPOSITION, WAIT_REINSPECTION.advance(INSPECT_REJECT));
        assertEquals(WAIT_PACKING, WAIT_REINSPECTION.advance(INSPECT_ACCEPT));
        assertEquals(SCRAPPED, WAIT_DISPOSITION.advance(DISPOSITION_SCRAP));
        assertEquals(PACKED, WAIT_PACKING.advance(PACK));
    }

    @Test
    void illegalUnitTransitionsIncludingTerminalStatesAreRejected() {
        assertEquals("STATE_CONFLICT", assertThrows(ExecutionException.class,
                () -> WAIT_ASSEMBLY.advance(PACK)).getErrorCode());
        for (ExecutionAction action : ExecutionAction.values()) {
            assertThrows(ExecutionException.class, () -> PACKED.advance(action));
            assertThrows(ExecutionException.class, () -> SCRAPPED.advance(action));
        }
    }

    @Test
    void taskAndWorkorderStatesCannotSkipSteps() {
        assertEquals(TaskExecutionState.READY, TaskExecutionState.WAITING.advance(TaskExecutionState.READY));
        assertEquals(TaskExecutionState.STARTED, TaskExecutionState.READY.advance(TaskExecutionState.STARTED));
        assertEquals(TaskExecutionState.FINISHED, TaskExecutionState.STARTED.advance(TaskExecutionState.FINISHED));
        assertThrows(ExecutionException.class, () -> TaskExecutionState.WAITING.advance(TaskExecutionState.FINISHED));
        assertThrows(ExecutionException.class, () -> TaskExecutionState.STARTED.advance(TaskExecutionState.READY));
        assertEquals(WorkorderExecutionState.CONFIRMED,
                WorkorderExecutionState.PREPARE.advance(RELEASE));
        assertEquals(WorkorderExecutionState.FINISHED,
                WorkorderExecutionState.CONFIRMED.advance(COMPLETE));
        assertThrows(ExecutionException.class, () -> WorkorderExecutionState.PREPARE.advance(COMPLETE));
    }

    @Test
    void unitCountsKeepAllocatedAssemblyQualificationPackingAndScrapSeparate() {
        ExecutionStatistics counts = ExecutionStatistics.fromStates(Arrays.asList(
                WAIT_ASSEMBLY, WAIT_INSPECTION, WAIT_DISPOSITION, REWORK_IN_PROGRESS,
                WAIT_REINSPECTION, WAIT_PACKING, PACKED, PACKED, SCRAPPED));
        assertEquals(9, counts.getAllocated());
        assertEquals(8, counts.getAssembled());
        assertEquals(3, counts.getQualified());
        assertEquals(1, counts.getPackable());
        assertEquals(2, counts.getPacked());
        assertEquals(1, counts.getScrapped());
        assertEquals(1, counts.getPendingDisposition());
        assertEquals(1, counts.getReworkInProgress());
        assertFalse(counts.allTerminal());
        assertTrue(ExecutionStatistics.fromStates(Arrays.asList(PACKED, SCRAPPED)).allTerminal());
        assertFalse(ExecutionStatistics.fromStates(java.util.Collections.emptyList()).allTerminal());
    }
}
