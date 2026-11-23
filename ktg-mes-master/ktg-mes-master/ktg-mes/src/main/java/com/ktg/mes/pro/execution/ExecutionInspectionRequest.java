package com.ktg.mes.pro.execution;
import com.fasterxml.jackson.annotation.JsonAnySetter;
import java.math.BigDecimal;
import java.util.List;
public class ExecutionInspectionRequest  {
    public String operationId;
    @com.fasterxml.jackson.databind.annotation.JsonDeserialize(using=ExecutionReleaseRequest.StrictLong.class) public Long taskId;
    public List<IndexResult> indices;
    @JsonAnySetter public void unknown(String k,Object v) {
        throw ExecutionException.validation("未知检验字段: "+k);
    }
    public static class IndexResult  {
        @com.fasterxml.jackson.databind.annotation.JsonDeserialize(using=ExecutionReleaseRequest.StrictLong.class) public Long indexId;
        public BigDecimal value;
        public Boolean passed;
        @com.fasterxml.jackson.databind.annotation.JsonDeserialize(using=ExecutionReleaseRequest.StrictLong.class) public Long defectId;
        public String reason;
        @JsonAnySetter public void unknown(String k,Object v) {
            throw ExecutionException.validation("未知指标字段: "+k);
        }
    }
}
