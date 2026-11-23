package com.ktg.mes.pro.execution;
import com.fasterxml.jackson.annotation.JsonAnySetter;
import java.util.List;
public class ExecutionPackingRequest {
    public String operationId;
    @com.fasterxml.jackson.databind.annotation.JsonDeserialize(contentUsing=ExecutionReleaseRequest.StrictLong.class)
    public List<Long> snIds;
    @com.fasterxml.jackson.databind.annotation.JsonDeserialize(using=ExecutionReleaseRequest.StrictLong.class)
    public Long packageLineId;
    @JsonAnySetter public void unknown(String key,Object value) {throw ExecutionException.validation("未知包装字段: "+key);}
}
