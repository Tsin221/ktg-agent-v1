package com.ktg.mes.pro.execution;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import java.util.HashMap;
import java.util.Map;
import static org.junit.jupiter.api.Assertions.*;

class ProExecutionAssignmentTest {
    @Test void rejectsUnknownIdentityAndFractionalUserIds() throws Exception {
        ObjectMapper json = new ObjectMapper();
        assertThrows(Exception.class, () -> json.readValue("{\"actorId\":2}", ExecutionTaskAssignmentRequest.class));
        assertThrows(Exception.class, () -> json.readValue("{\"assignedUserId\":2.5}", ExecutionTaskAssignmentRequest.class));
        assertEquals(2L, json.readValue("{\"assignedUserId\":2}", ExecutionTaskAssignmentRequest.class).assignedUserId.longValue());
    }

    @Test void onlyUnstartedTasksAreAssignable() {
        Map<String,Object> row=new HashMap<>(); row.put("status", "WAITING"); row.put("order_status", "CONFIRMED");
        assertTrue(ExecutionAssignmentService.assignable(row));
        row.put("status", "READY"); assertTrue(ExecutionAssignmentService.assignable(row));
        row.put("actual_started_at", new java.sql.Timestamp(1)); assertFalse(ExecutionAssignmentService.assignable(row));
        row.put("actual_started_at", null); row.put("status", "STARTED"); assertFalse(ExecutionAssignmentService.assignable(row));
        row.put("status", "FINISHED"); assertFalse(ExecutionAssignmentService.assignable(row));
        row.put("status", "READY"); row.put("order_status", "PREPARE"); assertFalse(ExecutionAssignmentService.assignable(row));
    }
}
