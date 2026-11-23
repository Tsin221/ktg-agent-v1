package com.ktg.mes.pro.execution;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import java.math.BigDecimal;
import java.util.Collections;

import static org.junit.jupiter.api.Assertions.*;

class ProExecutionMaterialQueryTest {
    private static final ObjectMapper JSON = new ObjectMapper();

    @Test void frozenWorkorderTotalIsAppliedPerSelectedUnitWithoutRounding() throws Exception {
        ExecutionMaterialRequirements requirements = ExecutionMaterialRequirements.parse(JSON, 10,
                "[{\"item_id\":7,\"item_or_product\":\"ITEM\",\"unit_of_measure\":\"EA\",\"quantity\":10},"
                        + "{\"item_id\":8,\"item_or_product\":\"ITEM\",\"unit_of_measure\":\"KG\",\"quantity\":2.50}]");
        assertTrue(requirements.allows(7L, "EA"));
        assertFalse(requirements.allows(7L, "KG"));
        assertFalse(requirements.allows(9L, "EA"));
        assertThrows(ExecutionException.class, () -> requirements.validateUnit(Collections.singletonMap(7L, BigDecimal.ONE)));
        java.util.Map<Long, BigDecimal> actual = new java.util.HashMap<>();
        actual.put(7L, BigDecimal.ONE);
        actual.put(8L, new BigDecimal("0.25"));
        assertDoesNotThrow(() -> requirements.validateUnit(actual));
        actual.put(8L, new BigDecimal("0.26"));
        assertThrows(ExecutionException.class, () -> requirements.validateUnit(actual));
    }

    @Test void unsupportedOrIncompleteFrozenRequirementsAreNotSilentlyIgnored() {
        assertThrows(ExecutionException.class, () -> ExecutionMaterialRequirements.parse(JSON, 10,
                "[{\"item_id\":7,\"item_or_product\":\"PRODUCT\",\"unit_of_measure\":\"EA\",\"quantity\":10}]"));
        assertThrows(ExecutionException.class, () -> ExecutionMaterialRequirements.parse(JSON, 10,
                "[{\"item_id\":7,\"item_or_product\":\"ITEM\",\"unit_of_measure\":\"EA\"}]"));
    }
}
