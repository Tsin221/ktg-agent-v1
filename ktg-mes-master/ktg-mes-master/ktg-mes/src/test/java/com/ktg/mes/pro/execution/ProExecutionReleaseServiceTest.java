package com.ktg.mes.pro.execution;
import org.junit.jupiter.api.Test;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;
class ProExecutionReleaseServiceTest {
 @Test void requiresCompleteUniqueChoicesAndVersion() {
  ExecutionReleaseRequest r=new ExecutionReleaseRequest();assertThrows(ExecutionException.class,()->ExecutionReleaseService.validateRequest(r));
  r.routeId=1L;r.expectedVersion=0L;r.steps=new ArrayList<>();
  for(long i=1;i<=3;i++){ExecutionReleaseRequest.Step s=new ExecutionReleaseRequest.Step();s.routeProcessId=i;s.workstationId=i;r.steps.add(s);}
  assertDoesNotThrow(()->ExecutionReleaseService.validateRequest(r));
  r.steps.get(2).routeProcessId=1L;assertThrows(ExecutionException.class,()->ExecutionReleaseService.validateRequest(r));
 }
 @Test void rejectsInvalidTypesRepeatedProcessAndTiedOrder() {
  List<Map<String,Object>> s=new ArrayList<>();String[] k={"ASSEMBLY","INSPECTION","PACKING"};
  for(int i=0;i<3;i++){Map<String,Object> row=new HashMap<>();row.put("process_id",i+1);row.put("order_num",i+1);row.put("execution_kind",k[i]);s.add(row);}
  assertDoesNotThrow(()->ExecutionReleaseService.validateRoute(s));
  s.get(1).put("order_num",1);assertThrows(ExecutionException.class,()->ExecutionReleaseService.validateRoute(s));
  s.get(1).put("order_num",2);s.get(1).put("process_id",1);assertThrows(ExecutionException.class,()->ExecutionReleaseService.validateRoute(s));
  s.get(1).put("process_id",2);s.get(1).put("execution_kind",null);assertThrows(ExecutionException.class,()->ExecutionReleaseService.validateRoute(s));
 }
 @Test void strictJsonRejectsCallerIdentityAndFractionalIds() throws Exception {
  ObjectMapper j=new ObjectMapper();
  assertThrows(Exception.class,()->j.readValue("{\"actorId\":1}",ExecutionReleaseRequest.class));
  assertThrows(Exception.class,()->j.readValue("{\"expectedVersion\":0.9}",ExecutionReleaseRequest.class));
  assertThrows(Exception.class,()->j.readValue("{\"steps\":[{\"workstationId\":1.9}]}",ExecutionReleaseRequest.class));
  assertEquals(0L,j.readValue("{\"expectedVersion\":0}",ExecutionReleaseRequest.class).expectedVersion.longValue());
 }
}
