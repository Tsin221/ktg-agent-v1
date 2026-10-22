package com.ktg.mes.pro.controller;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.ktg.common.core.domain.AjaxResult;
import com.ktg.common.exception.ServiceException;
import com.ktg.framework.web.exception.GlobalExceptionHandler;
import com.ktg.mes.pro.controller.dto.ProWorkorderDraftRequest;
import com.ktg.mes.pro.domain.ProWorkorderOperation;
import com.ktg.mes.pro.service.impl.ProWorkorderOperationService;
import com.ktg.framework.web.service.PermissionService;
import com.ktg.mes.pro.domain.ProWorkorder;
import com.ktg.mes.pro.service.IProWorkorderService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.Spy;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.dao.DataAccessResourceFailureException;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import org.springframework.validation.beanvalidation.LocalValidatorFactoryBean;

import java.util.Collections;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.argThat;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.mockito.Mockito.doReturn;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@ExtendWith(MockitoExtension.class)
class ProWorkorderControllerTest {

    @Mock
    private IProWorkorderService proWorkorderService;

    @Mock
    private ProWorkorderOperationService operationService;

    @Mock
    private PermissionService permissionService;

    @Spy
    @InjectMocks
    private ProWorkorderController controller;

    private MockMvc mockMvc;

    @BeforeEach
    void setUp() {
        LocalValidatorFactoryBean validator = new LocalValidatorFactoryBean();
        validator.afterPropertiesSet();
        mockMvc = MockMvcBuilders.standaloneSetup(controller)
                .setControllerAdvice(new GlobalExceptionHandler())
                .setValidator(validator)
                .build();
    }

    @Test
    void operationRouteRejectsMissingActionPermissionBeforeWriting() throws Exception {
        mockMvc.perform(post("/mes/pro/workorder/operations")
                        .contentType("application/json")
                        .content("{\"operationId\":\"8eec45ac-f6ea-4d07-8758-bec40a772dde\",\"action\":\"CREATE_DRAFT\"}"))
                .andExpect(status().isForbidden())
                .andExpect(jsonPath("$.errorCode").value("PERMISSION_DENIED"));
        verify(operationService, never()).execute(any(), any(), any());
    }

    @Test
    void operationRouteUsesCreatePermissionAndReturnsServiceResult() throws Exception {
        doReturn(7L).when(controller).getUserId();
        doReturn("alice").when(controller).getUsername();
        when(permissionService.hasPermi("mes:pro:workorder:add")).thenReturn(true);
        when(operationService.execute(any(), any(), any())).thenReturn(AjaxResult.success());
        mockMvc.perform(post("/mes/pro/workorder/operations")
                        .contentType("application/json")
                        .content("{\"operationId\":\"8eec45ac-f6ea-4d07-8758-bec40a772dde\",\"action\":\"CREATE_DRAFT\"}"))
                .andExpect(status().isOk());
        verify(operationService).execute(any(), org.mockito.ArgumentMatchers.eq(7L),
                org.mockito.ArgumentMatchers.eq("alice"));
    }

