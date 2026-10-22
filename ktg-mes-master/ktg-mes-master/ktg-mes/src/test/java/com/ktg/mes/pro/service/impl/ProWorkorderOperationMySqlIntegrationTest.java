package com.ktg.mes.pro.service.impl;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.ktg.common.core.domain.AjaxResult;
import com.ktg.mes.pro.domain.ProWorkorder;
import com.ktg.mes.pro.domain.ProWorkorderOperation;
import com.ktg.mes.pro.mapper.ProWorkorderOperationMapper;
import com.ktg.mes.pro.service.IProWorkorderService;
import com.ktg.mes.md.domain.MdItem;
import com.ktg.mes.md.mapper.MdProductBomMapper;
import com.ktg.mes.md.service.IMdItemService;
import com.ktg.mes.pro.mapper.ProWorkorderMapper;
import com.ktg.mes.pro.mapper.ProWorkorderBomMapper;
import com.ktg.mes.pro.controller.dto.ProWorkorderOperationRequest;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.SqlSessionTemplate;
import org.springframework.core.io.ClassPathResource;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.validation.beanvalidation.LocalValidatorFactoryBean;

import javax.sql.DataSource;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

/** Optional integration against a disposable MySQL database provided via MES09_MYSQL_* environment variables. */
@EnabledIfEnvironmentVariable(named = "MES09_MYSQL_URL", matches = ".+")
class ProWorkorderOperationMySqlIntegrationTest {
    private static final String DRAFT = "{\"workorderCode\":\"MES09-MYSQL-100\",\"workorderName\":\"Sample workorder\","
            + "\"workorderType\":\"SELF\",\"orderSource\":\"MANUAL\",\"productId\":12,"
            + "\"quantity\":\"10.000\",\"requestDate\":\"2026-10-23\",\"parentId\":0}";

    private ObjectMapper json;
    private JdbcTemplate jdbc;
    private ProWorkorderOperationService service;
    private IProWorkorderService workorders;
    private AtomicInteger writes;
    private PlatformTransactionManager transactions;

    @BeforeEach
    void setup() throws Exception {
        DriverManagerDataSource dataSource = new DriverManagerDataSource(
                System.getenv("MES09_MYSQL_URL"), System.getenv("MES09_MYSQL_USER"),
                System.getenv("MES09_MYSQL_PASSWORD"));
        dataSource.setDriverClassName("com.mysql.cj.jdbc.Driver");
        DataSource db = dataSource;
        jdbc = new JdbcTemplate(db);
        String database = jdbc.queryForObject("SELECT DATABASE()", String.class);
        if (database == null || !database.startsWith("mes09_")) {
            throw new IllegalStateException("MES-09 MySQL integration test requires a disposable mes09_* database");
        }
        jdbc.update("DELETE FROM pro_workorder_operation");
        jdbc.execute("DROP TABLE IF EXISTS pro_workorder_bom");
        jdbc.execute("DROP TABLE IF EXISTS pro_workorder");
        jdbc.execute("CREATE TABLE pro_workorder (workorder_id BIGINT PRIMARY KEY,"
                + " workorder_code VARCHAR(64) NOT NULL, version BIGINT NOT NULL) ENGINE=InnoDB");

        SqlSessionFactoryBean factory = new SqlSessionFactoryBean();
        factory.setDataSource(db);
        factory.setMapperLocations(new ClassPathResource("mapper/pro/ProWorkorderOperationMapper.xml"));
        ProWorkorderOperationMapper mapper = new SqlSessionTemplate(factory.getObject())
                .getMapper(ProWorkorderOperationMapper.class);
        transactions = new DataSourceTransactionManager(db);
        json = new ObjectMapper();
        service = new ProWorkorderOperationService();
        workorders = mock(IProWorkorderService.class);
        writes = new AtomicInteger();
        ReflectionTestUtils.setField(service, "mapper", mapper);
        ReflectionTestUtils.setField(service, "workorders", workorders);
        ReflectionTestUtils.setField(service, "transactionManager", transactions);
        ReflectionTestUtils.setField(service, "objectMapper", json);
        LocalValidatorFactoryBean validator = new LocalValidatorFactoryBean();
        validator.afterPropertiesSet();
        ReflectionTestUtils.setField(service, "validator", validator);
        when(workorders.createDraftWorkorder(any())).thenAnswer(invocation -> {
            ProWorkorder order = invocation.getArgument(0);
            long id = 900_000L + writes.incrementAndGet();
            jdbc.update("INSERT INTO pro_workorder VALUES (?, ?, 0)", id, order.getWorkorderCode());
            return id;
        });
        when(workorders.selectProWorkorderByWorkorderId(any())).thenAnswer(invocation -> {
            Long id = invocation.getArgument(0);
            return jdbc.queryForObject("SELECT workorder_id, workorder_code, version FROM pro_workorder WHERE workorder_id=?",
                    (rs, rowNum) -> {
                        ProWorkorder order = new ProWorkorder();
                        order.setWorkorderId(rs.getLong("workorder_id"));
                        order.setWorkorderCode(rs.getString("workorder_code"));
                        order.setVersion(rs.getLong("version"));
                        return order;
                    }, id);
        });
    }

