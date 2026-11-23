package com.ktg.mes.pro.execution;
import org.junit.jupiter.api.Test;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
class ProExecutionCompletionControllerTest {
 @Test void versionRejectsTextAndFractionsBeforeServiceCall() throws Exception {
  ExecutionCompletionService service=mock(ExecutionCompletionService.class);
  MockMvc mvc=MockMvcBuilders.standaloneSetup(new ExecutionOrderController(mock(ExecutionReleaseService.class),mock(ExecutionOrderProgressService.class),service)).build();
  for(String version:new String[]{"1.5","\"1\""})mvc.perform(post("/mes/pro/execution/workorders/1/completions").contentType("application/json").content("{\"operationId\":\"a3343ea8-30ae-4d7a-8689-eb389869c573\",\"version\":"+version+"}")).andExpect(jsonPath("$.code").value(400)).andExpect(jsonPath("$.errorCode").value("VALIDATION_ERROR"));
  verifyNoInteractions(service);
 }
}
