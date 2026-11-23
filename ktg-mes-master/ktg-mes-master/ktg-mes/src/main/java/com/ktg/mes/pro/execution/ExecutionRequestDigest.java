package com.ktg.mes.pro.execution;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;

/** Canonical encoding for already validated endpoint DTOs, never raw request JSON. */
public final class ExecutionRequestDigest {
    private final ObjectMapper json;

    public ExecutionRequestDigest(ObjectMapper json) { this.json = json; }

    public String digest(ExecutionAction action, Long orderId, Long taskId, Long snId, JsonNode payload) {
        if (payload == null || !payload.isObject()) throw ExecutionException.validation("请求内容必须是对象");
        TreeMap<String, Object> envelope = new TreeMap<>();
        envelope.put("action", action.name());
        envelope.put("workorderId", orderId);
        if (taskId != null) envelope.put("taskId", taskId);
        if (snId != null) envelope.put("snId", snId);
        envelope.put("payload", normalize(payload, ""));
        try {
            byte[] canonical = json.writeValueAsString(envelope).getBytes(StandardCharsets.UTF_8);
            byte[] hash = MessageDigest.getInstance("SHA-256").digest(canonical);
            StringBuilder hex = new StringBuilder("sha256:");
            for (byte value : hash) hex.append(String.format("%02x", value & 255));
            return hex.toString();
        } catch (Exception ex) {
            throw new IllegalStateException("Cannot compute execution request digest", ex);
        }
    }

    private Object normalize(JsonNode node, String field) {
        if (node.isObject()) {
            TreeMap<String, Object> result = new TreeMap<>();
            node.fields().forEachRemaining(entry -> {
                if (!entry.getValue().isNull()) {
                    result.put(entry.getKey(), normalize(entry.getValue(), entry.getKey()));
                }
            });
            return result;
        }
        if (node.isArray()) {
            if ("snIds".equals(field)) {
                TreeSet<Long> ids = new TreeSet<>();
                node.forEach(value -> {
                    if (!ids.add(id(value))) throw ExecutionException.validation("SN ID 重复");
                });
                return new ArrayList<>(ids);
            }
            if ("inputs".equals(field)) return inputs(node);
            List<Object> result = new ArrayList<>();
            node.forEach(value -> result.add(normalize(value, "")));
            return result;
        }
        if ("quantityPerUnit".equals(field) || "quantity".equals(field)) {
            try {
                if (!node.isNumber() && !node.isTextual()) throw new NumberFormatException();
                BigDecimal amount = new BigDecimal(node.asText()).stripTrailingZeros();
                if (amount.signum() <= 0 || amount.scale() > 2 || amount.compareTo(new BigDecimal("9999999999999999.99")) > 0)
                    throw new NumberFormatException();
                return amount.toPlainString();
            } catch (NumberFormatException ex) {
                throw ExecutionException.validation("实际用量不合法");
            }
        }
        if (node.isIntegralNumber() && node.canConvertToLong()) return node.longValue();
        if (node.isFloatingPointNumber()) return node.decimalValue().stripTrailingZeros().toPlainString();
        if (node.isTextual()) return node.textValue();
        if (node.isBoolean()) return node.booleanValue();
        throw ExecutionException.validation("请求内容类型不合法");
    }

    private List<Object> inputs(JsonNode node) {
        List<JsonNode> sorted = new ArrayList<>();
        Set<String> usedPairs = new HashSet<>();
        for (JsonNode group : node) {
            if (!group.isObject() || !group.path("snIds").isArray())
                throw ExecutionException.validation("投入分组无效");
            long stockId = id(group.path("materialStockId"));
            @SuppressWarnings("unchecked")
            List<Long> snIds = (List<Long>) normalize(group.path("snIds"), "snIds");
            if (snIds.isEmpty()) throw ExecutionException.validation("投入分组不能为空");
            for (Long snId : snIds) {
                if (!usedPairs.add(snId + ":" + stockId)) throw ExecutionException.validation("SN 与库存重复关联");
            }
            sorted.add(group);
        }
        sorted.sort(Comparator.comparingLong((JsonNode group) -> id(group.path("materialStockId")))
                .thenComparing(this::sortedGroupIds, ExecutionRequestDigest::compareIds));
        List<Object> result = new ArrayList<>();
        sorted.forEach(group -> result.add(normalize(group, "")));
        return result;
    }

    @SuppressWarnings("unchecked")
    private List<Long> sortedGroupIds(JsonNode group) {
        return (List<Long>) normalize(group.path("snIds"), "snIds");
    }

    private static int compareIds(List<Long> a, List<Long> b) {
        for (int i = 0; i < Math.min(a.size(), b.size()); i++) {
            int comparison = Long.compare(a.get(i), b.get(i));
            if (comparison != 0) return comparison;
        }
        return Integer.compare(a.size(), b.size());
    }

    private long id(JsonNode value) {
        if (!value.isIntegralNumber() || !value.canConvertToLong() || value.longValue() <= 0)
            throw ExecutionException.validation("ID 必须为正整数");
        return value.longValue();
    }
}
