package com.ktg.mes.pro.service.impl;

import com.ktg.common.core.domain.AjaxResult;
import com.ktg.common.exception.ServiceException;
import com.ktg.mes.md.domain.MdItem;
import com.ktg.mes.md.domain.MdProductBom;
import com.ktg.mes.md.mapper.MdProductBomMapper;
import com.ktg.mes.pro.controller.vo.ProRouteHomeVO;
import com.ktg.mes.pro.controller.vo.ProWorkorderHomeVO;
import com.ktg.mes.pro.domain.ProFeedback;
import com.ktg.mes.pro.domain.ProRouteProcess;
import com.ktg.mes.pro.domain.ProRouteProduct;
import com.ktg.mes.pro.domain.ProWorkorder;
import com.ktg.mes.pro.domain.ProWorkorderBom;
import com.ktg.mes.pro.mapper.ProWorkorderBomMapper;
import com.ktg.mes.pro.mapper.ProWorkorderMapper;
import com.ktg.mes.pro.service.IProFeedbackService;
import com.ktg.mes.pro.service.IProRouteProcessService;
import com.ktg.mes.pro.service.IProRouteProductService;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.ArgumentCaptor;
import org.mockito.junit.jupiter.MockitoExtension;

import java.math.BigDecimal;
import java.util.Collections;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.argThat;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.mockito.Mockito.doAnswer;

@ExtendWith(MockitoExtension.class)
class ProWorkorderServiceImplTest {

    @Mock
    private ProWorkorderMapper proWorkorderMapper;
    @Mock
    private IProRouteProductService proRouteProductService;
    @Mock
    private IProRouteProcessService proRouteProcessService;
    @Mock
    private IProFeedbackService proFeedbackService;
    @Mock
    private com.ktg.mes.md.service.IMdItemService mdItemService;
    @Mock
    private MdProductBomMapper mdProductBomMapper;
    @Mock
    private ProWorkorderBomMapper proWorkorderBomMapper;

    @InjectMocks
    private ProWorkorderServiceImpl service;

    @Test
    void createDraftReturnsGeneratedIdAndBuildsIndependentBomRows()
    {
        ProWorkorder workorder = draft(10L, "5");
        when(mdItemService.selectMdItemById(10L)).thenReturn(product(10L, "P-10", "产品十", "件"));
        when(proWorkorderMapper.checkWorkorderCodeUnique(workorder)).thenReturn(null);
        doAnswer(invocation -> {
            workorder.setWorkorderId(77L);
            return 1;
        }).when(proWorkorderMapper).insertProWorkorder(workorder);

        MdProductBom first = bom(101L, "M-101", "2");
        MdProductBom second = bom(102L, "M-102", "3");
        when(mdProductBomMapper.selectMdProductBomList(any()))
                .thenReturn(java.util.Arrays.asList(first, second));
        when(proWorkorderBomMapper.insertProWorkorderBom(any())).thenReturn(1);

        Long createdId = service.createDraftWorkorder(workorder);

        assertEquals(77L, createdId);
        ArgumentCaptor<ProWorkorderBom> rows = ArgumentCaptor.forClass(ProWorkorderBom.class);
        verify(proWorkorderBomMapper, org.mockito.Mockito.times(2)).insertProWorkorderBom(rows.capture());
        assertEquals(2, rows.getAllValues().size());
        assertEquals(101L, rows.getAllValues().get(0).getItemId());
        assertEquals(new BigDecimal("10"), rows.getAllValues().get(0).getQuantity());
        assertEquals(102L, rows.getAllValues().get(1).getItemId());
        assertEquals(new BigDecimal("15"), rows.getAllValues().get(1).getQuantity());
        org.junit.jupiter.api.Assertions.assertNotSame(rows.getAllValues().get(0), rows.getAllValues().get(1));
    }

