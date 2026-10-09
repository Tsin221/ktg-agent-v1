package com.ktg.mes.pro.service.impl;

import com.ktg.common.core.domain.AjaxResult;
import com.ktg.common.exception.ServiceException;
import com.ktg.mes.md.domain.MdItem;
import com.ktg.mes.md.service.IMdItemService;
import com.ktg.mes.pro.controller.vo.ProRouteHomeVO;
import com.ktg.mes.pro.controller.vo.ProWorkorderHomeVO;
import com.ktg.mes.pro.domain.ProFeedback;
import com.ktg.mes.pro.domain.ProRouteProcess;
import com.ktg.mes.pro.domain.ProRouteProduct;
import com.ktg.mes.pro.domain.ProWorkorder;
import com.ktg.mes.pro.mapper.ProWorkorderMapper;
import com.ktg.mes.pro.service.IProFeedbackService;
import com.ktg.mes.pro.service.IProRouteProcessService;
import com.ktg.mes.pro.service.IProRouteProductService;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.math.BigDecimal;
import java.util.Collections;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

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
    private IMdItemService mdItemService;

    @InjectMocks
    private ProWorkorderServiceImpl service;

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
        workorder.setParentId(9L);
        ProWorkorder parent = new ProWorkorder();
        parent.setWorkorderId(9L);
        parent.setAncestors("0,7");
        when(mdItemService.selectMdItemById(10L)).thenReturn(product(10L, "P-10", "产品十", "件"));
        when(proWorkorderMapper.selectProWorkorderByWorkorderId(9L)).thenReturn(parent);

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
