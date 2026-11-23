package com.ktg.mes.pro.execution;

import java.math.BigDecimal;
import java.util.*;

final class ExecutionReportValidator {
    private static final BigDecimal MAX = new BigDecimal("9999999999999999.99");

    static void validate(ExecutionReportRequest request) {
        if (request == null) throw ExecutionException.validation("报工参数为空");
        Set<Long> selected = positiveUnique(request.snIds);
        if (request.inputs == null || request.inputs.isEmpty()) throw ExecutionException.validation("请填写实际用料");
        Map<Long, Set<Long>> stockUnits = new HashMap<>();
        Set<Long> covered = new HashSet<>();
        for (ExecutionInputGroup group : request.inputs) {
            if (group == null || group.materialStockId == null || group.materialStockId <= 0)
                throw ExecutionException.validation("实际库存 ID 无效");
            Set<Long> units = positiveUnique(group.snIds);
            if (!selected.containsAll(units)) throw ExecutionException.validation("投入包含未选择的单件");
            BigDecimal quantity = group.quantityPerUnit;
            if (quantity == null || quantity.signum() <= 0 || quantity.stripTrailingZeros().scale() > 2
                    || quantity.compareTo(MAX) > 0 || quantity.multiply(BigDecimal.valueOf(units.size())).compareTo(MAX) > 0)
                throw ExecutionException.validation("每件实际用量必须为正且不超过两位有效小数");
            Set<Long> used = stockUnits.computeIfAbsent(group.materialStockId, key -> new HashSet<>());
            for (Long sn : units) if (!used.add(sn)) throw ExecutionException.validation("同一 SN 重复选择相同库存");
            covered.addAll(units);
        }
        if (!covered.equals(selected)) throw ExecutionException.validation("每个报工 SN 必须填写投入");
    }

    static Set<Long> positiveUnique(List<Long> ids) {
        if (ids == null || ids.isEmpty()) throw ExecutionException.validation("请选择单件");
        Set<Long> unique = new TreeSet<>();
        for (Long id : ids) if (id == null || id <= 0 || !unique.add(id)) throw ExecutionException.validation("SN ID 无效或重复");
        return unique;
    }
}
