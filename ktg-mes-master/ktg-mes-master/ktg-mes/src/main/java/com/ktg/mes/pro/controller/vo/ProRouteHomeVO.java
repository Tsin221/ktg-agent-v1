package com.ktg.mes.pro.controller.vo;

import com.ktg.mes.pro.domain.ProRouteProcess;

import java.math.BigDecimal;

public class ProRouteHomeVO extends ProRouteProcess
{
    private BigDecimal completeNumber;
    private BigDecimal incompleteNumber;
    private BigDecimal total;

    public BigDecimal getCompleteNumber() { return completeNumber; }
    public void setCompleteNumber(BigDecimal completeNumber) { this.completeNumber = completeNumber; }
    public BigDecimal getIncompleteNumber() { return incompleteNumber; }
    public void setIncompleteNumber(BigDecimal incompleteNumber) { this.incompleteNumber = incompleteNumber; }
    public BigDecimal getTotal() { return total; }
    public void setTotal(BigDecimal total) { this.total = total; }
}
