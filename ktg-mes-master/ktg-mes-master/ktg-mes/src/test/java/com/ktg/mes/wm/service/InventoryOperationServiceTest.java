package com.ktg.mes.wm.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.ktg.common.core.domain.AjaxResult;
import com.ktg.common.core.domain.entity.SysUser;
import com.ktg.common.core.domain.model.LoginUser;
import com.ktg.mes.wm.domain.tx.IssueTxBean;
import com.ktg.mes.wm.domain.tx.ItemRecptTxBean;
import com.ktg.mes.wm.mapper.InventoryOperationMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mockito;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabaseBuilder;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabaseType;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.test.context.ContextConfiguration;
import org.springframework.test.context.junit.jupiter.SpringExtension;
import org.springframework.transaction.PlatformTransactionManager;

import javax.sql.DataSource;
import java.math.BigDecimal;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.anyList;

@ExtendWith(SpringExtension.class)
@ContextConfiguration(classes = InventoryOperationServiceTest.Config.class)
class InventoryOperationServiceTest {
    @Autowired private InventoryOperationService service;
    @Autowired private JdbcTemplate jdbc;
    @Autowired private IWmItemRecptService receipts;
    @Autowired private IWmIssueHeaderService issues;
    @Autowired private IStorageCoreService storage;

    @BeforeEach
    void setUp() {
        Mockito.reset(receipts, issues, storage);
        jdbc.execute("DELETE FROM wm_inventory_operation_transaction");
        jdbc.execute("DELETE FROM wm_inventory_executed_document");
        jdbc.execute("DELETE FROM wm_inventory_operation");
        jdbc.execute("DELETE FROM wm_transaction");
        jdbc.execute("DELETE FROM wm_item_recpt");
        jdbc.execute("DELETE FROM wm_issue_header");
        jdbc.execute("DELETE FROM wm_material_stock");
        jdbc.update("INSERT INTO wm_material_stock(material_stock_id, quantity_onhand) VALUES (1, 10)");
        jdbc.update("INSERT INTO wm_item_recpt(recpt_id, recpt_code, status) VALUES (1, 'IR-1', 'APPROVED')");
        jdbc.update("INSERT INTO wm_issue_header(issue_id, issue_code, status) VALUES (2, 'ISSUE-2', 'APPROVED')");
        authenticate();
    }

    private void authenticate() {
        LoginUser login = new LoginUser();
        SysUser user = new SysUser();
        user.setUserName("operator");
        login.setUser(user);
        login.setUserId(7L);
        SecurityContextHolder.getContext().setAuthentication(
                new UsernamePasswordAuthenticationToken(login, null, Collections.emptyList()));
    }

