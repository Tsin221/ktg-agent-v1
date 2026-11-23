package com.ktg.mes.pro.execution;

import com.ktg.common.core.domain.AjaxResult;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

class ProExecutionReleaseControllerTest {
 @Test void malformedAndSpoofedBodiesNeverInvokeBusinessService() throws Exception {
  ExecutionReleaseService service=mock(ExecutionReleaseService.class);
  MockMvc mvc=MockMvcBuilders.standaloneSetup(new ExecutionOrderController(service)).build();
  for(String body:new String[]{"{\"actorId\":2}","{\"expectedVersion\":1.2}","{\"steps\":[{\"workstationId\":1.2}]}"}) {
   mvc.perform(post("/mes/pro/execution/workorders/19/releases").contentType(MediaType.APPLICATION_JSON).content(body))
    .andExpect(jsonPath("$.code").value(400)).andExpect(jsonPath("$.errorCode").value("VALIDATION_ERROR"));
  }
  verifyZeroInteractions(service);
 }
 @Test void objectScopeRejectionsRemainStructuredAndNeverBecomeSuccess() throws Exception {
  ExecutionReleaseService service=mock(ExecutionReleaseService.class);
  when(service.options(19L)).thenThrow(ExecutionException.forbidden());
  MockMvc mvc=MockMvcBuilders.standaloneSetup(new ExecutionOrderController(service))
    .setControllerAdvice(new ExecutionExceptionAdvice()).build();
  mvc.perform(get("/mes/pro/execution/workorders/19/release-options"))
   .andExpect(jsonPath("$.code").value(403)).andExpect(jsonPath("$.errorCode").value("FORBIDDEN"));
 }
}
