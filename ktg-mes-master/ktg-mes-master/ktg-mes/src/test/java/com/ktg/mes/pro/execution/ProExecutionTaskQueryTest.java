package com.ktg.mes.pro.execution;

import org.junit.jupiter.api.Test;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;

class ProExecutionTaskQueryTest {
    @Test void onlyEligibleStatesForStepAndNoFallbackForUnknownStep() {
        assertEquals(Collections.singletonList("WAIT_ASSEMBLY"), ExecutionTaskQueryService.eligibleStates("NORMAL", "ASSEMBLY"));
        assertEquals(Arrays.asList("WAIT_INSPECTION", "WAIT_REINSPECTION"), ExecutionTaskQueryService.eligibleStates("NORMAL", "INSPECTION"));
        assertEquals(Collections.singletonList("WAIT_PACKING"), ExecutionTaskQueryService.eligibleStates("NORMAL", "PACKING"));
        assertEquals(Collections.singletonList("REWORK_IN_PROGRESS"), ExecutionTaskQueryService.eligibleStates("REWORK", "ASSEMBLY"));
        assertTrue(ExecutionTaskQueryService.eligibleStates("NORMAL", "UNKNOWN").isEmpty());
        assertTrue(ExecutionTaskQueryService.eligibleStates("OTHER", "ASSEMBLY").isEmpty());
    }
    @Test void paginationBoundsPreventInvalidQuery() {
        assertThrows(ExecutionException.class, () -> ExecutionTaskQueryService.checkPage(0, 10));
        assertThrows(ExecutionException.class, () -> ExecutionTaskQueryService.checkPage(1, 0));
        assertThrows(ExecutionException.class, () -> ExecutionTaskQueryService.checkPage(1, 201));
        assertDoesNotThrow(() -> ExecutionTaskQueryService.checkPage(1, 200));
    }
}
