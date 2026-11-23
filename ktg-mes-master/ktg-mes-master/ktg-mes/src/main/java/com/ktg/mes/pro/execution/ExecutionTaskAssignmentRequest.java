package com.ktg.mes.pro.execution;

import com.fasterxml.jackson.annotation.JsonAnySetter;
import com.fasterxml.jackson.databind.annotation.JsonDeserialize;

/** Coordinator chooses an executor; the actor and workstation are not writable. */
public class ExecutionTaskAssignmentRequest {
    public String operationId;
    @JsonDeserialize(using = ExecutionReleaseRequest.StrictLong.class)
    public Long assignedUserId;

    @JsonAnySetter public void unknown(String name, Object value) {
        throw ExecutionException.validation("未知分派字段: " + name);
    }
}
