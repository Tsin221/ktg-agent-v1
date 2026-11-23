package com.ktg.mes.pro.execution;

import com.fasterxml.jackson.annotation.JsonAnySetter;
import com.fasterxml.jackson.databind.annotation.JsonDeserialize;
import java.util.List;

/** Only actual selection and per-piece consumption are accepted from the operator. */
public class ExecutionReportRequest {
    public String operationId;
    @JsonDeserialize(contentUsing = ExecutionReleaseRequest.StrictLong.class)
    public List<Long> snIds;
    public List<ExecutionInputGroup> inputs;

    @JsonAnySetter public void unknown(String name, Object value) {
        throw ExecutionException.validation("未知报工字段: " + name);
    }
}
