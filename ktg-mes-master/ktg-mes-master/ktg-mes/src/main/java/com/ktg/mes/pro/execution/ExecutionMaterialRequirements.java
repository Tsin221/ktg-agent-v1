package com.ktg.mes.pro.execution;

import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.*;

/** Frozen workorder BOM quantities are order totals, not quantities per unit. */
final class ExecutionMaterialRequirements {
    private final Map<Long, String> units = new LinkedHashMap<>();
    private final Map<Long, BigDecimal> quantities = new LinkedHashMap<>();

    static ExecutionMaterialRequirements parse(ObjectMapper json, int plannedUnits, String snapshot) {
        ExecutionMaterialRequirements result = new ExecutionMaterialRequirements();
        try {
            JsonNode rows = json.reader().with(DeserializationFeature.USE_BIG_DECIMAL_FOR_FLOATS).readTree(snapshot);
            if (plannedUnits <= 0) throw invalid("计划数量必须为正整数");
            if (rows == null || !rows.isArray() || rows.size() == 0) throw invalid("至少需要一条用料明细");
            Map<Long, BigDecimal> totals = new LinkedHashMap<>();
            for (JsonNode row : rows) {
                JsonNode id = row.path("item_id"), quantity = row.path("quantity");
                String unit = row.path("unit_of_measure").asText("");
                if (!id.isIntegralNumber() || !id.canConvertToLong() || id.asLong() <= 0)
                    throw invalid("物料 ID 必须为正整数");
                Long itemId = id.asLong();
                if (!"ITEM".equals(row.path("item_or_product").asText()))
                    throw invalid("物料 ID " + itemId + " 必须为原材料（ITEM）");
                if (unit.trim().isEmpty() || !unit.equals(unit.trim()))
                    throw invalid("物料 ID " + itemId + " 的单位不能为空或含首尾空格");
                if (!quantity.isNumber() || quantity.decimalValue().signum() <= 0)
                    throw invalid("物料 ID " + itemId + " 的预计使用量必须大于 0");
                String priorUnit = result.units.putIfAbsent(itemId, unit);
                if (priorUnit != null && !priorUnit.equals(unit))
                    throw invalid("物料 ID " + itemId + " 的用料明细单位不一致");
                totals.merge(itemId, quantity.decimalValue(), BigDecimal::add);
            }
            totals.forEach((item, total) -> {
                try {
                    result.quantities.put(item, total.divide(BigDecimal.valueOf(plannedUnits), 2, RoundingMode.UNNECESSARY));
                } catch (ArithmeticException imprecise) {
                    throw invalid("物料 ID " + item + " 总用量无法按计划数量精确分摊至两位小数，请调整预计使用量或计划数量");
                }
            });
            return result;
        } catch (ExecutionException invalid) {
            throw invalid;
        } catch (Exception invalid) {
            throw invalid("用料数据格式无效");
        }
    }

    Set<Long> itemIds() { return Collections.unmodifiableSet(units.keySet()); }
    String unit(Long itemId) { return units.get(itemId); }

    boolean allows(Long itemId, String unit) { return units.containsKey(itemId) && units.get(itemId).equals(unit); }

    void validateUnit(Map<Long, BigDecimal> actual) {
        if (!actual.keySet().equals(quantities.keySet())) throw ExecutionException.validation("每件产品必须完整记录冻结用料要求");
        quantities.forEach((item, required) -> {
            if (required.compareTo(actual.get(item)) != 0)
                throw ExecutionException.validation("每件产品的实际用量须符合冻结用料要求");
        });
    }

    private static ExecutionException invalid(String reason) {
        return ExecutionException.validation("冻结用料要求无效：" + reason);
    }
}
