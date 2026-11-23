package com.ktg.mes.pro.execution;
import org.junit.jupiter.api.Test;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import com.ktg.common.core.domain.AjaxResult;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;
import static org.junit.jupiter.api.Assertions.*;
class ProExecutionPackingControllerTest {
 @Test void strictDtoRejectsActorAndFractionalIds() throws Exception {
  ObjectMapper json=new ObjectMapper();
  assertThrows(Exception.class,()->json.readValue("{\"actorId\":1}",ExecutionPackingRequest.class));
  assertThrows(Exception.class,()->json.readValue("{\"snIds\":[1.2]}",ExecutionPackingRequest.class));
  assertThrows(Exception.class,()->json.readValue("{\"packageLineId\":\"1\"}",ExecutionPackingRequest.class));
 }
 @Test void routesTaskIdAndReturnsReceipt() throws Exception {
  ExecutionPackingService service=mock(ExecutionPackingService.class);
  when(service.pack(eq(7L),any())).thenReturn(AjaxResult.success("packed"));
  MockMvc web=MockMvcBuilders.standaloneSetup(new ExecutionPackingController(service)).build();
  web.perform(post("/mes/pro/execution/tasks/7/packages").contentType("application/json").content("{\"operationId\":\"test\",\"snIds\":[1]}" )).andExpect(status().isOk()).andExpect(jsonPath("$.code").value(200));
  verify(service).pack(eq(7L),any());
 }
 @Test void validatesBatchBeforeAnyDatabaseAction() {
  ExecutionPackingService service=new ExecutionPackingService(mock(javax.sql.DataSource.class),mock(ExecutionOperationService.class),mock(ExecutionOwnershipService.class),new ObjectMapper());
  ExecutionPackingRequest r=new ExecutionPackingRequest();r.snIds=java.util.Arrays.asList(1L,1L);
  assertThrows(ExecutionException.class,()->service.pack(1L,r));
  r.snIds=java.util.Collections.emptyList();assertThrows(ExecutionException.class,()->service.pack(1L,r));
 }
}
