package com.ktg.mes.pro.execution;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.ktg.common.core.domain.AjaxResult;
import com.ktg.mes.pro.execution.mapper.ExecutionOperationMapper;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.security.core.context.SecurityContextHolder;

import java.math.BigDecimal;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Proxy;
import java.util.UUID;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;

import static com.ktg.mes.pro.execution.ExecutionAction.*;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.mock;

/** Real migrated MySQL 5.7 schema. Only dedicated per-test rows are mutated/cleaned up. */
@EnabledIfEnvironmentVariable(named = "MES_B01_MYSQL_URL", matches = ".+")
class ProExecutionOperationMySqlTest {
    private final ObjectMapper json = new ObjectMapper();
    private final String fixture = "B01-OP-" + UUID.randomUUID().toString();
    private final String otherFixture = fixture + "-OTHER";
    private JdbcTemplate jdbc;
    private DriverManagerDataSource source;
    private ExecutionOperationService service;
    private Long orderId;
    private Long stockId;
    private boolean safeDatabase;

    @BeforeEach
    void setup() throws Exception {
        source = new DriverManagerDataSource(System.getenv("MES_B01_MYSQL_URL"),
                System.getenv("MES_B01_MYSQL_USER"), System.getenv("MES_B01_MYSQL_PASSWORD"));
        source.setDriverClassName("com.mysql.cj.jdbc.Driver");
        jdbc = new JdbcTemplate(source);
        String database = jdbc.queryForObject("SELECT DATABASE()", String.class);
        if (database == null || !database.matches("(ktg_mes_b01_[a-z0-9_]+|ktg_mes_b04_test)"))
            throw new IllegalStateException("MES-B01 MySQL tests require a dedicated ktg_mes_b01_* database");
        assertTrue(jdbc.queryForObject("SELECT VERSION()", String.class).startsWith("5.7."));
        safeDatabase = true;
        // Only fixture code is used for cleanup, never a bulk table operation.
        orderId = createOrder(fixture);
        assertEquals(1, jdbc.update("INSERT INTO wm_material_stock (item_id,item_code,item_name,unit_of_measure,batch_code,"
                        + "warehouse_id,location_id,area_id,vendor_id,quantity_onhand,workorder_id,workorder_code) "
                        + "SELECT item_id,item_code,item_name,unit_of_measure,?,warehouse_id,location_id,area_id,vendor_id,10,?,?"
                        + " FROM wm_material_stock WHERE unit_of_measure IS NOT NULL LIMIT 1",
                fixture, orderId, fixture));
        stockId = jdbc.queryForObject("SELECT material_stock_id FROM wm_material_stock WHERE batch_code=? AND workorder_id=?",
                Long.class, fixture, orderId);
        service = service();
        ProExecutionOperationServiceTest.authenticate(77L);
    }

    private ExecutionOperationService service() throws Exception {
        return new ExecutionOperationService(ProExecutionOperationMapperTest.mapper(source),
                mock(ExecutionAccessService.class), new DataSourceTransactionManager(source), json);
    }

    private Long createOrder(String code) {
        assertEquals(1, jdbc.update("INSERT INTO pro_workorder(workorder_code,workorder_name,workorder_type,"
                        + "order_source,product_id,product_code,product_name,unit_of_measure,quantity,request_date,"
                        + "parent_id,ancestors,status,version,create_by) "
                        + "SELECT ?,?,'SELF','MANUAL',item_id,item_code,item_name,unit_of_measure,10,NOW(),0,'0','PREPARE',0,'B01-test'"
                        + " FROM md_item WHERE unit_of_measure IS NOT NULL LIMIT 1", code, code));
        return jdbc.queryForObject("SELECT workorder_id FROM pro_workorder WHERE workorder_code=?", Long.class, code);
    }

    @AfterEach
    void cleanup() {
        SecurityContextHolder.clearContext();
        if (!safeDatabase) return;
        jdbc.update("DELETE FROM pro_execution_operation WHERE workorder_id IN "
                + "(SELECT workorder_id FROM pro_workorder WHERE workorder_code IN (?,?))", fixture, otherFixture);
        jdbc.update("DELETE FROM wm_material_stock WHERE batch_code=? AND workorder_code=?", fixture, fixture);
        jdbc.update("DELETE FROM pro_workorder WHERE workorder_code IN (?,?)", fixture, otherFixture);
    }

