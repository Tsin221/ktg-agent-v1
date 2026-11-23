package com.ktg.mes.pro.execution;

import com.fasterxml.jackson.annotation.JsonAnySetter;
import com.fasterxml.jackson.databind.annotation.JsonDeserialize;
import java.math.BigDecimal;
import java.util.List;

public class ExecutionInputGroup {
    @JsonDeserialize(contentUsing = ExecutionReleaseRequest.StrictLong.class)
    public List<Long> snIds;
    @JsonDeserialize(using = ExecutionReleaseRequest.StrictLong.class)
    public Long materialStockId;
    public BigDecimal quantityPerUnit;

    @JsonAnySetter public void unknown(String name, Object value) {
        throw ExecutionException.validation("未知投入字段: " + name);
    }
}
