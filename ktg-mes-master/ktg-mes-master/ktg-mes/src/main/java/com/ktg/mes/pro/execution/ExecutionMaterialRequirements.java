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
            if (plannedUnits <= 0 || rows == null || !rows.isArray() || rows.size() == 0) throw invalid();
            Map<Long, BigDecimal> totals = new LinkedHashMap<>();
            for (JsonNode row : rows) {
                JsonNode id = row.path("item_id"), quantity = row.path("quantity");
                String unit = row.path("unit_of_measure").asText("");
                if (!id.isIntegralNumber() || !id.canConvertToLong() || id.asLong() <= 0
                        || !"ITEM".equals(row.path("item_or_product").asText())
                        || unit.trim().isEmpty() || !unit.equals(unit.trim()) || !quantity.isNumber()
                        || quantity.decimalValue().signum() <= 0) throw invalid();
                Long itemId = id.asLong();
                String priorUnit = result.units.putIfAbsent(itemId, unit);
                if (priorUnit != null && !priorUnit.equals(unit)) throw invalid();
                totals.merge(itemId, quantity.decimalValue(), BigDecimal::add);
            }
            totals.forEach((item, total) -> result.quantities.put(item,
                    total.divide(BigDecimal.valueOf(plannedUnits), 2, RoundingMode.UNNECESSARY)));
            return result;
        } catch (ExecutionException invalid) {
            throw invalid;
        } catch (Exception invalid) {
            throw invalid();
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

    private static ExecutionException invalid() {
        return ExecutionException.validation("冻结用料要求无效或需要超出本期支持范围的用料");
    }
}