    @Test
    void updateDraftDoesNotRebuildBomWhenProductAndQuantityAreUnchanged()
    {
        ProWorkorder existing = draft(10L, "5.0");
        existing.setWorkorderId(7L);
        existing.setStatus("PREPARE");
        ProWorkorder update = draft(10L, "5.00");
        update.setWorkorderId(7L);
        when(proWorkorderMapper.selectProWorkorderByWorkorderId(7L)).thenReturn(existing);
        when(proWorkorderMapper.checkWorkorderCodeUnique(update)).thenReturn(existing);
        when(mdItemService.selectMdItemById(10L)).thenReturn(product(10L, "P-10", "产品十", "件"));
        when(proWorkorderMapper.updateProWorkorder(update)).thenReturn(1);

        int result = service.updateDraftWorkorder(update);

        assertEquals(1, result);
        verify(proWorkorderBomMapper, never()).deleteProWorkorderBomByWorkorderId(any());
        verify(proWorkorderBomMapper, never()).insertProWorkorderBom(any());
    }

    @Test
    void updateDraftRebuildsBomWhenQuantityChanges()
    {
        ProWorkorder existing = draft(10L, "5");
        existing.setWorkorderId(7L);
        existing.setStatus("PREPARE");
        ProWorkorder update = draft(10L, "8");
        update.setWorkorderId(7L);
        when(proWorkorderMapper.selectProWorkorderByWorkorderId(7L)).thenReturn(existing);
        when(proWorkorderMapper.checkWorkorderCodeUnique(update)).thenReturn(existing);
        when(mdItemService.selectMdItemById(10L)).thenReturn(product(10L, "P-10", "产品十", "件"));
        when(proWorkorderMapper.updateProWorkorder(update)).thenReturn(1);
        when(mdProductBomMapper.selectMdProductBomList(any()))
                .thenReturn(Collections.singletonList(bom(101L, "M-101", "2")));
        when(proWorkorderBomMapper.insertProWorkorderBom(any())).thenReturn(1);

        int result = service.updateDraftWorkorder(update);

        assertEquals(1, result);
        verify(proWorkorderBomMapper).deleteProWorkorderBomByWorkorderId(7L);
        verify(proWorkorderBomMapper).insertProWorkorderBom(argThat(row ->
                Long.valueOf(7L).equals(row.getWorkorderId())
                        && new BigDecimal("16").compareTo(row.getQuantity()) == 0));
    }

    @Test
    void insertCreatesPrepareDraftAndDefaultsMissingWorkorderTypeToSelf()
    {
        ProWorkorder workorder = new ProWorkorder();
        workorder.setProductId(10L);
        workorder.setStatus("CONFIRMED");
        MdItem product = product(10L, "P-10", "产品十", "件");
        when(mdItemService.selectMdItemById(10L)).thenReturn(product);
        when(proWorkorderMapper.insertProWorkorder(workorder)).thenReturn(1);

        int result = service.insertProWorkorder(workorder);

        assertEquals(1, result);
        assertEquals("PREPARE", workorder.getStatus());
        assertEquals("SELF", workorder.getWorkorderType());
        assertEquals("P-10", workorder.getProductCode());
        assertEquals("产品十", workorder.getProductName());
        assertEquals("件", workorder.getUnitOfMeasure());
        verify(proWorkorderMapper).insertProWorkorder(workorder);
    }

    @Test
    void insertRejectsUnknownProductBeforeWriting() {
        ProWorkorder workorder = new ProWorkorder();
        workorder.setProductId(999L);
        when(mdItemService.selectMdItemById(999L)).thenReturn(null);

        ServiceException error = assertThrows(ServiceException.class,
                () -> service.insertProWorkorder(workorder));

        assertEquals("productId: 产品不存在", error.getMessage());
        verify(proWorkorderMapper, never()).insertProWorkorder(any());
    }

    @Test
    void insertRejectsUnknownParentBeforeWriting() {
        ProWorkorder workorder = new ProWorkorder();
        workorder.setProductId(10L);
        workorder.setParentId(99L);
        when(mdItemService.selectMdItemById(10L)).thenReturn(product(10L, "P-10", "产品十", "件"));
        when(proWorkorderMapper.selectProWorkorderByWorkorderId(99L)).thenReturn(null);

        ServiceException error = assertThrows(ServiceException.class,
                () -> service.insertProWorkorder(workorder));

        assertEquals("parentId: 父工单不存在", error.getMessage());
        verify(proWorkorderMapper, never()).insertProWorkorder(any());
    }

