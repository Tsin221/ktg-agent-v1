package com.ktg.mes.pro.execution;
import com.fasterxml.jackson.annotation.JsonAnySetter;
public class ExecutionCompletionRequest {
    public String operationId;
    @com.fasterxml.jackson.databind.annotation.JsonDeserialize(using=ExecutionReleaseRequest.StrictLong.class)
    public Long version;
    @JsonAnySetter public void unknown(String name,Object value) {throw ExecutionException.validation("未知完结字段: "+name);}
}
