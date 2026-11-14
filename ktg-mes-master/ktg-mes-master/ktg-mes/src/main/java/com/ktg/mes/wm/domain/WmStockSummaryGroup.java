package com.ktg.mes.wm.domain;

import java.math.BigDecimal;

public class WmStockSummaryGroup
{
    private final Long itemId;
    private final String itemCode;
    private final Long warehouseId;
    private final String warehouseCode;
    private final String warehouseName;
    private final String unitOfMeasure;
    private final BigDecimal quantityOnhand;
    private final Long sourceRowCount;

    public WmStockSummaryGroup(Long itemId, String itemCode, Long warehouseId, String warehouseCode,
                               String warehouseName, String unitOfMeasure, BigDecimal quantityOnhand,
                               Long sourceRowCount)
    {
        this.itemId = itemId;
        this.itemCode = itemCode;
        this.warehouseId = warehouseId;
        this.warehouseCode = warehouseCode;
        this.warehouseName = warehouseName;
        this.unitOfMeasure = unitOfMeasure;
        this.quantityOnhand = quantityOnhand;
        this.sourceRowCount = sourceRowCount;
    }

    public Long getItemId()
    {
        return itemId;
    }

    public String getItemCode()
    {
        return itemCode;
    }

    public Long getWarehouseId()
    {
        return warehouseId;
    }

    public String getWarehouseCode()
    {
        return warehouseCode;
    }

    public String getWarehouseName()
    {
        return warehouseName;
    }

    public String getUnitOfMeasure()
    {
        return unitOfMeasure;
    }

    public String getQuantityOnhand()
    {
        return quantityOnhand.toPlainString();
    }

    public Long getSourceRowCount()
    {
        return sourceRowCount;
    }
}
