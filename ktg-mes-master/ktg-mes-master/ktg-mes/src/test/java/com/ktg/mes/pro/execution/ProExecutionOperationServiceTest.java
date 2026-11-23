package com.ktg.mes.pro.execution;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.ktg.common.core.domain.AjaxResult;
import com.ktg.common.core.domain.model.LoginUser;
import org.junit.jupiter.api.*;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabaseBuilder;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabaseType;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.context.SecurityContextHolder;

import javax.sql.DataSource;
import java.util.Collections;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicInteger;

import static com.ktg.mes.pro.execution.ExecutionAction.*;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

class ProExecutionOperationServiceTest {
    private final ObjectMapper json = new ObjectMapper();
    private ExecutionOperationService service;
    private ExecutionAccessService access;
    private JdbcTemplate jdbc;

    @BeforeEach
    void setup() throws Exception {
        authenticate(7L);
        DataSource source = new EmbeddedDatabaseBuilder().setType(EmbeddedDatabaseType.H2)
                .setName("mes-b01-service-" + UUID.randomUUID()).build();
        jdbc = new JdbcTemplate(source);
        jdbc.execute("CREATE TABLE pro_workorder (workorder_id BIGINT PRIMARY KEY, status VARCHAR(32))");
        jdbc.execute("CREATE TABLE pro_execution_operation (operation_id CHAR(36) PRIMARY KEY,"
                + " action_type VARCHAR(32), workorder_id BIGINT, actor_id BIGINT,"
                + " request_digest CHAR(71), result_ref VARCHAR(4096), committed_at TIMESTAMP)");
        jdbc.execute("CREATE TABLE sample_stock (id BIGINT PRIMARY KEY, balance INT)");
        jdbc.update("INSERT INTO pro_workorder VALUES (19,'PREPARE'),(20,'PREPARE')");
        jdbc.update("INSERT INTO sample_stock VALUES (1,10)");
        access = mock(ExecutionAccessService.class);
        service = new ExecutionOperationService(ProExecutionOperationMapperTest.mapper(source), access,
                new DataSourceTransactionManager(source), json);
    }

    @AfterEach void logout() { SecurityContextHolder.clearContext(); }

    @Test
    void successReplayAndRecoveryNeverRepeatMutationOrExposeOtherActor() {
        String operationId = UUID.randomUUID().toString();
        AtomicInteger mutations = new AtomicInteger();
        java.util.function.Supplier<com.fasterxml.jackson.databind.node.ObjectNode> body = () -> {
            mutations.incrementAndGet();
            jdbc.update("UPDATE pro_workorder SET status='CONFIRMED' WHERE workorder_id=19");
            return json.createObjectNode().put("state", "CONFIRMED");
        };
        AjaxResult original = service.execute(operationId, RELEASE, 19L, null, null, json.createObjectNode(), body);
        assertEquals(200, original.get("code"));
        assertEquals(original.get("data"), service.execute(operationId, RELEASE, 19L, null, null,
                json.createObjectNode(), body).get("data"));
        authenticate(7L); // fresh login context
        assertEquals(original.get("data"), service.result(operationId).get("data"));
        assertEquals(1, mutations.get());
        assertEquals("OPERATION_CONFLICT", service.execute(operationId, COMPLETE, 19L, null, null,
                json.createObjectNode(), body).get("errorCode"));
        assertEquals("OPERATION_CONFLICT", service.execute(operationId, RELEASE, 20L, null, null,
                json.createObjectNode(), body).get("errorCode"));
        assertEquals("OPERATION_CONFLICT", service.execute(operationId, RELEASE, 19L, null, null,
                json.createObjectNode().put("version", 2), body).get("errorCode"));
        authenticate(8L);
        assertEquals("NOT_FOUND", service.result(operationId).get("errorCode"));
        assertEquals("OPERATION_CONFLICT", service.execute(operationId, RELEASE, 19L, null, null,
                json.createObjectNode(), body).get("errorCode"));
        assertEquals(1, mutations.get());
        authenticate(7L);
        doThrow(ExecutionException.forbidden()).when(access).requireReceipt(RELEASE, 19L, null, null);
        assertEquals("NOT_FOUND", service.result(operationId).get("errorCode"));
        assertEquals("NOT_FOUND", service.execute(operationId, RELEASE, 19L, null, null,
                json.createObjectNode(), body).get("errorCode"));
    }

    @Test
    void failedMutationRollsBackStateStockAndClaim() {
        String operationId = UUID.randomUUID().toString();
        AjaxResult result = service.execute(operationId, REPORT, 19L, 31L, null, json.createObjectNode(), () -> {
            jdbc.update("UPDATE pro_workorder SET status='CONFIRMED' WHERE workorder_id=19");
            jdbc.update("UPDATE sample_stock SET balance=5 WHERE id=1");
            throw ExecutionException.stateConflict("状态不合法");
        });
        assertEquals("STATE_CONFLICT", result.get("errorCode"));
        assertEquals("PREPARE", jdbc.queryForObject("SELECT status FROM pro_workorder WHERE workorder_id=19", String.class));
        assertEquals(10, jdbc.queryForObject("SELECT balance FROM sample_stock WHERE id=1", Integer.class));
        assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM pro_execution_operation", Integer.class));
        assertEquals("NOT_FOUND", service.result(operationId).get("errorCode"));
    }

