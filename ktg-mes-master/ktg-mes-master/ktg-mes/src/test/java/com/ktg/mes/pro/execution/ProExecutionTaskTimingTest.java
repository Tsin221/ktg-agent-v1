package com.ktg.mes.pro.execution;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import java.util.HashMap;
import java.util.Map;
import static org.junit.jupiter.api.Assertions.*;

class ProExecutionTaskTimingTest {
    @Test void startRequiresReadyReleasedTaskAndCurrentUnits() {
        Map<String,Object> row = new HashMap<>(); row.put("order_status", "CONFIRMED"); row.put("status", "READY");
        assertTrue(ExecutionTaskStartService.startable(row, 1));
        assertFalse(ExecutionTaskStartService.startable(row, 0));
        row.put("status", "WAITING"); assertFalse(ExecutionTaskStartService.startable(row, 1));
        row.put("status", "STARTED"); assertFalse(ExecutionTaskStartService.startable(row, 1));
        row.put("status", "READY"); row.put("actual_started_at", new java.sql.Timestamp(1)); assertFalse(ExecutionTaskStartService.startable(row, 1));
        row.put("actual_started_at", null); row.put("order_status", "PREPARE"); assertFalse(ExecutionTaskStartService.startable(row, 1));
    }
    @Test void clientCannotChooseTimeOrActor() {
        ObjectMapper json=new ObjectMapper();
        assertThrows(Exception.class, () -> json.readValue("{\"startedBy\":3}", ExecutionTaskStartRequest.class));
        assertThrows(Exception.class, () -> json.readValue("{\"actualStartedAt\":\"2020-01-01\"}", ExecutionTaskStartRequest.class));
    }
}
