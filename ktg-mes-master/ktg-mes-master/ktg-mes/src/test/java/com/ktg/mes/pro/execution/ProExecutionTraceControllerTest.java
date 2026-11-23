package com.ktg.mes.pro.execution;

import org.junit.jupiter.api.Test;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import com.ktg.common.core.page.TableDataInfo;
import java.util.*;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;

class ProExecutionTraceControllerTest {
    private final ExecutionTraceService service=mock(ExecutionTraceService.class);
    private final MockMvc mvc=MockMvcBuilders.standaloneSetup(new ExecutionTraceController(service))
            .setControllerAdvice(new ExecutionExceptionAdvice()).build();

    @Test void malformedOrMissingArgumentsAreRejectedBeforeService() throws Exception {
        mvc.perform(get("/mes/pro/execution/units/trace").param("snCode","  "))
                .andExpect(jsonPath("$.code").value(400));
        for (String path : Arrays.asList("/units/trace","/materials/trace?itemId=0&batchCode=B07",
                "/materials/trace?itemId=1.5&batchCode=B07","/materials/trace?itemId=1&batchCode=B07&pageSize=201",
                "/materials/trace?itemId=1&batchCode=B07&pageNum=0","/materials/trace?itemId=1&batchCode=")) {
            mvc.perform(get("/mes/pro/execution"+path)).andExpect(jsonPath("$.code").value(400))
                    .andExpect(jsonPath("$.errorCode").value("VALIDATION_ERROR"));
        }
        verifyNoInteractions(service);
    }

    @Test void knownLegacyAndUnknownAreDifferentAndPermissionErrorsRemainExplicit() throws Exception {
        when(service.unit("old")).thenReturn(Collections.singletonMap("traceStatus","LEGACY_NO_RECORD"));
        when(service.unit("unknown")).thenThrow(ExecutionException.notFound());
        when(service.unit("denied")).thenThrow(ExecutionException.forbidden());
        mvc.perform(get("/mes/pro/execution/units/trace").param("snCode","old"))
                .andExpect(jsonPath("$.data.traceStatus").value("LEGACY_NO_RECORD"));
        mvc.perform(get("/mes/pro/execution/units/trace").param("snCode","unknown"))
                .andExpect(jsonPath("$.code").value(404)).andExpect(jsonPath("$.errorCode").value("NOT_FOUND"));
        mvc.perform(get("/mes/pro/execution/units/trace").param("snCode","denied"))
                .andExpect(jsonPath("$.code").value(403));
    }

    @Test void largeIdentifiersRemainExactDecimalStringsWhileQuantityStaysNumeric() throws Exception {
        long largeId=9007199254740993L;
        Map<String,Object> row=new LinkedHashMap<>();
        row.put("snId",largeId);row.put("inputCount",2);
        Map<String,Object> input=new LinkedHashMap<>();
        input.put("consumeLineId",largeId);input.put("usedBy",largeId);input.put("quantity",1.25);
        Map<String,Object> detail=new LinkedHashMap<>(row);
        detail.put("inputs",Collections.singletonList(input));
        when(service.unit("large")).thenReturn(detail);
        mvc.perform(get("/mes/pro/execution/units/trace").param("snCode","large"))
                .andExpect(jsonPath("$.data.snId").value("9007199254740993"))
                .andExpect(jsonPath("$.data.inputs[0].consumeLineId").value("9007199254740993"))
                .andExpect(jsonPath("$.data.inputs[0].usedBy").value("9007199254740993"))
                .andExpect(jsonPath("$.data.inputs[0].quantity").value(1.25));
        TableDataInfo page=new TableDataInfo(Collections.singletonList(row),1);page.setCode(200);
        when(service.material(largeId,"B07",1,20)).thenReturn(page);
        mvc.perform(get("/mes/pro/execution/materials/trace").param("itemId",Long.toString(largeId)).param("batchCode","B07"))
                .andExpect(jsonPath("$.rows[0].snId").value("9007199254740993"))
                .andExpect(jsonPath("$.rows[0].inputCount").value(2))
                .andExpect(jsonPath("$.total").value(1));
    }

    @Test void nonblankIdentifierWhitespaceIsNotDiscarded() throws Exception {
        when(service.unit(" SN")).thenReturn(Collections.singletonMap("traceStatus","LEGACY_NO_RECORD"));
        mvc.perform(get("/mes/pro/execution/units/trace").param("snCode"," SN"))
                .andExpect(jsonPath("$.code").value(200));
        verify(service).unit(" SN");
        TableDataInfo page=new TableDataInfo(Collections.emptyList(),0);page.setCode(200);
        when(service.material(1L," B07 ",1,20)).thenReturn(page);
        mvc.perform(get("/mes/pro/execution/materials/trace").param("itemId","1").param("batchCode"," B07 "))
                .andExpect(jsonPath("$.code").value(200));
        verify(service).material(1L," B07 ",1,20);
    }
}
