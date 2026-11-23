package com.ktg.mes.pro.execution;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

class ProExecutionReportValidationTest {
    private final ObjectMapper json = new ObjectMapper();

    private ExecutionReportRequest request(String ids, String inputs) throws Exception {
        return json.readValue("{\"operationId\":\"00000000-0000-0000-0000-000000000001\",\"snIds\":" + ids
            + ",\"inputs\":" + inputs + "}", ExecutionReportRequest.class);
    }

    @Test void acceptsActualStockGroupsAndFractionalQuantities() throws Exception {
        ExecutionReportValidator.validate(request("[1,2]", "[{\"snIds\":[1],\"materialStockId\":3,\"quantityPerUnit\":\"1.20\"},"
            + "{\"snIds\":[2],\"materialStockId\":4,\"quantityPerUnit\":1}]"));
    }

    @Test void rejectsUnselectedDuplicateOrUncoveredSn() throws Exception {
        for (String groups : new String[]{
            "[{\"snIds\":[3],\"materialStockId\":3,\"quantityPerUnit\":1}]",
            "[{\"snIds\":[1,1],\"materialStockId\":3,\"quantityPerUnit\":1}]",
            "[{\"snIds\":[1],\"materialStockId\":3,\"quantityPerUnit\":1}]",
            "[{\"snIds\":[1,2],\"materialStockId\":3,\"quantityPerUnit\":1},{\"snIds\":[1],\"materialStockId\":3,\"quantityPerUnit\":1}]"}) {
            assertThrows(ExecutionException.class, () -> ExecutionReportValidator.validate(request("[1,2]", groups)));
        }
        assertThrows(ExecutionException.class, () -> ExecutionReportValidator.validate(request("[1,1]", "[]")));
    }

    @Test void rejectsUnsafeAmountsAndUnknownOrNonIntegralInputs() throws Exception {
        for (String amount : new String[]{"0", "-1", "0.001", "10000000000000000"}) {
            assertThrows(ExecutionException.class, () -> ExecutionReportValidator.validate(request("[1]",
                "[{\"snIds\":[1],\"materialStockId\":3,\"quantityPerUnit\":" + amount + "}]")));
        }
        assertThrows(Exception.class, () -> request("[1.5]", "[]"));
        assertThrows(Exception.class, () -> request("[1]", "[{\"snIds\":[1],\"materialStockId\":3,\"quantityPerUnit\":1,\"batchCode\":\"fake\"}]"));
        assertThrows(Exception.class, () -> request("[1]", "[{\"snIds\":[1],\"materialStockId\":\"3\",\"quantityPerUnit\":1}]"));
    }
}