    @Test
    void authorizationFailureDoesNotInvokeMutationAndInvalidIdIsRejected() {
        doThrow(ExecutionException.forbidden()).when(access).requireOperation(REPORT, 19L, 31L, null);
        assertEquals("FORBIDDEN", service.execute(UUID.randomUUID().toString(), REPORT, 19L, 31L, null,
                json.createObjectNode(), () -> { fail("No mutation allowed"); return null; }).get("errorCode"));
        assertEquals("VALIDATION_ERROR", service.execute("invalid", REPORT, 19L, 31L, null,
                json.createObjectNode(), () -> { fail("No mutation allowed"); return null; }).get("errorCode"));
        assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM pro_execution_operation", Integer.class));
    }

    @Test
    void receiptEndpointReturnsServiceStatusAndNoWriteMapping() throws Exception {
        String operationId = UUID.randomUUID().toString();
        org.springframework.test.web.servlet.MockMvc mvc =
                org.springframework.test.web.servlet.setup.MockMvcBuilders
                        .standaloneSetup(new ExecutionOperationController(service)).build();
        mvc.perform(org.springframework.test.web.servlet.request.MockMvcRequestBuilders
                        .get("/mes/pro/execution/operations/" + operationId))
                .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers.status().isNotFound())
                .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath("$.errorCode").value("NOT_FOUND"));
        mvc.perform(org.springframework.test.web.servlet.request.MockMvcRequestBuilders
                        .post("/mes/pro/execution/operations/" + operationId))
                .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers.status().isMethodNotAllowed());
    }

    @Test
    void unexpectedFailureIsUnknownAndInvalidResultRollsBack() {
        String operationId = UUID.randomUUID().toString();
        assertEquals("RESULT_UNKNOWN", service.execute(operationId, REPORT, 19L, 31L, null, json.createObjectNode(), () -> {
            jdbc.update("UPDATE sample_stock SET balance=0 WHERE id=1");
            return json.createObjectNode().put("unsafeRawPayload", "must not be persisted");
        }).get("errorCode"));
        assertEquals(10, jdbc.queryForObject("SELECT balance FROM sample_stock WHERE id=1", Integer.class));
        assertEquals("NOT_FOUND", service.result(operationId).get("errorCode"));
    }

    @Test
    void canonicalDigestNormalizesFieldOrderQuantitiesAndUnorderedSnGroups() throws Exception {
        ExecutionRequestDigest digest = new ExecutionRequestDigest(json);
        JsonNode a = json.readTree("{\"snIds\":[3,2,1],\"inputs\":[{\"snIds\":[3,2],\"materialStockId\":9,\"quantityPerUnit\":\"1.00\"},{\"snIds\":[1],\"materialStockId\":8,\"quantityPerUnit\":2}]}");
        JsonNode b = json.readTree("{\"inputs\":[{\"materialStockId\":8,\"quantityPerUnit\":\"2.0\",\"snIds\":[1]},{\"quantityPerUnit\":1,\"materialStockId\":9,\"snIds\":[2,3]}],\"snIds\":[1,2,3]}");
        assertEquals(digest.digest(REPORT, 19L, 31L, null, a), digest.digest(REPORT, 19L, 31L, null, b));
        assertNotEquals(digest.digest(REPORT, 19L, 31L, null, a), digest.digest(REPORT, 19L, 32L, null, a));
        JsonNode duplicate = json.readTree("{\"snIds\":[1,1]}");
        assertThrows(ExecutionException.class, () -> digest.digest(REPORT, 19L, 31L, null, duplicate));
        JsonNode duplicatePair = json.readTree("{\"inputs\":[{\"snIds\":[1],\"materialStockId\":8,\"quantityPerUnit\":1},{\"snIds\":[1],\"materialStockId\":8,\"quantityPerUnit\":1}]}");
        assertThrows(ExecutionException.class, () -> digest.digest(REPORT, 19L, 31L, null, duplicatePair));
        JsonNode overPrecision = json.readTree("{\"quantityPerUnit\":\"0.001\"}");
        assertThrows(ExecutionException.class, () -> digest.digest(REPORT, 19L, 31L, null, overPrecision));
    }

    static void authenticate(long id) {
        LoginUser login = new LoginUser();
        login.setUserId(id);
        SecurityContextHolder.getContext().setAuthentication(
                new UsernamePasswordAuthenticationToken(login, null, Collections.emptyList()));
    }
}
