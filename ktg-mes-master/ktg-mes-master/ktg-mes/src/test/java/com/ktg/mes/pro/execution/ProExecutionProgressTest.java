package com.ktg.mes.pro.execution;
import org.junit.jupiter.api.Test;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;
class ProExecutionProgressTest {
 @Test void currentCountsDoNotSumTasksOrReinspection() {
  ExecutionStatistics s=ExecutionStatistics.fromStates(Arrays.asList(ExecutionState.PACKED,ExecutionState.WAIT_PACKING,ExecutionState.WAIT_DISPOSITION,ExecutionState.REWORK_IN_PROGRESS,ExecutionState.WAIT_REINSPECTION,ExecutionState.WAIT_INSPECTION,ExecutionState.WAIT_ASSEMBLY,ExecutionState.SCRAPPED));
  assertEquals(8,s.getAllocated());assertEquals(7,s.getAssembled());assertEquals(2,s.getQualified());assertEquals(1,s.getPackable());assertFalse(s.allTerminal());
  assertEquals(s.getAllocated(),s.getWaitingAssembly()+s.getWaitingInspection()+s.getPendingDisposition()+s.getReworkInProgress()+s.getWaitingReinspection()+s.getPackable()+s.getPacked()+s.getScrapped());
 }
}
