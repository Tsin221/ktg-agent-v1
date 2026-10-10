package com.ktg.mes.pro.service.impl;

import com.ktg.common.exception.ServiceException;
import com.ktg.mes.pro.domain.ProWorkorder;
import com.ktg.mes.pro.domain.ProWorkorderBom;
import com.ktg.mes.pro.mapper.ProWorkorderBomMapper;
import com.ktg.mes.pro.mapper.ProWorkorderMapper;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.math.BigDecimal;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
class ProWorkorderBomServiceImplTest
{
    @Mock
    private ProWorkorderBomMapper proWorkorderBomMapper;
    @Mock
    private ProWorkorderMapper proWorkorderMapper;
    @InjectMocks
    private ProWorkorderBomServiceImpl service;

    @Test
    void insertLocksDraftWorkorderBeforeWritingBom()
    {
        ProWorkorder workorder = workorder(7L, "PREPARE");
        ProWorkorderBom bom = bom(7L);
        when(proWorkorderMapper.selectProWorkorderByWorkorderIdForUpdate(7L)).thenReturn(workorder);
        when(proWorkorderBomMapper.insertProWorkorderBom(bom)).thenReturn(1);

        assertEquals(1, service.insertProWorkorderBom(bom));

        verify(proWorkorderMapper).selectProWorkorderByWorkorderIdForUpdate(7L);
        verify(proWorkorderBomMapper).insertProWorkorderBom(bom);
    }

    @Test
    void insertRejectsMissingOrNonDraftWorkorderBeforeWritingBom()
    {
        ProWorkorderBom missingBom = bom(404L);
        ProWorkorderBom confirmedBom = bom(8L);
        when(proWorkorderMapper.selectProWorkorderByWorkorderIdForUpdate(404L)).thenReturn(null);
        when(proWorkorderMapper.selectProWorkorderByWorkorderIdForUpdate(8L))
                .thenReturn(workorder(8L, "CONFIRMED"));

        ServiceException missing = assertThrows(ServiceException.class,
                () -> service.insertProWorkorderBom(missingBom));
        ServiceException confirmed = assertThrows(ServiceException.class,
                () -> service.insertProWorkorderBom(confirmedBom));

        assertEquals(Integer.valueOf(404), missing.getCode());
        assertEquals(Integer.valueOf(409), confirmed.getCode());
        verify(proWorkorderBomMapper, never()).insertProWorkorderBom(any());
    }

    @Test
    void insertRejectsEmptyWorkorderIdBeforeReadingOrWriting()
    {
        ServiceException error = assertThrows(ServiceException.class,
                () -> service.insertProWorkorderBom(new ProWorkorderBom()));

        assertEquals(Integer.valueOf(400), error.getCode());
        verify(proWorkorderMapper, never()).selectProWorkorderByWorkorderIdForUpdate(any());
        verify(proWorkorderBomMapper, never()).insertProWorkorderBom(any());
    }

    private ProWorkorder workorder(Long id, String status)
    {
        ProWorkorder workorder = new ProWorkorder();
        workorder.setWorkorderId(id);
        workorder.setStatus(status);
        return workorder;
    }

    private ProWorkorderBom bom(Long workorderId)
    {
        ProWorkorderBom bom = new ProWorkorderBom();
        bom.setWorkorderId(workorderId);
        bom.setItemId(101L);
        bom.setItemCode("M-101");
        bom.setItemName("物料一");
        bom.setQuantity(BigDecimal.ONE);
        return bom;
    }
}
