package com.ktg.mes.pro.execution;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import java.math.BigDecimal;
import static org.junit.jupiter.api.Assertions.*;
class ProExecutionInspectionRecordTest  {
    final ObjectMapper json=new ObjectMapper();
    @Test void measuresThresholdsWithoutTrustingSubmittedConclusion() throws Exception  {
        com.fasterxml.jackson.databind.JsonNode n=json.readTree("{\"index_type\":\"QUANTITATIVE\",\"threshold_min\":9,\"threshold_max\":11}");
        ExecutionInspectionRequest.IndexResult r=new ExecutionInspectionRequest.IndexResult();
        r.value=new BigDecimal("9");
        assertTrue(ExecutionInspectionRecordService.passes(n,r));
        r.value=new BigDecimal("11.001");
        assertFalse(ExecutionInspectionRecordService.passes(n,r));
        r.passed=true;
        assertThrows(ExecutionException.class,()->ExecutionInspectionRecordService.passes(n,r));
    }
    @Test void rejectsUnknownFinalResultAndMissingQualitativeResult() throws Exception  {
        assertThrows(Exception.class,()->json.readValue("{\"checkResult\":\"ACCEPT\"}",ExecutionInspectionRequest.class));
        assertThrows(ExecutionException.class,()->ExecutionInspectionRecordService.passes(json.readTree("{\"index_type\":\"QUALITATIVE\"}"),new ExecutionInspectionRequest.IndexResult()));
    }
}