    @Test
    void concurrentSameOperationCommitsExactlyOneBusinessMutationAndCanRecoverFromNewService() throws Exception {
        String id = UUID.randomUUID().toString();
        AtomicInteger mutations = new AtomicInteger();
        CountDownLatch entered = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        CountDownLatch secondRequested = new CountDownLatch(1);
        ExecutorService executor = Executors.newFixedThreadPool(2);
        Callable<AjaxResult> request = () -> {
            ProExecutionOperationServiceTest.authenticate(77L);
            try {
                return service.execute(id, REPORT, orderId, 19L, null, json.createObjectNode(), () -> {
                    mutations.incrementAndGet();
                    jdbc.update("UPDATE pro_workorder SET quantity_produced=quantity_produced+1 WHERE workorder_id=?", orderId);
                    jdbc.update("UPDATE wm_material_stock SET quantity_onhand=quantity_onhand-1 WHERE material_stock_id=?", stockId);
                    entered.countDown();
                    try {
                        if (!release.await(10, TimeUnit.SECONDS)) throw new IllegalStateException("test wait failed");
                    } catch (InterruptedException ex) {
                        Thread.currentThread().interrupt();
                        throw new IllegalStateException(ex);
                    }
                    return json.createObjectNode().put("snCount", 1);
                });
            } finally { SecurityContextHolder.clearContext(); }
        };
        try {
            Future<AjaxResult> first = executor.submit(request);
            assertTrue(entered.await(10, TimeUnit.SECONDS));
            Future<AjaxResult> second = executor.submit(() -> { secondRequested.countDown(); return request.call(); });
            assertTrue(secondRequested.await(10, TimeUnit.SECONDS));
            // A separate connection cannot see the in-flight receipt before its business commit.
            assertEquals(0, countReceipt(id));
            release.countDown();
            AjaxResult original = first.get(15, TimeUnit.SECONDS);
            assertEquals(200, original.get("code"));
            AjaxResult replay = second.get(15, TimeUnit.SECONDS);
            assertEquals(200, replay.get("code"));
            assertEquals(original.get("data"), replay.get("data"));
            assertEquals(original.get("data"), service().result(id).get("data"));
            assertEquals(1, mutations.get());
            assertEquals(1, countReceipt(id));
            assertEquals(1, jdbc.queryForObject("SELECT quantity_produced FROM pro_workorder WHERE workorder_id=?", Integer.class, orderId));
            assertEquals(0, new BigDecimal("9.00").compareTo(balance()));
        } finally {
            release.countDown();
            executor.shutdownNow();
            executor.awaitTermination(10, TimeUnit.SECONDS);
        }
    }

    @Test
    void rejectionAfterActualStockAndStateWritesRollsBackBothAndReceipt() {
        String id = UUID.randomUUID().toString();
        AjaxResult rejected = service.execute(id, REPORT, orderId, 19L, null, json.createObjectNode(), () -> {
            assertEquals(1, jdbc.update("UPDATE pro_workorder SET status='CONFIRMED' WHERE workorder_id=?", orderId));
            assertEquals(1, jdbc.update("UPDATE wm_material_stock SET quantity_onhand=2 WHERE material_stock_id=?", stockId));
            throw ExecutionException.stateConflict("sample rejection after business writes");
        });
        assertEquals("STATE_CONFLICT", rejected.get("errorCode"));
        assertEquals(0, countReceipt(id));
        assertEquals(0, new BigDecimal("10.00").compareTo(balance()));
        assertEquals("PREPARE", jdbc.queryForObject("SELECT status FROM pro_workorder WHERE workorder_id=?", String.class, orderId));
        assertEquals("NOT_FOUND", service.result(id).get("errorCode"));
    }