    @Test
    void concurrentServiceClaimsAndNewServiceInstanceReplayCommittedReceipt() throws Exception {
        ProWorkorderOperationRequest command = command(DRAFT);
        CountDownLatch enteredWrite = new CountDownLatch(1);
        CountDownLatch releaseWrite = new CountDownLatch(1);
        doAnswer(invocation -> {
            ProWorkorder order = invocation.getArgument(0);
            long id = 900_001L;
            jdbc.update("INSERT INTO pro_workorder VALUES (?, ?, 0)", id, order.getWorkorderCode());
            writes.incrementAndGet();
            enteredWrite.countDown();
            assertTrue(releaseWrite.await(10, TimeUnit.SECONDS));
            return id;
        }).when(workorders).createDraftWorkorder(any());

        ExecutorService workers = Executors.newFixedThreadPool(2);
        try {
            Future<AjaxResult> first = workers.submit(() -> service.execute(command, 7009L, "mes09-test"));
            assertTrue(enteredWrite.await(10, TimeUnit.SECONDS));
            Future<AjaxResult> second = workers.submit(() -> service.execute(command, 7009L, "mes09-test"));
            Thread.sleep(500); // Let the second MySQL connection reach the unique-key claim.
            releaseWrite.countDown();
            assertEquals(200, first.get(15, TimeUnit.SECONDS).get("code"));
            assertEquals(200, second.get(15, TimeUnit.SECONDS).get("code"));
        } finally {
            releaseWrite.countDown();
            workers.shutdownNow();
        }
        assertEquals(1, writes.get());
        assertEquals(1, jdbc.queryForObject("SELECT COUNT(*) FROM pro_workorder WHERE workorder_code='MES09-MYSQL-100'", Integer.class));
        ProWorkorderOperation receipt = (ProWorkorderOperation) service.result(command.getOperationId(), 7009L).get("data");
        assertEquals("succeeded", receipt.getOutcome());
        assertEquals(900_001L, receipt.getWorkorderId());

        ProWorkorderOperationService restartedService = new ProWorkorderOperationService();
        for (String field : new String[]{"mapper", "workorders", "transactionManager", "objectMapper", "validator"}) {
            ReflectionTestUtils.setField(restartedService, field, ReflectionTestUtils.getField(service, field));
        }
        assertEquals(200, restartedService.execute(command, 7009L, "mes09-test").get("code"));
        assertEquals("succeeded", ((ProWorkorderOperation) restartedService.result(command.getOperationId(), 7009L)
                .get("data")).getOutcome());
        assertEquals(1, writes.get());
    }

