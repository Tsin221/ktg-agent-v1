package com.ktg.mes.pro.execution;

import com.fasterxml.jackson.annotation.JsonAnySetter;

public class ExecutionTaskStartRequest {
    public String operationId;

    @JsonAnySetter public void unknown(String name, Object value) {
        throw ExecutionException.validation("未知开工字段: " + name);
    }
}
