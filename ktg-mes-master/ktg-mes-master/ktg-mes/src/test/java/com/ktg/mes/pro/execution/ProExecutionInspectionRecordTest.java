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
    @Test void malformedThresholdIsNotSilentlyTreatedAsZero() throws Exception {
        com.fasterxml.jackson.databind.JsonNode n=json.readTree("{\"index_id\":1,\"index_code\":\"I\",\"index_name\":\"I\",\"index_type\":\"QUANTITATIVE\",\"threshold_min\":\"invalid\",\"threshold_max\":11}");
        ExecutionInspectionRequest.IndexResult r=new ExecutionInspectionRequest.IndexResult();
        r.value=BigDecimal.ZERO;
        assertEquals(409,assertThrows(ExecutionException.class,()->ExecutionInspectionRecordService.passes(n,r)).getCode());
        com.fasterxml.jackson.databind.node.ObjectNode snapshot=json.createObjectNode();snapshot.putArray("indices").add(n);
        assertEquals(409,assertThrows(ExecutionException.class,()->ExecutionInspectionRecordService.validateForFreeze(snapshot)).getCode());
    }
    @Test void historicalOptionalDisplayFieldsAreNotNewInspectionPrerequisites() throws Exception {
        ExecutionInspectionRecordService records=new ExecutionInspectionRecordService(org.mockito.Mockito.mock(javax.sql.DataSource.class),json);
        com.fasterxml.jackson.databind.JsonNode snapshot=records.snapshot("{\"schemaVersion\":1,\"indices\":[{\"index_id\":1,\"index_type\":\"QUALITATIVE\"}]}");
        ExecutionInspectionRequest r=new ExecutionInspectionRequest();r.taskId=1L;
        ExecutionInspectionRequest.IndexResult result=new ExecutionInspectionRequest.IndexResult();result.indexId=1L;result.passed=true;
        r.indices=java.util.Collections.singletonList(result);
        assertTrue(records.validate(snapshot,r,false));
        assertThrows(ExecutionException.class,()->ExecutionInspectionRecordService.validateForFreeze(snapshot));
    }
}
