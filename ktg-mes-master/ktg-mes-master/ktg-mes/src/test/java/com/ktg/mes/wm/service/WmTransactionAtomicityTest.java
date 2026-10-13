package com.ktg.mes.wm.service;

import com.ktg.mes.md.domain.MdItem;
import com.ktg.mes.md.mapper.MdItemMapper;
import com.ktg.mes.wm.domain.WmTransaction;
import com.ktg.mes.wm.domain.WmMaterialStock;
import com.ktg.mes.wm.domain.WmWarehouse;
import com.ktg.mes.wm.domain.WmStorageLocation;
import com.ktg.mes.wm.domain.WmStorageArea;
import com.ktg.mes.wm.domain.tx.ItemRecptTxBean;
import com.ktg.mes.wm.domain.tx.IssueTxBean;
import com.ktg.mes.wm.mapper.InventoryOperationMapper;
import com.ktg.mes.wm.mapper.WmMaterialStockMapper;
import com.ktg.mes.wm.mapper.WmTransactionMapper;
import com.ktg.mes.wm.service.impl.WmTransactionServiceImpl;
import com.ktg.mes.wm.service.impl.StorageCoreServiceImpl;
import com.ktg.mes.wm.service.impl.WmMaterialStockServiceImpl;
import com.ktg.common.core.domain.AjaxResult;
import com.ktg.common.core.domain.entity.SysUser;
import com.ktg.common.core.domain.model.LoginUser;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.apache.ibatis.session.SqlSessionFactory;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.mapper.MapperFactoryBean;
import org.mockito.Mockito;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Primary;
import org.springframework.core.io.support.PathMatchingResourcePatternResolver;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.test.context.ContextConfiguration;
import org.springframework.test.context.junit.jupiter.SpringExtension;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.annotation.EnableTransactionManagement;
import org.springframework.transaction.support.TransactionTemplate;

import javax.sql.DataSource;
import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.Collections;

import static org.junit.jupiter.api.Assertions.*;

@ExtendWith(SpringExtension.class)
@ContextConfiguration(classes = WmTransactionAtomicityTest.Config.class)
class WmTransactionAtomicityTest {
    @Autowired private IWmTransactionService service;
    @Autowired private IWmMaterialStockService stockService;
    @Autowired @Qualifier("secondTransactionService") private IWmTransactionService secondService;
    @Autowired private JdbcTemplate jdbc;
    @Autowired private PlatformTransactionManager transactionManager;
    @Autowired private InventoryOperationService operationService;
    @Autowired private IWmItemRecptService receiptLines;
    @Autowired private IWmIssueHeaderService issueLines;
    @Autowired private IWmWarehouseService warehouses;
    @Autowired private IWmStorageLocationService locations;
    @Autowired private IWmStorageAreaService areas;

    @BeforeEach
    void reset() {
        Assumptions.assumeTrue("ktg_s304_atomicity_test".equals(System.getenv("KTG_S304_MYSQL_DB")));
        Mockito.reset(receiptLines, issueLines, warehouses, locations, areas);
        jdbc.execute("DELETE FROM wm_inventory_operation_transaction");
        jdbc.execute("DELETE FROM wm_inventory_executed_document");
        jdbc.execute("DELETE FROM wm_inventory_operation");
        jdbc.execute("DELETE FROM wm_transaction");
        jdbc.execute("DELETE FROM wm_material_stock");
        jdbc.execute("DELETE FROM wm_item_recpt");
        jdbc.execute("DELETE FROM wm_issue_header");
        jdbc.execute("CREATE TABLE IF NOT EXISTS wm_warehouse (warehouse_id BIGINT PRIMARY KEY, warehouse_code VARCHAR(64), warehouse_name VARCHAR(64))");
        jdbc.execute("CREATE TABLE IF NOT EXISTS wm_storage_location (location_id BIGINT PRIMARY KEY, warehouse_id BIGINT, location_code VARCHAR(64), location_name VARCHAR(64))");
        jdbc.execute("CREATE TABLE IF NOT EXISTS wm_storage_area (area_id BIGINT PRIMARY KEY, location_id BIGINT, area_code VARCHAR(64), area_name VARCHAR(64))");
        jdbc.update("INSERT INTO wm_warehouse (warehouse_id, warehouse_code, warehouse_name) VALUES (202, 'XBK_VIRTUAL', '线边库') ON DUPLICATE KEY UPDATE warehouse_code=VALUES(warehouse_code)");
        jdbc.update("INSERT INTO wm_storage_location (location_id, warehouse_id, location_code, location_name) VALUES (302, 202, 'XBKKQ_VIRTUAL', '线边库区') ON DUPLICATE KEY UPDATE warehouse_id=VALUES(warehouse_id)");
        jdbc.update("INSERT INTO wm_storage_area (area_id, location_id, area_code, area_name) VALUES (402, 302, 'XBKKW_VIRTUAL', '线边库位') ON DUPLICATE KEY UPDATE location_id=VALUES(location_id)");
        LoginUser login = new LoginUser();
        SysUser user = new SysUser();
        user.setUserName("s304-operator");
        login.setUser(user);
        login.setUserId(704L);
        SecurityContextHolder.getContext().setAuthentication(
                new UsernamePasswordAuthenticationToken(login, null, Collections.emptyList()));
    }

