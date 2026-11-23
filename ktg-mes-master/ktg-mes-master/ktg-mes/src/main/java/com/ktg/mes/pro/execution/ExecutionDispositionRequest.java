package com.ktg.mes.pro.execution;
import com.fasterxml.jackson.annotation.JsonAnySetter;
public class ExecutionDispositionRequest  {
    public String operationId,decision,reason;
    public Long inspectionId;
    @JsonAnySetter public void unknown(String name,Object value) {
        throw ExecutionException.validation("未知处置字段: "+name);
    }
    public void validate() {
        if(inspectionId==null||inspectionId<=0||!("REWORK".equals(decision)||"SCRAP".equals(decision))||reason==null||reason.trim().isEmpty()||reason.length()>500)throw ExecutionException.validation("失败检验、决定和处置原因必填");
        reason=reason.trim();
    }
}