    @Test
    void twoJavaExecutorsShareOneMySqlDocumentClaim() throws Exception {
        Assumptions.assumeTrue("ktg_s302_java_test".equals(System.getenv("KTG_S302_MYSQL_DB")));
        Mockito.when(receipts.getTxBeans(1L)).thenReturn(Collections.singletonList(receiptLine()));
        CountDownLatch firstInside = new CountDownLatch(1);
        CountDownLatch releaseFirst = new CountDownLatch(1);
        Mockito.doAnswer(call -> {
            firstInside.countDown();
            assertTrue(releaseFirst.await(10, TimeUnit.SECONDS));
            jdbc.update("UPDATE wm_material_stock SET quantity_onhand = quantity_onhand + 2 "
                    + "WHERE material_stock_id = 1");
            jdbc.update("INSERT INTO wm_transaction(transaction_type, transaction_flag, transaction_quantity, "
                    + "source_doc_type, source_doc_id, source_doc_line_id, unit_of_measure) "
                    + "VALUES ('ITEM_RECPT', 1, 2, 'IR', 1, 11, 'pcs')");
            return null;
        }).when(storage).processItemRecpt(anyList());
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            Future<AjaxResult> first = pool.submit(() -> {
                authenticate();
                return service.executeReceipt(1L, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa");
            });
            assertTrue(firstInside.await(10, TimeUnit.SECONDS));
            Future<AjaxResult> second = pool.submit(() -> {
                authenticate();
                return service.executeReceipt(1L, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb");
            });
            Thread.sleep(300);
            releaseFirst.countDown();
            assertEquals(200, first.get(10, TimeUnit.SECONDS).get("code"));
            assertEquals("DOCUMENT_ALREADY_EXECUTED", second.get(10, TimeUnit.SECONDS).get("errorCode"));
        } finally {
            releaseFirst.countDown();
            pool.shutdownNow();
        }
        assertEquals(1, count("wm_transaction"));
        assertEquals(1, count("wm_inventory_executed_document"));
        assertEquals(0, new BigDecimal("12.00").compareTo(jdbc.queryForObject(
                "SELECT quantity_onhand FROM wm_material_stock WHERE material_stock_id = 1", BigDecimal.class)));
    }

    @Test
    void committedReceiptSurvivesReplayAndBlocksAnotherOperationId() {
        ItemRecptTxBean line = receiptLine();
        Mockito.when(receipts.getTxBeans(1L)).thenReturn(Collections.singletonList(line));
        Mockito.doAnswer(call -> {
            jdbc.update("UPDATE wm_material_stock SET quantity_onhand = quantity_onhand + 2 WHERE material_stock_id = 1");
            jdbc.update("INSERT INTO wm_transaction(transaction_type, transaction_flag, transaction_quantity, "
                    + "source_doc_type, source_doc_id, source_doc_line_id, unit_of_measure) "
                    + "VALUES ('ITEM_RECPT', 1, 2, 'IR', 1, 11, 'pcs')");
            return null;
        }).when(storage).processItemRecpt(anyList());

        String id = "2fd58fc8-148d-4bde-9083-86b6c7a46c5b";
        AjaxResult first = service.executeReceipt(1L, id);
        AjaxResult replay = service.executeReceipt(1L, id);
        AjaxResult another = service.executeReceipt(1L, "2fd58fc8-148d-4bde-9083-86b6c7a46c5c");
        AjaxResult conflicting = service.executeIssue(2L, id);

        assertEquals(200, first.get("code"));
        assertEquals(200, replay.get("code"));
        assertEquals(Boolean.TRUE, data(replay).get("replayed"));
        assertEquals(409, another.get("code"));
        assertEquals(id, data(another).get("existingOperationId"));
        assertEquals("OPERATION_ID_CONFLICT", conflicting.get("errorCode"));
        assertEquals(1, count("wm_transaction"));
        assertEquals(1, count("wm_inventory_operation_transaction"));
        assertEquals(1, count("wm_inventory_executed_document"));
        assertEquals(0, new BigDecimal("12.00").compareTo(jdbc.queryForObject(
                "SELECT quantity_onhand FROM wm_material_stock WHERE material_stock_id = 1", BigDecimal.class)));
        assertEquals("FINISHED", jdbc.queryForObject(
                "SELECT status FROM wm_item_recpt WHERE recpt_id = 1", String.class));
        assertNotNull(jdbc.queryForObject("SELECT result_json FROM wm_inventory_operation WHERE operation_id = ?",
                String.class, id));
    }

    @Test
    void secondIssueLedgerFailureRollsBackStockLedgerDocumentAndSuccessReceipt() {
        IssueTxBean line = issueLine();
        Mockito.when(issues.getTxBeans(2L)).thenReturn(Collections.singletonList(line));
        Mockito.doAnswer(call -> {
            jdbc.update("UPDATE wm_material_stock SET quantity_onhand = quantity_onhand - 2 WHERE material_stock_id = 1");
            jdbc.update("INSERT INTO wm_transaction(transaction_type, transaction_flag, transaction_quantity, "
                    + "source_doc_type, source_doc_id, source_doc_line_id, unit_of_measure) "
                    + "VALUES ('ITEM_ISSUE_OUT', -1, -2, 'ISSUE', 2, 21, 'pcs')");
            throw new IllegalStateException("simulated second ledger failure");
        }).when(storage).processIssue(anyList());

        String id = "b9ec1397-ddeb-48f6-a08e-c0d5a6331457";
        AjaxResult result = service.executeIssue(2L, id);

        assertEquals(500, result.get("code"));
        assertEquals(0, count("wm_transaction"));
        assertEquals(0, count("wm_inventory_executed_document"));
        assertEquals(0, count("wm_inventory_operation_transaction"));
        assertEquals(0, new BigDecimal("10.00").compareTo(jdbc.queryForObject(
                "SELECT quantity_onhand FROM wm_material_stock WHERE material_stock_id = 1", BigDecimal.class)));
        assertEquals("APPROVED", jdbc.queryForObject(
                "SELECT status FROM wm_issue_header WHERE issue_id = 2", String.class));
        assertEquals("failed", jdbc.queryForObject(
                "SELECT operation_status FROM wm_inventory_operation WHERE operation_id = ?", String.class, id));
        assertEquals(500, service.executeIssue(2L, id).get("code"));
        Mockito.verify(storage, Mockito.times(1)).processIssue(anyList());
    }

    private ItemRecptTxBean receiptLine() {
        ItemRecptTxBean line = new ItemRecptTxBean();
        line.setSourceDocLineId(11L);
        line.setTransactionQuantity(new BigDecimal("2.00"));
        return line;
    }

    private IssueTxBean issueLine() {
        IssueTxBean line = new IssueTxBean();
        line.setSourceDocLineId(21L);
        line.setTransactionQuantity(new BigDecimal("2.00"));
        return line;
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> data(AjaxResult result) {
        return (Map<String, Object>) result.get("data");
    }

    private int count(String table) {
        return jdbc.queryForObject("SELECT COUNT(*) FROM " + table, Integer.class);
    }

    @Configuration
    static class Config {
        @Bean DataSource dataSource() {
            if ("ktg_s302_java_test".equals(System.getenv("KTG_S302_MYSQL_DB"))) {
                DriverManagerDataSource source = new DriverManagerDataSource();
                source.setDriverClassName("com.mysql.cj.jdbc.Driver");
                source.setUrl("jdbc:mysql://127.0.0.1:3306/ktg_s302_java_test"
                        + "?useUnicode=true&characterEncoding=utf8&serverTimezone=Asia/Shanghai");
                source.setUsername("root");
                source.setPassword(System.getenv("MYSQL_ROOT_PASSWORD"));
                return source;
            }
            return new EmbeddedDatabaseBuilder().setType(EmbeddedDatabaseType.H2)
                    .setName("inventory_operation;MODE=MySQL;DB_CLOSE_DELAY=-1")
                    .addScript("classpath:inventory-operation-test-schema.sql").build();
        }
        @Bean JdbcTemplate jdbcTemplate(DataSource ds) { return new JdbcTemplate(ds); }
        @Bean PlatformTransactionManager transactionManager(DataSource ds) {
            return new DataSourceTransactionManager(ds);
        }
        @Bean ObjectMapper objectMapper() { return new ObjectMapper(); }
        @Bean InventoryOperationMapper inventoryOperationMapper() { return new InventoryOperationMapper(); }
        @Bean InventoryOperationService inventoryOperationService() { return new InventoryOperationService(); }
        @Bean IWmItemRecptService itemRecptService() { return Mockito.mock(IWmItemRecptService.class); }
        @Bean IWmIssueHeaderService issueHeaderService() { return Mockito.mock(IWmIssueHeaderService.class); }
        @Bean IStorageCoreService storageCoreService() { return Mockito.mock(IStorageCoreService.class); }
    }
}