    @Test
    void realWorkorderServiceWritesWorkorderBomAndReceiptInMySql() throws Exception {
        // The database-name guard in setup runs before any DDL or cleanup. This schema
        // replacement is restricted to the disposable mes09_* database supplied to this test.
        ClassPathResource schemaResource = new ClassPathResource("pro-workorder-transaction-schema.sql");
        String mysqlSchema = new String(schemaResource.getInputStream().readAllBytes(),
                java.nio.charset.StandardCharsets.UTF_8).replace(" timestamp,", " timestamp NULL,");
        org.springframework.jdbc.datasource.init.ResourceDatabasePopulator schema =
                new org.springframework.jdbc.datasource.init.ResourceDatabasePopulator(
                        new org.springframework.core.io.ByteArrayResource(
                                mysqlSchema.getBytes(java.nio.charset.StandardCharsets.UTF_8)));
        schema.execute(jdbc.getDataSource());
        jdbc.execute("ALTER TABLE md_product_bom ADD COLUMN enable_flag CHAR(1) NULL");
        jdbc.execute("ALTER TABLE md_product_bom ADD COLUMN remark VARCHAR(500) NULL, ADD COLUMN attr1 VARCHAR(64) NULL, ADD COLUMN attr2 VARCHAR(255) NULL, ADD COLUMN attr3 INT NULL, ADD COLUMN attr4 INT NULL, ADD COLUMN create_by VARCHAR(64) NULL, ADD COLUMN create_time TIMESTAMP NULL, ADD COLUMN update_by VARCHAR(64) NULL, ADD COLUMN update_time TIMESTAMP NULL");
        jdbc.update("INSERT INTO md_item(item_id,item_code,item_name,specification,unit_of_measure) VALUES (12,'P-MES09','MES09 product','P-spec','ea')");
        jdbc.update("INSERT INTO md_product_bom(item_id,bom_item_id,bom_item_code,bom_item_name,bom_item_spec,unit_of_measure,item_or_product,quantity,enable_flag) VALUES (12,120,'M-MES09','MES09 material','M-spec','ea','ITEM',2,'Y')");

        org.mybatis.spring.SqlSessionFactoryBean factory = new SqlSessionFactoryBean();
        factory.setDataSource(jdbc.getDataSource());
        factory.setTypeAliasesPackage("com.ktg.mes.pro.domain,com.ktg.mes.md.domain");
        factory.setMapperLocations(new ClassPathResource("mapper/pro/ProWorkorderMapper.xml"),
                new ClassPathResource("mapper/pro/ProWorkorderBomMapper.xml"),
                new ClassPathResource("mapper/md/MdProductBomMapper.xml"),
                new ClassPathResource("mapper/pro/ProWorkorderOperationMapper.xml"));
        SqlSessionTemplate sessions = new SqlSessionTemplate(factory.getObject());

        ProWorkorderServiceImpl business = new ProWorkorderServiceImpl();
        ReflectionTestUtils.setField(business, "proWorkorderMapper", sessions.getMapper(ProWorkorderMapper.class));
        ReflectionTestUtils.setField(business, "proWorkorderBomMapper", sessions.getMapper(ProWorkorderBomMapper.class));
        ReflectionTestUtils.setField(business, "mdProductBomMapper", sessions.getMapper(MdProductBomMapper.class));
        IMdItemService items = mock(IMdItemService.class);
        MdItem product = new MdItem();
        product.setItemId(12L);
        product.setItemCode("P-MES09");
        product.setItemName("MES09 product");
        product.setSpecification("P-spec");
        product.setUnitOfMeasure("ea");
        when(items.selectMdItemById(12L)).thenReturn(product);
        ReflectionTestUtils.setField(business, "mdItemService", items);
        ReflectionTestUtils.setField(business, "proRouteProductService", mock(com.ktg.mes.pro.service.IProRouteProductService.class));
        ReflectionTestUtils.setField(business, "proRouteProcessService", mock(com.ktg.mes.pro.service.IProRouteProcessService.class));
        ReflectionTestUtils.setField(business, "proFeedbackService", mock(com.ktg.mes.pro.service.IProFeedbackService.class));

        ProWorkorderOperationService actualBusinessService = new ProWorkorderOperationService();
        ReflectionTestUtils.setField(actualBusinessService, "mapper", sessions.getMapper(ProWorkorderOperationMapper.class));
        ReflectionTestUtils.setField(actualBusinessService, "workorders", business);
        ReflectionTestUtils.setField(actualBusinessService, "transactionManager", transactions);
        ReflectionTestUtils.setField(actualBusinessService, "objectMapper", json);
        LocalValidatorFactoryBean validator = new LocalValidatorFactoryBean();
        validator.afterPropertiesSet();
        ReflectionTestUtils.setField(actualBusinessService, "validator", validator);

        ProWorkorderOperationRequest request = command(DRAFT.replace("MES09-MYSQL-100", "MES09-MYSQL-BOM-100"));
        AjaxResult result = actualBusinessService.execute(request, 7009L, "mes09-test");
        assertEquals(200, result.get("code"));
        ProWorkorderOperation receipt = (ProWorkorderOperation) actualBusinessService
                .result(request.getOperationId(), 7009L).get("data");
        assertEquals("succeeded", receipt.getOutcome());
        assertEquals(1, jdbc.queryForObject("SELECT COUNT(*) FROM pro_workorder WHERE workorder_id=?", Integer.class, receipt.getWorkorderId()));
        assertEquals(1, jdbc.queryForObject("SELECT COUNT(*) FROM pro_workorder_bom WHERE workorder_id=? AND item_id=120 AND quantity=20", Integer.class, receipt.getWorkorderId()));

        ProWorkorderOperationRequest replay = command(DRAFT.replace("MES09-MYSQL-100", "MES09-MYSQL-BOM-100"));
        replay.setOperationId(request.getOperationId());
        replay.setRequestDigest(request.getRequestDigest());
        assertEquals(200, actualBusinessService.execute(replay, 7009L, "mes09-test").get("code"));
        assertEquals(1, jdbc.queryForObject("SELECT COUNT(*) FROM pro_workorder WHERE workorder_id=?", Integer.class, receipt.getWorkorderId()));
        assertEquals(1, jdbc.queryForObject("SELECT COUNT(*) FROM pro_workorder_bom WHERE workorder_id=?", Integer.class, receipt.getWorkorderId()));
    }

    private ProWorkorderOperationRequest command(String draft) throws Exception {
        ProWorkorderOperationRequest command = new ProWorkorderOperationRequest();
        command.setOperationId(UUID.randomUUID().toString());
        command.setAction("CREATE_DRAFT");
        command.setDraft(json.readTree(draft));
        command.setRequestDigest(service.digest(command.getAction(), command.getDraft()));
        return command;
    }
}