    @Test
    void independentlyLockedWorkordersRaceOnSameGlobalOperationIdOnlyOneCanCommit() throws Exception {
        Long otherOrderId = createOrder(otherFixture);
        String id = UUID.randomUUID().toString();
        CountDownLatch firstMutationEntered = new CountDownLatch(1);
        CountDownLatch allowFirstCommit = new CountDownLatch(1);
        CountDownLatch secondClaimAttempted = new CountDownLatch(1);
        AtomicInteger otherMutations = new AtomicInteger();

        // The wrapper observes the actual competing claim, then delegates all SQL unchanged
        // to the real MyBatis mapper so this covers the unique-key race, not just a precheck.
        final ExecutionOperationMapper delegate = ProExecutionOperationMapperTest.mapper(source);
        ExecutionOperationMapper observed = (ExecutionOperationMapper) Proxy.newProxyInstance(
                ExecutionOperationMapper.class.getClassLoader(), new Class<?>[]{ExecutionOperationMapper.class},
                (proxy, method, args) -> {
                    if ("insertClaim".equals(method.getName())) secondClaimAttempted.countDown();
                    try { return method.invoke(delegate, args); }
                    catch (InvocationTargetException wrapped) { throw wrapped.getCause(); }
                });
        ExecutionOperationService competitor = new ExecutionOperationService(observed, mock(ExecutionAccessService.class),
                new DataSourceTransactionManager(source), json);
        ExecutorService worker = Executors.newFixedThreadPool(2);
        try {
            Future<AjaxResult> first = worker.submit(() -> {
                ProExecutionOperationServiceTest.authenticate(77L);
                try {
                    return service.execute(id, REPORT, orderId, 19L, null, json.createObjectNode(), () -> {
                        jdbc.update("UPDATE pro_workorder SET quantity_produced=1 WHERE workorder_id=?", orderId);
                        firstMutationEntered.countDown();
                        try {
                            if (!allowFirstCommit.await(10, TimeUnit.SECONDS))
                                throw new IllegalStateException("First commit wait expired");
                        } catch (InterruptedException interrupted) {
                            Thread.currentThread().interrupt();
                            throw new IllegalStateException(interrupted);
                        }
                        return json.createObjectNode().put("snCount", 1);
                    });
                } finally { SecurityContextHolder.clearContext(); }
            });
            assertTrue(firstMutationEntered.await(10, TimeUnit.SECONDS));
            Future<AjaxResult> second = worker.submit(() -> {
                ProExecutionOperationServiceTest.authenticate(77L);
                try {
                    return competitor.execute(id, REPORT, otherOrderId, 19L, null, json.createObjectNode(), () -> {
                        otherMutations.incrementAndGet();
                        jdbc.update("UPDATE pro_workorder SET quantity_produced=1 WHERE workorder_id=?", otherOrderId);
                        return json.createObjectNode().put("snCount", 1);
                    });
                } finally { SecurityContextHolder.clearContext(); }
            });
            assertTrue(secondClaimAttempted.await(10, TimeUnit.SECONDS), "second workorder reached the unique-key claim");
            assertThrows(TimeoutException.class, () -> second.get(250, TimeUnit.MILLISECONDS),
                    "second claim waits for the winner's open transaction");
            assertEquals(0, countReceipt(id));
            allowFirstCommit.countDown();

            assertEquals(200, first.get(15, TimeUnit.SECONDS).get("code"));
            AjaxResult rejected = second.get(15, TimeUnit.SECONDS);
            assertEquals("OPERATION_CONFLICT", rejected.get("errorCode"));
            assertFalse(rejected.containsKey("data"));
            assertEquals(0, otherMutations.get());
            assertEquals(1, countReceipt(id));
            assertEquals(orderId, jdbc.queryForObject(
                    "SELECT workorder_id FROM pro_execution_operation WHERE operation_id=?", Long.class, id));
            assertEquals(1, jdbc.queryForObject("SELECT quantity_produced FROM pro_workorder WHERE workorder_id=?", Integer.class, orderId));
            assertEquals(0, jdbc.queryForObject("SELECT quantity_produced FROM pro_workorder WHERE workorder_id=?", Integer.class, otherOrderId));
        } finally {
            allowFirstCommit.countDown();
            worker.shutdownNow();
            worker.awaitTermination(10, TimeUnit.SECONDS);
        }
    }

    private int countReceipt(String id) {
        return jdbc.queryForObject("SELECT COUNT(*) FROM pro_execution_operation WHERE operation_id=?", Integer.class, id);
    }

    private BigDecimal balance() {
        return jdbc.queryForObject("SELECT quantity_onhand FROM wm_material_stock WHERE material_stock_id=?", BigDecimal.class, stockId);
    }
}