    @Test
    void operationResultHidesReceiptWhenActionPermissionWasRevoked() throws Exception {
        doReturn(7L).when(controller).getUserId();
        ProWorkorderOperation receipt = new ProWorkorderOperation();
        receipt.setAction("EDIT_DRAFT");
        when(operationService.result("8eec45ac-f6ea-4d07-8758-bec40a772dde", 7L))
                .thenReturn(AjaxResult.success(receipt));
        mockMvc.perform(get("/mes/pro/workorder/operations/8eec45ac-f6ea-4d07-8758-bec40a772dde"))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.errorCode").value("OPERATION_NOT_FOUND"));
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

    @Test
    void addReturnsTheCreatedIdFromTheTransactionalService() throws Exception
    {
        doReturn("tester").when(controller).getUsername();
        when(proWorkorderService.createDraftWorkorder(argThat(workorder ->
                "WO-001".equals(workorder.getWorkorderCode())
                        && "tester".equals(workorder.getCreateBy())))).thenReturn(77L);

        mockMvc.perform(post("/mes/pro/workorder")
                        .contentType("application/json")
                        .content(validDraftJson()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(200))
                .andExpect(jsonPath("$.data").value(77));
    }

    @Test
    void addDoesNotExposeDatabaseDetailsWhenBomWriteFails() throws Exception
    {
        doReturn("tester").when(controller).getUsername();
        when(proWorkorderService.createDraftWorkorder(any()))
                .thenThrow(new DataAccessResourceFailureException(
                        "SQL: insert into pro_workorder_bom; jar:file:/internal/ktg-mes.jar"));

        mockMvc.perform(post("/mes/pro/workorder")
                        .contentType("application/json")
                        .content(validDraftJson()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(500))
                .andExpect(jsonPath("$.msg").value("数据库操作失败，请稍后重试"));
    }

    @Test
    void editRejectsNonDraftWorkorderInsteadOfUsingGenericUpdateAsAStatusAction() throws Exception {
        doReturn("tester").when(controller).getUsername();
        when(proWorkorderService.updateDraftWorkorder(argThat(workorder ->
                Long.valueOf(7L).equals(workorder.getWorkorderId()))))
                .thenThrow(new ServiceException("只能编辑草稿状态生产工单！", 409));

        mockMvc.perform(put("/mes/pro/workorder")
                        .contentType("application/json")
                        .content(validDraftJson().replace("{", "{\"workorderId\":7,\"version\":3,\"status\":\"FINISHED\",")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(409))
                .andExpect(jsonPath("$.msg").value("只能编辑草稿状态生产工单！"));

        verify(proWorkorderService).updateDraftWorkorder(argThat(workorder ->
                Long.valueOf(7L).equals(workorder.getWorkorderId())
                        && Long.valueOf(3L).equals(workorder.getVersion())
                        && "tester".equals(workorder.getUpdateBy())));
    }

    @Test
    void addRejectsUnsupportedWorkorderTypeAtTheApiBoundary() throws Exception {
        mockMvc.perform(post("/mes/pro/workorder")
                        .contentType("application/json")
                        .content(validDraftJson().replace("SELF", "INVALID")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(500))
                .andExpect(jsonPath("$.msg").value("workorderType: 工单类型必须是 SELF、OUTSOURCE 或 PURCHASE"));

        verify(proWorkorderService, never()).createDraftWorkorder(argThat(workorder -> true));
    }

    @Test
    void addRejectsMissingProductBeforeCallingTheService() throws Exception {
        mockMvc.perform(post("/mes/pro/workorder")
                        .contentType("application/json")
                        .content(validDraftJson().replace("\"productId\":10,", "")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(500))
                .andExpect(jsonPath("$.msg").value("productId: 产品不能为空"));

        verify(proWorkorderService, never()).createDraftWorkorder(argThat(workorder -> true));
    }

    @Test
    void addRejectsNonPositiveQuantityBeforeCallingTheService() throws Exception {
        mockMvc.perform(post("/mes/pro/workorder")
                        .contentType("application/json")
                        .content(validDraftJson().replace("\"quantity\":5", "\"quantity\":0")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(500))
                .andExpect(jsonPath("$.msg").value("quantity: 生产数量必须大于0"));

        verify(proWorkorderService, never()).createDraftWorkorder(argThat(workorder -> true));
    }

    @Test
    void addRejectsOverlongWorkorderCodeBeforeCallingTheService() throws Exception {
        String longCode = String.join("", Collections.nCopies(65, "A"));

        mockMvc.perform(post("/mes/pro/workorder")
                        .contentType("application/json")
                        .content(validDraftJson().replace("WO-001", longCode)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(500))
                .andExpect(jsonPath("$.msg").value("workorderCode: 工单编号长度不能超过64个字符"));

        verify(proWorkorderService, never()).createDraftWorkorder(argThat(workorder -> true));
    }

    @Test
    void addRejectsInvalidBusinessDateBeforeCallingTheService() throws Exception {
        mockMvc.perform(post("/mes/pro/workorder")
                        .contentType("application/json")
                        .content(validDraftJson().replace("2026-10-10", "not-a-date")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(500))
                .andExpect(jsonPath("$.msg").value("requestDate: 需求日期格式必须为yyyy-MM-dd"));

        verify(proWorkorderService, never()).createDraftWorkorder(argThat(workorder -> true));
    }

    @Test
    void draftRequestIgnoresServerManagedAndProductSnapshotFields() throws Exception {
        ProWorkorderDraftRequest request = new ObjectMapper().readValue(
                validDraftJson().replace("}",
                        ",\"status\":\"FINISHED\",\"quantityProduced\":99,"
                                + "\"createBy\":\"attacker\",\"productCode\":\"FAKE\","
                                + "\"productName\":\"FAKE\",\"unitOfMeasure\":\"FAKE\"}"),
                ProWorkorderDraftRequest.class);

        ProWorkorder workorder = request.toWorkorder();

        org.junit.jupiter.api.Assertions.assertNull(workorder.getStatus());
        org.junit.jupiter.api.Assertions.assertNull(workorder.getQuantityProduced());
        org.junit.jupiter.api.Assertions.assertNull(workorder.getCreateBy());
        org.junit.jupiter.api.Assertions.assertNull(workorder.getProductCode());
        org.junit.jupiter.api.Assertions.assertNull(workorder.getProductName());
        org.junit.jupiter.api.Assertions.assertNull(workorder.getUnitOfMeasure());
    }

    @Test
    void draftRequestCarriesVersionButStillIgnoresForgedStatus() throws Exception
    {
        ProWorkorderDraftRequest request = new ObjectMapper().readValue(
                validDraftJson().replace("{", "{\"workorderId\":7,\"version\":3,\"status\":\"FINISHED\","),
                ProWorkorderDraftRequest.class);

        ProWorkorder workorder = request.toWorkorder();

        org.junit.jupiter.api.Assertions.assertEquals(3L, workorder.getVersion());
        org.junit.jupiter.api.Assertions.assertNull(workorder.getStatus());
    }

    @Test
    void editRejectsUnknownWorkorderBeforeWriting() throws Exception {
        doReturn("tester").when(controller).getUsername();
        when(proWorkorderService.updateDraftWorkorder(argThat(workorder ->
                Long.valueOf(404L).equals(workorder.getWorkorderId()))))
                .thenThrow(new ServiceException("生产工单不存在！", 404));

        mockMvc.perform(put("/mes/pro/workorder")
                        .contentType("application/json")
                        .content(validDraftJson().replace("{", "{\"workorderId\":404,")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(404))
                .andExpect(jsonPath("$.msg").value("生产工单不存在！"));

        verify(proWorkorderService).updateDraftWorkorder(argThat(workorder ->
                Long.valueOf(404L).equals(workorder.getWorkorderId())));
    }

    @Test
    void editRejectsRemarkOnlyRequestBeforeWriting() throws Exception {
        mockMvc.perform(put("/mes/pro/workorder")
                        .contentType("application/json")
                        .content("{\"workorderId\":7,\"remark\":\"只改备注\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(500));

        verify(proWorkorderService, never()).updateDraftWorkorder(argThat(workorder -> true));
    }

    @Test
    void removeDelegatesTheWholeBatchToTheTransactionalService() throws Exception
    {
        when(proWorkorderService.deleteProWorkorderByWorkorderIds(argThat(ids ->
                ids.length == 2 && Long.valueOf(1L).equals(ids[0]) && Long.valueOf(2L).equals(ids[1]))))
                .thenReturn(2);

        mockMvc.perform(delete("/mes/pro/workorder/1,2"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(200));

        verify(proWorkorderService).deleteProWorkorderByWorkorderIds(argThat(ids -> ids.length == 2));
        verify(proWorkorderService, never()).selectProWorkorderByWorkorderId(any());
    }

    @Test
    void addRejectsImpossibleCalendarDateBeforeCallingTheService() throws Exception {
        mockMvc.perform(post("/mes/pro/workorder")
                        .contentType("application/json")
                        .content(validDraftJson().replace("2026-10-10", "2026-02-30")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(500))
                .andExpect(jsonPath("$.msg").value("requestDate: 需求日期不是有效日期"));

        verify(proWorkorderService, never()).createDraftWorkorder(argThat(workorder -> true));
    }

    private String validDraftJson() {
        return "{\"workorderCode\":\"WO-001\",\"workorderName\":\"测试工单\","
                + "\"workorderType\":\"SELF\",\"orderSource\":\"MANUAL\","
                + "\"productId\":10,\"quantity\":5,\"requestDate\":\"2026-10-10\","
                + "\"parentId\":0}";
    }
}