    @Test
    void updateDraftRejectsCircularParentBeforeWriting() {
        ProWorkorder workorder = new ProWorkorder();
        workorder.setWorkorderId(7L);
        workorder.setProductId(10L);
        workorder.setQuantity(BigDecimal.ONE);
        workorder.setParentId(9L);
        ProWorkorder parent = new ProWorkorder();
        parent.setWorkorderId(9L);
        parent.setAncestors("0,7");
        ProWorkorder existing = new ProWorkorder();
        existing.setWorkorderId(7L);
        existing.setProductId(10L);
        existing.setQuantity(BigDecimal.ONE);
        existing.setStatus("PREPARE");
        when(mdItemService.selectMdItemById(10L)).thenReturn(product(10L, "P-10", "产品十", "件"));
        when(proWorkorderMapper.selectProWorkorderByWorkorderId(7L)).thenReturn(existing);
        when(proWorkorderMapper.selectProWorkorderByWorkorderId(9L)).thenReturn(parent);
        when(proWorkorderMapper.checkWorkorderCodeUnique(workorder)).thenReturn(existing);

        ServiceException error = assertThrows(ServiceException.class,
                () -> service.updateDraftWorkorder(workorder));

        assertEquals("parentId: 父工单不能形成循环引用", error.getMessage());
        verify(proWorkorderMapper, never()).updateProWorkorder(any());
    }

    private MdItem product(Long id, String code, String name, String unit) {
        MdItem item = new MdItem();
        item.setItemId(id);
        item.setItemCode(code);
        item.setItemName(name);
        item.setUnitOfMeasure(unit);
        item.setSpecification("规格");
        return item;
    }

    private ProWorkorder draft(Long productId, String quantity)
    {
        ProWorkorder workorder = new ProWorkorder();
        workorder.setWorkorderCode("WO-001");
        workorder.setProductId(productId);
        workorder.setQuantity(new BigDecimal(quantity));
        workorder.setParentId(0L);
        return workorder;
    }

    private MdProductBom bom(Long itemId, String itemCode, String quantity)
    {
        MdProductBom bom = new MdProductBom();
        bom.setBomItemId(itemId);
        bom.setBomItemCode(itemCode);
        bom.setBomItemName("物料" + itemId);
        bom.setBomItemSpec("规格" + itemId);
        bom.setItemOrProduct("ITEM");
        bom.setUnitOfMeasure("件");
        bom.setQuantity(new BigDecimal(quantity));
        return bom;
    }

    @Test
    @SuppressWarnings("unchecked")
    void getHomeListBuildsProcessProgressFromFeedbackQuantities()
    {
        ProWorkorder workorder = new ProWorkorder();
        workorder.setWorkorderId(1L);
        workorder.setProductId(10L);
        workorder.setParentId(0L);
        workorder.setQuantity(new BigDecimal("10"));

        ProRouteProduct routeProduct = new ProRouteProduct();
        routeProduct.setItemId(10L);
        routeProduct.setRouteId(20L);

        ProRouteProcess routeProcess = new ProRouteProcess();
        routeProcess.setRouteId(20L);
        routeProcess.setProcessId(30L);

        ProFeedback feedback = new ProFeedback();
        feedback.setWorkorderId(1L);
        feedback.setProcessId(30L);
        feedback.setQuantityFeedback(new BigDecimal("3"));

        when(proWorkorderMapper.selectProWorkorderList(any())).thenReturn(Collections.singletonList(workorder));
        when(proFeedbackService.selectByWorkorderIds(Collections.singletonList(1L)))
                .thenReturn(Collections.singletonList(feedback));
        when(proRouteProductService.selectByProductIds(Collections.singletonList(10L)))
                .thenReturn(Collections.singletonList(routeProduct));
        when(proRouteProcessService.selectByRouteIds(Collections.singletonList(20L)))
                .thenReturn(Collections.singletonList(routeProcess));

        AjaxResult result = service.getHomeList(new ProWorkorder());
        List<ProWorkorderHomeVO> workorders = (List<ProWorkorderHomeVO>) result.get("data");
        ProRouteHomeVO progress = workorders.get(0).getRouteHomg().get(0);

        assertEquals(new BigDecimal("10"), progress.getTotal());
        assertEquals(new BigDecimal("3"), progress.getCompleteNumber());
        assertEquals(new BigDecimal("7"), progress.getIncompleteNumber());
    }
}
