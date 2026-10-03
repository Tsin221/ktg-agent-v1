package com.ktg.mes.pro.controller;

import com.ktg.common.core.domain.AjaxResult;
import com.ktg.mes.pro.domain.ProWorkorder;
import com.ktg.mes.pro.service.IProWorkorderService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import java.util.Collections;

import static org.mockito.ArgumentMatchers.argThat;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@ExtendWith(MockitoExtension.class)
class ProWorkorderControllerTest {

    @Mock
    private IProWorkorderService proWorkorderService;

    @InjectMocks
    private ProWorkorderController controller;

    private MockMvc mockMvc;

    @BeforeEach
    void setUp() {
        mockMvc = MockMvcBuilders.standaloneSetup(controller).build();
    }

    @Test
    void getHomeListReturnsDashboardDataInsteadOfTreatingPathAsWorkorderId() throws Exception {
        when(proWorkorderService.getHomeList(argThat(workorder ->
                "CONFIRMED".equals(workorder.getStatus()))))
                .thenReturn(AjaxResult.success(Collections.emptyList()));

        mockMvc.perform(get("/mes/pro/workorder/getHomeList")
                        .param("status", "CONFIRMED"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(200))
                .andExpect(jsonPath("$.data").isArray());
    }
}