    private WmTransaction transaction(int flag, String quantity, long documentId) {
        WmTransaction tx = new WmTransaction();
        tx.setTransactionType(flag > 0 ? "ITEM_RECPT" : "ITEM_ISSUE_OUT");
        tx.setTransactionFlag(flag);
        tx.setTransactionQuantity(new BigDecimal(quantity));
        tx.setItemId(101L);
        tx.setItemCode("S304-ITEM");
        tx.setUnitOfMeasure("pcs");
        tx.setBatchCode("B1");
        tx.setWarehouseId(201L);
        tx.setLocationId(301L);
        tx.setAreaId(401L);
        tx.setSourceDocType(flag > 0 ? "IR" : "ISSUE");
        tx.setSourceDocId(documentId);
        tx.setSourceDocCode("S304-" + documentId);
        tx.setSourceDocLineId(documentId);
        tx.setStorageCheckFlag(true);
        return tx;
    }

    private List<Boolean> concurrent(int flag, String quantity) throws Exception {
        ExecutorService pool = Executors.newFixedThreadPool(2);
        CountDownLatch start = new CountDownLatch(1);
        try {
            List<Future<Boolean>> futures = new ArrayList<>();
            for (long id = 1; id <= 2; id++) {
                final long documentId = id;
                futures.add(pool.submit(() -> {
                    start.await();
                    try {
                        (documentId == 1 ? service : secondService)
                                .processTransaction(transaction(flag, quantity, documentId));
                        return true;
                    } catch (RuntimeException ex) {
                        return false;
                    }
                }));
            }
            start.countDown();
            List<Boolean> result = new ArrayList<>();
            for (Future<Boolean> future : futures) result.add(future.get(15, TimeUnit.SECONDS));
            return result;
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void concurrentReceiptsCreateOneBalanceAndTwoLedgers() throws Exception {
        assertEquals(2, concurrent(1, "3.00").stream().filter(Boolean::booleanValue).count());
        assertEquals(1, jdbc.queryForObject("SELECT COUNT(*) FROM wm_material_stock", Integer.class));
        assertEquals(0, new BigDecimal("6.00").compareTo(jdbc.queryForObject(
                "SELECT quantity_onhand FROM wm_material_stock", BigDecimal.class)));
        assertEquals(2, jdbc.queryForObject("SELECT COUNT(*) FROM wm_transaction", Integer.class));
    }

    @Test
    void concurrentIssuesCannotOverdrawCheckedBalance() throws Exception {
        service.processTransaction(transaction(1, "5.00", 10));
        assertEquals(1, concurrent(-1, "4.00").stream().filter(Boolean::booleanValue).count());
        assertEquals(0, new BigDecimal("1.00").compareTo(jdbc.queryForObject(
                "SELECT quantity_onhand FROM wm_material_stock", BigDecimal.class)));
        assertEquals(2, jdbc.queryForObject("SELECT COUNT(*) FROM wm_transaction", Integer.class));
    }

    @Test
    void emptyBatchNormalizesButDifferentWarehouseStaysSeparate() {
        WmTransaction first = transaction(1, "2.00", 1);
        first.setBatchCode(null);
        service.processTransaction(first);
        WmTransaction second = transaction(1, "3.00", 2);
        second.setBatchCode("  ");
        service.processTransaction(second);
        assertEquals(1, jdbc.queryForObject("SELECT COUNT(*) FROM wm_material_stock", Integer.class));
        assertEquals(0, new BigDecimal("5.00").compareTo(jdbc.queryForObject(
                "SELECT quantity_onhand FROM wm_material_stock", BigDecimal.class)));
        WmTransaction otherWarehouse = transaction(1, "1.00", 3);
        otherWarehouse.setBatchCode(null);
        otherWarehouse.setWarehouseId(202L);
        service.processTransaction(otherWarehouse);
        assertEquals(2, jdbc.queryForObject("SELECT COUNT(*) FROM wm_material_stock", Integer.class));
    }

    @Test
    void invalidPrecisionAndDirectionDoNotWriteInventory() {
        assertThrows(RuntimeException.class, () -> service.processTransaction(transaction(1, "0.001", 1)));
        WmTransaction invalidDirection = transaction(1, "1.00", 2);
        invalidDirection.setTransactionFlag(0);
        assertThrows(RuntimeException.class, () -> service.processTransaction(invalidDirection));
        assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM wm_material_stock", Integer.class));
        assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM wm_transaction", Integer.class));
    }

    @Test
    void explicitlyUncheckedConsumptionCanCreateNegativeBalance() {
        WmTransaction consumption = transaction(-1, "2.00", 1);
        consumption.setStorageCheckFlag(false);
        service.processTransaction(consumption);
        assertEquals(0, new BigDecimal("-2.00").compareTo(jdbc.queryForObject(
                "SELECT quantity_onhand FROM wm_material_stock", BigDecimal.class)));
        assertEquals(1, jdbc.queryForObject("SELECT COUNT(*) FROM wm_transaction", Integer.class));
    }

    @Test
    void directBalanceAndLedgerWritesAreRejected() {
        assertThrows(RuntimeException.class, () -> stockService.insertWmMaterialStock(new WmMaterialStock()));
        assertThrows(RuntimeException.class, () -> stockService.updateWmMaterialStock(new WmMaterialStock()));
        assertThrows(RuntimeException.class, () -> stockService.deleteWmMaterialStockByMaterialStockId(1L));
        assertThrows(RuntimeException.class, () -> stockService.deleteWmMaterialStockByMaterialStockIds(new Long[]{1L}));
        assertThrows(RuntimeException.class, () -> service.insertWmTransaction(new WmTransaction()));
        assertThrows(RuntimeException.class, () -> service.updateWmTransaction(new WmTransaction()));
        assertThrows(RuntimeException.class, () -> service.deleteWmTransactionByTransactionId(1L));
        assertThrows(RuntimeException.class, () -> service.deleteWmTransactionByTransactionIds(new Long[]{1L}));
        assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM wm_material_stock", Integer.class));
        assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM wm_transaction", Integer.class));
    }

    @Test
    void secondLegFailureRollsBackFirstLegAndBothBalances() {
        service.processTransaction(transaction(1, "5.00", 10));
        WmTransaction outgoing = transaction(-1, "2.00", 20);
        WmTransaction badIncoming = transaction(1, "2.00", 20);
        badIncoming.setWarehouseId(202L);
        badIncoming.setSourceDocCode(new String(new char[300]).replace('\0', 'X'));
        assertThrows(RuntimeException.class, () -> new TransactionTemplate(transactionManager).execute(status -> {
            service.processTransaction(outgoing);
            service.processTransaction(badIncoming);
            return null;
        }));
        assertEquals(0, new BigDecimal("5.00").compareTo(jdbc.queryForObject(
                "SELECT quantity_onhand FROM wm_material_stock WHERE warehouse_id=201", BigDecimal.class)));
        assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM wm_material_stock WHERE warehouse_id=202", Integer.class));
        assertEquals(1, jdbc.queryForObject("SELECT COUNT(*) FROM wm_transaction", Integer.class));
    }

    @Test
    void receiptCommitsBalanceLedgerDocumentAndReceiptTogether() {
        jdbc.update("INSERT INTO wm_item_recpt(recpt_id, recpt_code, status) VALUES (1, 'S304-IR', 'APPROVED')");
        ItemRecptTxBean line = new ItemRecptTxBean();
        line.setItemId(101L);
        line.setItemCode("S304-ITEM");
        line.setUnitOfMeasure("pcs");
        line.setBatchCode("B1");
        line.setWarehouseId(201L);
        line.setLocationId(301L);
        line.setAreaId(401L);
        line.setSourceDocType("IR");
        line.setReferenceValid(true);
        line.setSourceDocId(1L);
        line.setSourceDocCode("S304-IR");
        line.setSourceDocLineId(11L);
        line.setTransactionQuantity(new BigDecimal("3.00"));
        Mockito.when(receiptLines.getTxBeans(1L)).thenReturn(Collections.singletonList(line));
        AjaxResult result = operationService.executeReceipt(1L, "04040404-0404-4040-8040-040404040404");
        assertEquals(200, result.get("code"));
        assertEquals("FINISHED", jdbc.queryForObject("SELECT status FROM wm_item_recpt WHERE recpt_id=1", String.class));
        assertEquals(0, new BigDecimal("3.00").compareTo(jdbc.queryForObject(
                "SELECT quantity_onhand FROM wm_material_stock", BigDecimal.class)));
        assertEquals(1, jdbc.queryForObject("SELECT COUNT(*) FROM wm_transaction", Integer.class));
        assertEquals(1, jdbc.queryForObject("SELECT COUNT(*) FROM wm_inventory_operation_transaction", Integer.class));
        assertEquals("succeeded", jdbc.queryForObject("SELECT operation_status FROM wm_inventory_operation", String.class));
    }

    @Test
    void twoReceiptLinesInDifferentBatchesCommitOnceAndReplay() {
        jdbc.update("INSERT INTO wm_item_recpt(recpt_id, recpt_code, status) VALUES (1, 'S305-IR', 'APPROVED')");
        ItemRecptTxBean first = new ItemRecptTxBean();
        first.setItemId(101L);
        first.setItemCode("S304-ITEM");
        first.setUnitOfMeasure("pcs");
        first.setBatchCode("B1");
        first.setWarehouseId(201L);
        first.setLocationId(301L);
        first.setAreaId(401L);
        first.setSourceDocType("IR");
        first.setSourceDocId(1L);
        first.setSourceDocCode("S305-IR");
        first.setSourceDocLineId(11L);
        first.setReferenceValid(true);
        first.setTransactionQuantity(new BigDecimal("2.00"));
        ItemRecptTxBean second = new ItemRecptTxBean();
        second.setItemId(101L);
        second.setItemCode("S304-ITEM");
        second.setUnitOfMeasure("pcs");
        second.setBatchCode("B2");
        second.setWarehouseId(201L);
        second.setLocationId(301L);
        second.setAreaId(401L);
        second.setSourceDocType("IR");
        second.setSourceDocId(1L);
        second.setSourceDocCode("S305-IR");
        second.setSourceDocLineId(12L);
        second.setReferenceValid(true);
        second.setTransactionQuantity(new BigDecimal("1.50"));
        Mockito.when(receiptLines.getTxBeans(1L)).thenReturn(java.util.Arrays.asList(first, second));

        String id = "54bb4ad7-00b1-4370-9319-0439cc70b67d";
        assertEquals(200, operationService.executeReceipt(1L, id).get("code"));
        assertEquals(200, operationService.executeReceipt(1L, id).get("code"));
        assertEquals("DOCUMENT_ALREADY_EXECUTED", operationService.executeReceipt(1L,
                "54bb4ad7-00b1-4370-9319-0439cc70b67e").get("errorCode"));
        assertEquals(2, jdbc.queryForObject("SELECT COUNT(*) FROM wm_material_stock", Integer.class));
        assertEquals(2, jdbc.queryForObject("SELECT COUNT(*) FROM wm_transaction WHERE source_doc_type='IR'", Integer.class));
        assertEquals(2, jdbc.queryForObject("SELECT COUNT(*) FROM wm_inventory_operation_transaction", Integer.class));
        assertEquals(0, new BigDecimal("3.50").compareTo(jdbc.queryForObject(
                "SELECT SUM(quantity_onhand) FROM wm_material_stock", BigDecimal.class)));
    }

    @Test
    void issueTargetFailureRollsBackSourceLedgerAndDocument() {
        service.processTransaction(transaction(1, "5.00", 10));
        long sourceStockId = jdbc.queryForObject("SELECT material_stock_id FROM wm_material_stock", Long.class);
        jdbc.update("INSERT INTO wm_issue_header(issue_id, issue_code, status) VALUES (2, 'S304-ISSUE', 'APPROVED')");
        IssueTxBean line = issueLine(sourceStockId);
        Mockito.when(issueLines.getTxBeans(2L)).thenReturn(Collections.singletonList(line));
        WmWarehouse invalidTarget = new WmWarehouse();
        Mockito.when(warehouses.selectWmWarehouseByWarehouseCode(Mockito.anyString())).thenReturn(invalidTarget);
        AjaxResult result = operationService.executeIssue(2L, "05050505-0505-4050-8050-050505050505");
        assertEquals("EXECUTION_FAILED", result.get("errorCode"));
        assertEquals("APPROVED", jdbc.queryForObject("SELECT status FROM wm_issue_header WHERE issue_id=2", String.class));
        assertEquals(0, new BigDecimal("5.00").compareTo(jdbc.queryForObject(
                "SELECT quantity_onhand FROM wm_material_stock WHERE material_stock_id=?", BigDecimal.class, sourceStockId)));
        assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM wm_transaction WHERE source_doc_type='ISSUE'", Integer.class));
        assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM wm_inventory_operation_transaction", Integer.class));
        assertEquals("failed", jdbc.queryForObject("SELECT operation_status FROM wm_inventory_operation", String.class));
    }

    @Test
    void issueCommitsBothLegsWithSourceAndTargetBalances() {
        service.processTransaction(transaction(1, "5.00", 10));
        long sourceStockId = jdbc.queryForObject("SELECT material_stock_id FROM wm_material_stock", Long.class);
        jdbc.update("INSERT INTO wm_issue_header(issue_id, issue_code, status) VALUES (2, 'S304-ISSUE', 'APPROVED')");
        Mockito.when(issueLines.getTxBeans(2L)).thenReturn(Collections.singletonList(issueLine(sourceStockId)));
        WmWarehouse targetWarehouse = new WmWarehouse();
        targetWarehouse.setWarehouseId(202L);
        WmStorageLocation targetLocation = new WmStorageLocation();
        targetLocation.setLocationId(302L);
        WmStorageArea targetArea = new WmStorageArea();
        targetArea.setAreaId(402L);
        Mockito.when(warehouses.selectWmWarehouseByWarehouseCode(Mockito.anyString())).thenReturn(targetWarehouse);
        Mockito.when(locations.selectWmStorageLocationByLocationCode(Mockito.anyString())).thenReturn(targetLocation);
        Mockito.when(areas.selectWmStorageAreaByAreaCode(Mockito.anyString())).thenReturn(targetArea);
        AjaxResult result = operationService.executeIssue(2L, "06060606-0606-4060-8060-060606060606");
        assertEquals(200, result.get("code"));
        assertEquals("FINISHED", jdbc.queryForObject("SELECT status FROM wm_issue_header WHERE issue_id=2", String.class));
        assertEquals(0, new BigDecimal("3.00").compareTo(jdbc.queryForObject(
                "SELECT quantity_onhand FROM wm_material_stock WHERE warehouse_id=201", BigDecimal.class)));
        assertEquals(0, new BigDecimal("2.00").compareTo(jdbc.queryForObject(
                "SELECT quantity_onhand FROM wm_material_stock WHERE warehouse_id=202", BigDecimal.class)));
        assertEquals(2, jdbc.queryForObject("SELECT COUNT(*) FROM wm_transaction WHERE source_doc_type='ISSUE'", Integer.class));
        assertEquals(2, jdbc.queryForObject("SELECT COUNT(*) FROM wm_inventory_operation_transaction", Integer.class));
        assertEquals("succeeded", jdbc.queryForObject("SELECT operation_status FROM wm_inventory_operation", String.class));
    }

    @Test
    void issueMovesTwoBatchesFromDifferentSourceLocations() {
        service.processTransaction(transaction(1, "5.00", 10));
        WmTransaction secondStock = transaction(1, "3.00", 11);
        secondStock.setBatchCode("B2");
        secondStock.setLocationId(303L);
        secondStock.setAreaId(403L);
        service.processTransaction(secondStock);
        long firstId = jdbc.queryForObject(
                "SELECT material_stock_id FROM wm_material_stock WHERE batch_code='B1'", Long.class);
        long secondId = jdbc.queryForObject(
                "SELECT material_stock_id FROM wm_material_stock WHERE batch_code='B2'", Long.class);
        jdbc.update("INSERT INTO wm_issue_header(issue_id, issue_code, status) VALUES (2, 'S306-ISSUE', 'APPROVED')");
        IssueTxBean first = issueLine(firstId);
        IssueTxBean second = issueLine(secondId);
        second.setSourceDocLineId(22L);
        second.setBatchCode("B2");
        second.setLocationId(303L);
        second.setAreaId(403L);
        second.setAvailableQuantity(new BigDecimal("3.00"));
        second.setTransactionQuantity(new BigDecimal("1.00"));
        Mockito.when(issueLines.getTxBeans(2L)).thenReturn(java.util.Arrays.asList(first, second));
        WmWarehouse warehouse = new WmWarehouse();
        warehouse.setWarehouseId(202L);
        WmStorageLocation location = new WmStorageLocation();
        location.setLocationId(302L);
        WmStorageArea area = new WmStorageArea();
        area.setAreaId(402L);
        Mockito.when(warehouses.selectWmWarehouseByWarehouseCode(Mockito.anyString())).thenReturn(warehouse);
        Mockito.when(locations.selectWmStorageLocationByLocationCode(Mockito.anyString())).thenReturn(location);
        Mockito.when(areas.selectWmStorageAreaByAreaCode(Mockito.anyString())).thenReturn(area);

        AjaxResult result = operationService.executeIssue(2L, "f7de724d-8a68-4327-9944-cb70273f02bf");
        assertEquals(200, result.get("code"));
        assertEquals(4, jdbc.queryForObject("SELECT COUNT(*) FROM wm_transaction WHERE source_doc_type='ISSUE'", Integer.class));
        assertEquals(0, new BigDecimal("3.00").compareTo(jdbc.queryForObject(
                "SELECT quantity_onhand FROM wm_material_stock WHERE material_stock_id=?", BigDecimal.class, firstId)));
        assertEquals(0, new BigDecimal("2.00").compareTo(jdbc.queryForObject(
                "SELECT quantity_onhand FROM wm_material_stock WHERE material_stock_id=?", BigDecimal.class, secondId)));
        assertEquals(2, jdbc.queryForObject("SELECT COUNT(*) FROM wm_material_stock WHERE warehouse_id=202", Integer.class));
        assertEquals(4, jdbc.queryForObject("SELECT COUNT(*) FROM wm_inventory_operation_transaction", Integer.class));
    }

    private IssueTxBean issueLine(long sourceStockId) {
        IssueTxBean line = new IssueTxBean();
        line.setReferenceValid(true);
        line.setAvailableQuantity(new BigDecimal("5.00"));
        line.setMaterialStockId(sourceStockId);
        line.setItemId(101L);
        line.setItemCode("S304-ITEM");
        line.setUnitOfMeasure("pcs");
        line.setBatchCode("B1");
        line.setWarehouseId(201L);
        line.setLocationId(301L);
        line.setAreaId(401L);
        line.setSourceDocType("ISSUE");
        line.setSourceDocId(2L);
        line.setSourceDocCode("S304-ISSUE");
        line.setSourceDocLineId(21L);
        line.setTransactionQuantity(new BigDecimal("2.00"));
        return line;
    }

    @Configuration
    @EnableTransactionManagement
    static class Config {
        @Bean DataSource dataSource() {
            DriverManagerDataSource source = new DriverManagerDataSource();
            source.setDriverClassName("com.mysql.cj.jdbc.Driver");
            source.setUrl("jdbc:mysql://127.0.0.1:3306/ktg_s304_atomicity_test?useUnicode=true&characterEncoding=utf8&serverTimezone=Asia/Shanghai");
            source.setUsername("root");
            source.setPassword(System.getenv("MYSQL_ROOT_PASSWORD"));
            return source;
        }
        @Bean PlatformTransactionManager transactionManager(DataSource source) {
            return new DataSourceTransactionManager(source);
        }
        @Bean JdbcTemplate jdbcTemplate(DataSource source) { return new JdbcTemplate(source); }
        @Bean ObjectMapper objectMapper() { return new ObjectMapper(); }
        @Bean InventoryOperationMapper inventoryOperationMapper() { return new InventoryOperationMapper(); }
        @Bean InventoryOperationService inventoryOperationService() { return new InventoryOperationService(); }
        @Bean IWmItemRecptService itemRecptService() { return Mockito.mock(IWmItemRecptService.class); }
        @Bean IWmIssueHeaderService issueHeaderService() { return Mockito.mock(IWmIssueHeaderService.class); }
        @Bean IWmWarehouseService warehouseService() { return Mockito.mock(IWmWarehouseService.class); }
        @Bean IWmStorageLocationService locationService() { return Mockito.mock(IWmStorageLocationService.class); }
        @Bean IWmStorageAreaService areaService() { return Mockito.mock(IWmStorageAreaService.class); }
        @Bean IStorageCoreService storageCoreService() { return new StorageCoreServiceImpl(); }
        @Bean SqlSessionFactory sqlSessionFactory(DataSource source) throws Exception {
            SqlSessionFactoryBean factory = new SqlSessionFactoryBean();
            factory.setDataSource(source);
            factory.setTypeAliasesPackage("com.ktg.mes.wm.domain");
            PathMatchingResourcePatternResolver resolver = new PathMatchingResourcePatternResolver();
            factory.setMapperLocations(
                    resolver.getResource("classpath:mapper/wm/WmMaterialStockMapper.xml"),
                    resolver.getResource("classpath:mapper/wm/WmTransactionMapper.xml"));
            return factory.getObject();
        }
        @Bean MapperFactoryBean<WmMaterialStockMapper> stockMapper(SqlSessionFactory factory) {
            MapperFactoryBean<WmMaterialStockMapper> bean = new MapperFactoryBean<>(WmMaterialStockMapper.class);
            bean.setSqlSessionFactory(factory);
            return bean;
        }
        @Bean MapperFactoryBean<WmTransactionMapper> transactionMapper(SqlSessionFactory factory) {
            MapperFactoryBean<WmTransactionMapper> bean = new MapperFactoryBean<>(WmTransactionMapper.class);
            bean.setSqlSessionFactory(factory);
            return bean;
        }
        @Bean MdItemMapper itemMapper() {
            MdItemMapper mapper = Mockito.mock(MdItemMapper.class);
            MdItem item = new MdItem();
            item.setItemTypeId(1L);
            Mockito.when(mapper.selectMdItemById(Mockito.anyLong())).thenReturn(item);
            return mapper;
        }
        @Bean @Primary IWmTransactionService transactionService() { return new WmTransactionServiceImpl(); }
        @Bean IWmTransactionService secondTransactionService() { return new WmTransactionServiceImpl(); }
        @Bean IWmMaterialStockService stockService() { return new WmMaterialStockServiceImpl(); }
    }
}
