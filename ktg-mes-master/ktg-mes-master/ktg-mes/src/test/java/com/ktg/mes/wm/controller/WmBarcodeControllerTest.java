package com.ktg.mes.wm.controller;

import com.ktg.mes.wm.domain.WmBarcode;
import com.ktg.mes.wm.service.IWmBarcodeService;
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
class WmBarcodeControllerTest {

    @Mock
    private IWmBarcodeService wmBarcodeService;

    @InjectMocks
    private WmBarcodeController controller;

    private MockMvc mockMvc;

    @BeforeEach
    void setUp() {
        mockMvc = MockMvcBuilders.standaloneSetup(controller).build();
    }

    @Test
    void getBarcodeUrlReturnsBarcodeInsteadOfTreatingPathAsBarcodeId() throws Exception {
        WmBarcode barcode = new WmBarcode();
        barcode.setBarcodeId(8L);
        barcode.setBarcodeType("WORKORDER");
        barcode.setBussinessId(12L);
        barcode.setBussinessCode("WO-001");
        barcode.setBarcodeUrl("/profile/barcode/WO-001.png");

        when(wmBarcodeService.selectWmBarcodeList(argThat(query ->
                Long.valueOf(12L).equals(query.getBussinessId())
                        && "WO-001".equals(query.getBussinessCode())
                        && "WORKORDER".equals(query.getBarcodeType()))))
                .thenReturn(Collections.singletonList(barcode));

        mockMvc.perform(get("/mes/wm/barcode/getBarcodeUrl")
                        .param("bussinessId", "12")
                        .param("bussinessCode", "WO-001")
                        .param("barcodeType", "WORKORDER"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(200))
                .andExpect(jsonPath("$.data.barcodeId").value(8))
                .andExpect(jsonPath("$.data.barcodeUrl").value("/profile/barcode/WO-001.png"));
    }
}
