package com.ktg.mes.pro.service.impl;

import com.ktg.common.exception.ServiceException;
import com.ktg.mes.md.domain.MdItem;
import com.ktg.mes.md.domain.MdProductBom;
import com.ktg.mes.md.mapper.MdProductBomMapper;
import com.ktg.mes.md.service.IMdItemService;
import com.ktg.mes.pro.domain.ProWorkorder;
import com.ktg.mes.pro.domain.ProWorkorderBom;
import com.ktg.mes.pro.mapper.ProWorkorderBomMapper;
import com.ktg.mes.pro.mapper.ProWorkorderMapper;
import com.ktg.mes.pro.service.IProFeedbackService;
import com.ktg.mes.pro.service.IProRouteProcessService;
import com.ktg.mes.pro.service.IProRouteProductService;
import com.ktg.mes.pro.service.IProWorkorderBomService;
import com.ktg.mes.pro.service.IProWorkorderService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mockito;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.dao.DataAccessException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabaseBuilder;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabaseType;
import org.springframework.jdbc.support.GeneratedKeyHolder;
import org.springframework.jdbc.support.KeyHolder;
import org.springframework.test.context.ContextConfiguration;
import org.springframework.test.context.junit.jupiter.SpringExtension;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.annotation.EnableTransactionManagement;

import javax.sql.DataSource;
import java.math.BigDecimal;
import java.sql.PreparedStatement;
import java.sql.Statement;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

@ExtendWith(SpringExtension.class)
@ContextConfiguration(classes = ProWorkorderTransactionIntegrationTest.Config.class)
class ProWorkorderTransactionIntegrationTest
{
    @Autowired
    private IProWorkorderService service;
    @Autowired
    private IProWorkorderBomService bomService;
    @Autowired
    private JdbcTemplate jdbc;
    @Autowired
    private JdbcWorkorderBomMapper bomMapper;
    @Autowired
    private JdbcWorkorderMapper workorderMapper;

    @BeforeEach
    void resetDatabase()
    {
        bomMapper.resetFailure();
        workorderMapper.resetInsertBarrier();
        workorderMapper.resetUpdateBarrier();
        workorderMapper.resetDeleteControls();
        jdbc.update("delete from pro_workorder_bom");
        jdbc.update("delete from pro_workorder");
        jdbc.update("delete from md_product_bom");
        jdbc.update("delete from md_item");
        jdbc.update("insert into md_item(item_id,item_code,item_name,specification,unit_of_measure) values (10,'P-10','产品十','产品规格','件')");
        jdbc.update("insert into md_item(item_id,item_code,item_name,specification,unit_of_measure) values (101,'M-101','物料一','规格一','个')");
        jdbc.update("insert into md_item(item_id,item_code,item_name,specification,unit_of_measure) values (102,'M-102','物料二','规格二','个')");
        jdbc.update("insert into md_product_bom(item_id,bom_item_id,bom_item_code,bom_item_name,bom_item_spec,unit_of_measure,item_or_product,quantity) values (10,101,'M-101','物料一','规格一','个','ITEM',2)");
        jdbc.update("insert into md_product_bom(item_id,bom_item_id,bom_item_code,bom_item_name,bom_item_spec,unit_of_measure,item_or_product,quantity) values (10,102,'M-102','物料二','规格二','个','ITEM',3)");
    }

    @Test
    void mixedStatusBatchDeleteLeavesEveryWorkorderAndBomUnchanged()
    {
        Long draftId = insertExisting("WO-DELETE-DRAFT", "5", "草稿");
        Long confirmedId = insertExisting("WO-DELETE-CONFIRMED", "6", "已确认");
        jdbc.update("update pro_workorder set status='CONFIRMED' where workorder_id=?", confirmedId);

        ServiceException error = assertThrows(ServiceException.class,
                () -> service.deleteProWorkorderByWorkorderIds(new Long[]{draftId, confirmedId}));

        assertEquals(Integer.valueOf(409), error.getCode());
        assertEquals(2, count("select count(*) from pro_workorder"));
        assertEquals(4, count("select count(*) from pro_workorder_bom"));
    }

    @Test
    void batchDeleteRollsBackEarlierBomDeletesWhenLaterDeleteFails()
    {
        Long firstId = insertExisting("WO-DELETE-FIRST", "5", "第一张");
        Long secondId = insertExisting("WO-DELETE-SECOND", "6", "第二张");
        bomMapper.failOnDeleteWithDatabaseError(2);

        assertThrows(DataAccessException.class,
                () -> service.deleteProWorkorderByWorkorderIds(new Long[]{firstId, secondId}));

        assertEquals(2, count("select count(*) from pro_workorder"));
        assertEquals(4, count("select count(*) from pro_workorder_bom"));
        assertEquals(2, count("select count(*) from pro_workorder_bom where workorder_id=" + firstId));
    }

    @Test
    void finalWorkorderDatabaseFailureRestoresAllDeletedBomRows()
    {
        Long firstId = insertExisting("WO-DELETE-FINAL-1", "5", "第一张");
        Long secondId = insertExisting("WO-DELETE-FINAL-2", "6", "第二张");
        workorderMapper.failNextBatchDeleteWithDatabaseError();

        assertThrows(DataAccessException.class,
                () -> service.deleteProWorkorderByWorkorderIds(new Long[]{firstId, secondId}));

        assertEquals(2, count("select count(*) from pro_workorder"));
        assertEquals(4, count("select count(*) from pro_workorder_bom"));
    }

    @Test
    void invalidIdBatchesLeaveDatabaseUnchanged()
    {
        insertExisting("WO-DELETE-INVALID", "5", "保留");

        assertThrows(ServiceException.class, () -> service.deleteProWorkorderByWorkorderIds(null));
        assertThrows(ServiceException.class, () -> service.deleteProWorkorderByWorkorderIds(new Long[0]));
        assertThrows(ServiceException.class,
                () -> service.deleteProWorkorderByWorkorderIds(new Long[]{1L, null}));

        assertEquals(1, count("select count(*) from pro_workorder"));
        assertEquals(2, count("select count(*) from pro_workorder_bom"));
    }

    @Test
    void concurrentBomInsertCannotLeaveOrphanAfterWorkorderDelete() throws Exception
    {
        Long id = insertExisting("WO-DELETE-CONCURRENT-BOM", "5", "并发删除");
        workorderMapper.pauseBeforeNextBatchDelete();
        bomMapper.observeNextInsert();
        ExecutorService executor = Executors.newFixedThreadPool(2);
        try
        {
            Future<Integer> deletion = executor.submit(
                    () -> service.deleteProWorkorderByWorkorderIds(new Long[]{id}));
            assertTrue(workorderMapper.awaitBatchDeleteReady());

            ProWorkorderBom newBom = new ProWorkorderBom();
            newBom.setWorkorderId(id);
            newBom.setItemId(101L);
            newBom.setItemCode("M-101");
            newBom.setItemName("并发物料");
            newBom.setQuantity(BigDecimal.ONE);
            Future<Object> insertion = executor.submit(() -> insertBomOrReturnError(newBom));
            bomMapper.awaitObservedInsert(300, TimeUnit.MILLISECONDS);

            workorderMapper.releaseBatchDelete();
            assertEquals(1, deletion.get(5, TimeUnit.SECONDS));
            Object insertResult = insertion.get(5, TimeUnit.SECONDS);

            assertTrue(insertResult instanceof ServiceException);
            assertEquals(Integer.valueOf(404), ((ServiceException) insertResult).getCode());
            assertEquals(0, count("select count(*) from pro_workorder where workorder_id=" + id));
            assertEquals(0, count("select count(*) from pro_workorder_bom where workorder_id=" + id));
        }
        finally
        {
            workorderMapper.releaseBatchDelete();
            executor.shutdownNow();
        }
    }

    @Test
    void validBatchDeleteRemovesAllBomAndTreatsDuplicateIdsAsOneWorkorder()
    {
        Long firstId = insertExisting("WO-DELETE-OK-1", "5", "第一张");
        Long secondId = insertExisting("WO-DELETE-OK-2", "6", "第二张");

        int deleted = service.deleteProWorkorderByWorkorderIds(new Long[]{firstId, firstId, secondId});

        assertEquals(2, deleted);
        assertEquals(0, count("select count(*) from pro_workorder"));
        assertEquals(0, count("select count(*) from pro_workorder_bom"));
    }

    @Test
    void missingIdBatchDeleteLeavesExistingWorkorderAndBomUnchanged()
    {
        Long existingId = insertExisting("WO-DELETE-MISSING", "5", "存在");

        ServiceException error = assertThrows(ServiceException.class,
                () -> service.deleteProWorkorderByWorkorderIds(new Long[]{existingId, 404L}));

        assertEquals(Integer.valueOf(404), error.getCode());
        assertEquals(1, count("select count(*) from pro_workorder"));
        assertEquals(2, count("select count(*) from pro_workorder_bom"));
    }

    @Test
    void createRollsBackWorkorderAndFirstBomWhenSecondBomInsertFails()
    {
        bomMapper.failOnInsert(2);

        assertThrows(ServiceException.class, () -> service.createDraftWorkorder(draft("WO-ROLLBACK", "5")));

        assertEquals(0, count("select count(*) from pro_workorder where workorder_code='WO-ROLLBACK'"));
        assertEquals(0, count("select count(*) from pro_workorder_bom"));
    }

    @Test
    void rebuildFailureRestoresOldWorkorderAndOldBom()
    {
        Long id = insertExisting("WO-EDIT", "5", "原名称");
        jdbc.update("delete from pro_workorder_bom where workorder_id=?", id);
        jdbc.update("insert into pro_workorder_bom(workorder_id,item_id,item_code,item_name,quantity) values (?,?,?,?,?)",
                id, 999L, "OLD", "旧 BOM", new BigDecimal("7"));
        bomMapper.failOnInsert(2);
        ProWorkorder update = draft("WO-EDIT", "8");
        update.setWorkorderId(id);
        update.setVersion(0L);
        update.setWorkorderName("新名称");

        assertThrows(ServiceException.class, () -> service.updateDraftWorkorder(update));

        assertEquals(new BigDecimal("5.000000"), jdbc.queryForObject(
                "select quantity from pro_workorder where workorder_id=?", BigDecimal.class, id));
        assertEquals("原名称", jdbc.queryForObject(
                "select workorder_name from pro_workorder where workorder_id=?", String.class, id));
        assertEquals("OLD", jdbc.queryForObject(
                "select item_code from pro_workorder_bom where workorder_id=?", String.class, id));
        assertEquals(1, count("select count(*) from pro_workorder_bom where workorder_id=" + id));
    }

    @Test
    void nonBomEditPreservesExistingBomRow()
    {
        Long id = insertExisting("WO-NO-REBUILD", "5", "原名称");
        jdbc.update("delete from pro_workorder_bom where workorder_id=?", id);
        jdbc.update("insert into pro_workorder_bom(workorder_id,item_id,item_code,item_name,quantity) values (?,?,?,?,?)",
                id, 999L, "OLD", "旧 BOM", new BigDecimal("7"));
        Long lineId = jdbc.queryForObject(
                "select line_id from pro_workorder_bom where workorder_id=?", Long.class, id);
        ProWorkorder update = draft("WO-NO-REBUILD", "5.00");
        update.setWorkorderId(id);
        update.setVersion(0L);
        update.setWorkorderName("只改名称");

        assertEquals(1, service.updateDraftWorkorder(update));

        assertEquals("只改名称", jdbc.queryForObject(
                "select workorder_name from pro_workorder where workorder_id=?", String.class, id));
        assertEquals(lineId, jdbc.queryForObject(
                "select line_id from pro_workorder_bom where workorder_id=?", Long.class, id));
        assertEquals("OLD", jdbc.queryForObject(
                "select item_code from pro_workorder_bom where workorder_id=?", String.class, id));
    }

    @Test
    void concurrentDuplicateCodeCreatesExactlyOneWorkorder() throws Exception
    {
        jdbc.update("delete from md_product_bom");
        workorderMapper.barrierBeforeNextTwoInserts();
        ExecutorService executor = Executors.newFixedThreadPool(2);
        try
        {
            Future<Object> first = executor.submit(() -> createOrReturnError("WO-CONCURRENT"));
            Future<Object> second = executor.submit(() -> createOrReturnError("WO-CONCURRENT"));
            Object firstResult = first.get(10, TimeUnit.SECONDS);
            Object secondResult = second.get(10, TimeUnit.SECONDS);

            int successes = (firstResult instanceof Long ? 1 : 0) + (secondResult instanceof Long ? 1 : 0);
            int conflicts = (isConflict(firstResult) ? 1 : 0) + (isConflict(secondResult) ? 1 : 0);
            assertEquals(1, successes);
            assertEquals(1, conflicts);
            assertEquals(1, count("select count(*) from pro_workorder where workorder_code='WO-CONCURRENT'"));
            Long createdId = jdbc.queryForObject(
                    "select workorder_id from pro_workorder where workorder_code='WO-CONCURRENT'", Long.class);
            assertNotNull(createdId);
        }
        finally
        {
            executor.shutdownNow();
        }
    }

    @Test
    void nonDraftEditLeavesWorkorderAndBomUnchanged()
    {
        Long id = insertExisting("WO-CONFIRMED", "5", "原名称");
        jdbc.update("update pro_workorder set status='CONFIRMED' where workorder_id=?", id);
        List<BigDecimal> originalBom = jdbc.queryForList(
                "select quantity from pro_workorder_bom where workorder_id=? order by item_id", BigDecimal.class, id);
        ProWorkorder update = draft("WO-CONFIRMED", "8");
        update.setWorkorderId(id);
        update.setVersion(0L);
        update.setStatus("PREPARE");
        update.setWorkorderName("伪造编辑");

        ServiceException error = assertThrows(ServiceException.class,
                () -> service.updateDraftWorkorder(update));

        assertEquals(Integer.valueOf(409), error.getCode());
        assertEquals("原名称", jdbc.queryForObject(
                "select workorder_name from pro_workorder where workorder_id=?", String.class, id));
        assertEquals("CONFIRMED", jdbc.queryForObject(
                "select status from pro_workorder where workorder_id=?", String.class, id));
        assertEquals(0L, jdbc.queryForObject(
                "select version from pro_workorder where workorder_id=?", Long.class, id));
        assertEquals(originalBom, jdbc.queryForList(
                "select quantity from pro_workorder_bom where workorder_id=? order by item_id", BigDecimal.class, id));
    }

    @Test
    void concurrentDraftEditsAllowOneWinnerAndKeepMatchingBom() throws Exception
    {
        Long id = insertExisting("WO-VERSION", "5", "原名称");
        ProWorkorder firstUpdate = draft("WO-VERSION", "8");
        firstUpdate.setWorkorderId(id);
        firstUpdate.setVersion(0L);
        firstUpdate.setWorkorderName("客户端一");
        ProWorkorder secondUpdate = draft("WO-VERSION", "9");
        secondUpdate.setWorkorderId(id);
        secondUpdate.setVersion(0L);
        secondUpdate.setWorkorderName("客户端二");
        workorderMapper.barrierBeforeNextTwoDraftUpdates();
        ExecutorService executor = Executors.newFixedThreadPool(2);
        try
        {
            Future<Object> first = executor.submit(() -> updateOrReturnError(firstUpdate));
            Future<Object> second = executor.submit(() -> updateOrReturnError(secondUpdate));
            Object firstResult = first.get(10, TimeUnit.SECONDS);
            Object secondResult = second.get(10, TimeUnit.SECONDS);

            int successes = (Integer.valueOf(1).equals(firstResult) ? 1 : 0)
                    + (Integer.valueOf(1).equals(secondResult) ? 1 : 0);
            int conflicts = (isEditConflict(firstResult) ? 1 : 0) + (isEditConflict(secondResult) ? 1 : 0);
            assertEquals(1, successes);
            assertEquals(1, conflicts);
            assertEquals(1L, jdbc.queryForObject(
                    "select version from pro_workorder where workorder_id=?", Long.class, id));
            String winningName = jdbc.queryForObject(
                    "select workorder_name from pro_workorder where workorder_id=?", String.class, id);
            BigDecimal winningQuantity = jdbc.queryForObject(
                    "select quantity from pro_workorder where workorder_id=?", BigDecimal.class, id);
            if ("客户端一".equals(winningName))
            {
                assertEquals(0, new BigDecimal("8").compareTo(winningQuantity));
            }
            else
            {
                assertEquals("客户端二", winningName);
                assertEquals(0, new BigDecimal("9").compareTo(winningQuantity));
            }
            List<BigDecimal> bomQuantities = jdbc.queryForList(
                    "select quantity from pro_workorder_bom where workorder_id=? order by item_id", BigDecimal.class, id);
            assertEquals(2, bomQuantities.size());
            assertEquals(0, winningQuantity.multiply(new BigDecimal("2")).compareTo(bomQuantities.get(0)));
            assertEquals(0, winningQuantity.multiply(new BigDecimal("3")).compareTo(bomQuantities.get(1)));
        }
        finally
        {
            executor.shutdownNow();
        }
    }

    private Object updateOrReturnError(ProWorkorder update)
    {
        try
        {
            return service.updateDraftWorkorder(update);
        }
        catch (ServiceException e)
        {
            return e;
        }
    }

    private Object insertBomOrReturnError(ProWorkorderBom bom)
    {
        try
        {
            return bomService.insertProWorkorderBom(bom);
        }
        catch (ServiceException e)
        {
            return e;
        }
    }

    private boolean isEditConflict(Object result)
    {
        return result instanceof ServiceException
                && Integer.valueOf(409).equals(((ServiceException) result).getCode())
                && ((ServiceException) result).getMessage().contains("当前版本：1");
    }

    private Object createOrReturnError(String code)
    {
        try
        {
            return service.createDraftWorkorder(draft(code, "1"));
        }
        catch (ServiceException e)
        {
            return e;
        }
    }

    private boolean isConflict(Object result)
    {
        return result instanceof ServiceException
                && Integer.valueOf(409).equals(((ServiceException) result).getCode())
                && ((ServiceException) result).getMessage().contains("编号已存在");
    }

    private Long insertExisting(String code, String quantity, String name)
    {
        ProWorkorder workorder = draft(code, quantity);
        workorder.setWorkorderName(name);
        Long id = service.createDraftWorkorder(workorder);
        assertTrue(id > 0L);
        return id;
    }

    private ProWorkorder draft(String code, String quantity)
    {
        ProWorkorder workorder = new ProWorkorder();
        workorder.setWorkorderCode(code);
        workorder.setWorkorderName("测试工单");
        workorder.setWorkorderType("SELF");
        workorder.setOrderSource("MANUAL");
        workorder.setProductId(10L);
        workorder.setQuantity(new BigDecimal(quantity));
        workorder.setParentId(0L);
        workorder.setCreateBy("integration-test");
        workorder.setUpdateBy("integration-test");
        return workorder;
    }

    private int count(String sql)
    {
        return jdbc.queryForObject(sql, Integer.class);
    }

    @Configuration
    @EnableTransactionManagement
    static class Config
    {
        @Bean
        DataSource dataSource()
        {
            return new EmbeddedDatabaseBuilder()
                    .setType(EmbeddedDatabaseType.H2)
                    .setName("workorder_tx;MODE=MySQL;DB_CLOSE_DELAY=-1")
                    .addScript("classpath:pro-workorder-transaction-schema.sql")
                    .build();
        }

        @Bean
        JdbcTemplate jdbcTemplate(DataSource dataSource) { return new JdbcTemplate(dataSource); }

        @Bean
        PlatformTransactionManager transactionManager(DataSource dataSource)
        {
            return new DataSourceTransactionManager(dataSource);
        }

        @Bean
        JdbcWorkorderMapper workorderMapper(JdbcTemplate jdbc) { return new JdbcWorkorderMapper(jdbc); }

        @Bean
        JdbcWorkorderBomMapper workorderBomMapper(JdbcTemplate jdbc) { return new JdbcWorkorderBomMapper(jdbc); }

        @Bean
        MdProductBomMapper productBomMapper(JdbcTemplate jdbc) { return new JdbcProductBomMapper(jdbc); }

        @Bean
        IMdItemService itemService(JdbcTemplate jdbc) { return new JdbcItemService(jdbc); }

        @Bean IProRouteProductService routeProductService() { return Mockito.mock(IProRouteProductService.class); }
        @Bean IProRouteProcessService routeProcessService() { return Mockito.mock(IProRouteProcessService.class); }
        @Bean IProFeedbackService feedbackService() { return Mockito.mock(IProFeedbackService.class); }

        @Bean
        IProWorkorderService workorderService() { return new ProWorkorderServiceImpl(); }

        @Bean
        IProWorkorderBomService workorderBomService() { return new ProWorkorderBomServiceImpl(); }
    }

    static class JdbcWorkorderMapper implements ProWorkorderMapper
    {
        private final JdbcTemplate jdbc;
        private volatile CountDownLatch insertBarrier;
        private volatile CountDownLatch updateBarrier;
        private volatile CountDownLatch batchDeleteReady;
        private volatile CountDownLatch batchDeleteRelease;
        private volatile boolean failBatchDelete;
        private final RowMapper<ProWorkorder> rowMapper = (rs, rowNum) -> {
            ProWorkorder value = new ProWorkorder();
            value.setWorkorderId(rs.getLong("workorder_id"));
            value.setWorkorderCode(rs.getString("workorder_code"));
            value.setWorkorderName(rs.getString("workorder_name"));
            value.setProductId(rs.getLong("product_id"));
            value.setQuantity(rs.getBigDecimal("quantity"));
            value.setParentId(rs.getLong("parent_id"));
            value.setAncestors(rs.getString("ancestors"));
            value.setStatus(rs.getString("status"));
            value.setVersion(rs.getLong("version"));
            return value;
        };

        JdbcWorkorderMapper(JdbcTemplate jdbc) { this.jdbc = jdbc; }

        void barrierBeforeNextTwoInserts() { insertBarrier = new CountDownLatch(2); }
        void resetInsertBarrier() { insertBarrier = null; }
        void barrierBeforeNextTwoDraftUpdates() { updateBarrier = new CountDownLatch(2); }
        void resetUpdateBarrier() { updateBarrier = null; }
        void pauseBeforeNextBatchDelete()
        {
            batchDeleteReady = new CountDownLatch(1);
            batchDeleteRelease = new CountDownLatch(1);
        }
        boolean awaitBatchDeleteReady() throws InterruptedException
        {
            return batchDeleteReady.await(5, TimeUnit.SECONDS);
        }
        void releaseBatchDelete()
        {
            CountDownLatch release = batchDeleteRelease;
            if (release != null) release.countDown();
        }
        void failNextBatchDeleteWithDatabaseError() { failBatchDelete = true; }
        void resetDeleteControls()
        {
            batchDeleteReady = null;
            batchDeleteRelease = null;
            failBatchDelete = false;
        }

        @Override
        public ProWorkorder selectProWorkorderByWorkorderId(Long id)
        {
            List<ProWorkorder> rows = jdbc.query("select * from pro_workorder where workorder_id=?", rowMapper, id);
            return rows.isEmpty() ? null : rows.get(0);
        }

        @Override
        public ProWorkorder selectProWorkorderByWorkorderIdForUpdate(Long id)
        {
            List<ProWorkorder> rows = jdbc.query(
                    "select * from pro_workorder where workorder_id=? for update", rowMapper, id);
            return rows.isEmpty() ? null : rows.get(0);
        }

        @Override
        public ProWorkorder checkWorkorderCodeUnique(ProWorkorder value)
        {
            List<ProWorkorder> rows = jdbc.query("select * from pro_workorder where workorder_code=?", rowMapper,
                    value.getWorkorderCode());
            return rows.isEmpty() ? null : rows.get(0);
        }

        @Override
        public int insertProWorkorder(ProWorkorder value)
        {
            CountDownLatch barrier = insertBarrier;
            if (barrier != null)
            {
                barrier.countDown();
                try
                {
                    if (!barrier.await(5, TimeUnit.SECONDS)) throw new IllegalStateException("并发测试未就绪");
                }
                catch (InterruptedException e)
                {
                    Thread.currentThread().interrupt();
                    throw new IllegalStateException(e);
                }
            }
            KeyHolder keys = new GeneratedKeyHolder();
            int rows = jdbc.update(connection -> {
                PreparedStatement statement = connection.prepareStatement(
                        "insert into pro_workorder(workorder_code,workorder_name,workorder_type,order_source,product_id,product_code,product_name,product_spc,unit_of_measure,quantity,parent_id,ancestors,status,version,create_by,create_time) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        Statement.RETURN_GENERATED_KEYS);
                statement.setString(1, value.getWorkorderCode());
                statement.setString(2, value.getWorkorderName());
                statement.setString(3, value.getWorkorderType());
                statement.setString(4, value.getOrderSource());
                statement.setLong(5, value.getProductId());
                statement.setString(6, value.getProductCode());
                statement.setString(7, value.getProductName());
                statement.setString(8, value.getProductSpc());
                statement.setString(9, value.getUnitOfMeasure());
                statement.setBigDecimal(10, value.getQuantity());
                statement.setLong(11, value.getParentId());
                statement.setString(12, value.getAncestors());
                statement.setString(13, value.getStatus());
                statement.setLong(14, value.getVersion());
                statement.setString(15, value.getCreateBy());
                statement.setTimestamp(16, new java.sql.Timestamp(value.getCreateTime().getTime()));
                return statement;
            }, keys);
            value.setWorkorderId(keys.getKey().longValue());
            return rows;
        }

        @Override
        public int updateProWorkorder(ProWorkorder value)
        {
            return jdbc.update("update pro_workorder set workorder_code=?,workorder_name=?,product_id=?,product_code=?,product_name=?,product_spc=?,unit_of_measure=?,quantity=?,parent_id=?,ancestors=?,remark=?,update_by=?,update_time=? where workorder_id=?",
                    value.getWorkorderCode(), value.getWorkorderName(), value.getProductId(), value.getProductCode(),
                    value.getProductName(), value.getProductSpc(), value.getUnitOfMeasure(), value.getQuantity(),
                    value.getParentId(), value.getAncestors(), value.getRemark(), value.getUpdateBy(),
                    value.getUpdateTime(), value.getWorkorderId());
        }

        @Override
        public int updateDraftWorkorder(ProWorkorder value)
        {
            CountDownLatch barrier = updateBarrier;
            if (barrier != null)
            {
                barrier.countDown();
                try
                {
                    if (!barrier.await(5, TimeUnit.SECONDS)) throw new IllegalStateException("并发测试未就绪");
                }
                catch (InterruptedException e)
                {
                    Thread.currentThread().interrupt();
                    throw new IllegalStateException(e);
                }
            }
            return jdbc.update("update pro_workorder set workorder_code=?,workorder_name=?,product_id=?,product_code=?,product_name=?,product_spc=?,unit_of_measure=?,quantity=?,parent_id=?,ancestors=?,remark=?,update_by=?,update_time=?,version=version+1 where workorder_id=? and status='PREPARE' and version=?",
                    value.getWorkorderCode(), value.getWorkorderName(), value.getProductId(), value.getProductCode(),
                    value.getProductName(), value.getProductSpc(), value.getUnitOfMeasure(), value.getQuantity(),
                    value.getParentId(), value.getAncestors(), value.getRemark(), value.getUpdateBy(),
                    value.getUpdateTime(), value.getWorkorderId(), value.getVersion());
        }

        @Override public List<ProWorkorder> selectProWorkorderList(ProWorkorder value) { return Collections.emptyList(); }
        @Override public int deleteProWorkorderByWorkorderId(Long id) { return jdbc.update("delete from pro_workorder where workorder_id=?", id); }
        @Override
        public int deleteProWorkorderByWorkorderIds(Long[] ids)
        {
            CountDownLatch ready = batchDeleteReady;
            CountDownLatch release = batchDeleteRelease;
            if (ready != null && release != null)
            {
                batchDeleteReady = null;
                ready.countDown();
                try
                {
                    if (!release.await(5, TimeUnit.SECONDS)) throw new IllegalStateException("删除并发测试未释放");
                }
                catch (InterruptedException e)
                {
                    Thread.currentThread().interrupt();
                    throw new IllegalStateException(e);
                }
                batchDeleteRelease = null;
            }
            if (failBatchDelete)
            {
                jdbc.update("delete from missing_workorder_table");
            }
            int deleted = 0;
            for (Long id : ids)
            {
                deleted += jdbc.update("delete from pro_workorder where workorder_id=?", id);
            }
            return deleted;
        }
    }

    static class JdbcWorkorderBomMapper implements ProWorkorderBomMapper
    {
        private final JdbcTemplate jdbc;
        private final AtomicInteger insertCount = new AtomicInteger();
        private final AtomicInteger deleteCount = new AtomicInteger();
        private volatile int failingInsert;
        private volatile int failingDelete;
        private volatile CountDownLatch observedInsert;

        JdbcWorkorderBomMapper(JdbcTemplate jdbc) { this.jdbc = jdbc; }
        void failOnInsert(int number) { insertCount.set(0); failingInsert = number; }
        void failOnDeleteWithDatabaseError(int number) { deleteCount.set(0); failingDelete = number; }
        void observeNextInsert() { observedInsert = new CountDownLatch(1); }
        boolean awaitObservedInsert(long timeout, TimeUnit unit) throws InterruptedException
        {
            CountDownLatch observed = observedInsert;
            return observed != null && observed.await(timeout, unit);
        }
        void resetFailure()
        {
            insertCount.set(0);
            deleteCount.set(0);
            failingInsert = 0;
            failingDelete = 0;
            observedInsert = null;
        }

        @Override
        public int insertProWorkorderBom(ProWorkorderBom value)
        {
            CountDownLatch observed = observedInsert;
            if (observed != null) observed.countDown();
            if (insertCount.incrementAndGet() == failingInsert)
            {
                throw new ServiceException("模拟 BOM 写入失败");
            }
            return jdbc.update("insert into pro_workorder_bom(workorder_id,item_id,item_code,item_name,item_spc,unit_of_measure,item_or_product,quantity) values (?,?,?,?,?,?,?,?)",
                    value.getWorkorderId(), value.getItemId(), value.getItemCode(), value.getItemName(), value.getItemSpc(),
                    value.getUnitOfMeasure(), value.getItemOrProduct(), value.getQuantity());
        }

        @Override
        public int deleteProWorkorderBomByWorkorderId(Long id)
        {
            if (deleteCount.incrementAndGet() == failingDelete)
            {
                jdbc.update("delete from missing_workorder_bom_table");
            }
            return jdbc.update("delete from pro_workorder_bom where workorder_id=?", id);
        }
        @Override public ProWorkorderBom selectProWorkorderBomByLineId(Long id) { throw new UnsupportedOperationException(); }
        @Override public List<ProWorkorderBom> selectProWorkorderBomList(ProWorkorderBom value) { return Collections.emptyList(); }
        @Override public int updateProWorkorderBom(ProWorkorderBom value) { throw new UnsupportedOperationException(); }
        @Override public int deleteProWorkorderBomByLineId(Long id) { throw new UnsupportedOperationException(); }
        @Override public int deleteProWorkorderBomByLineIds(Long[] ids) { throw new UnsupportedOperationException(); }
    }

    static class JdbcProductBomMapper implements MdProductBomMapper
    {
        private final JdbcTemplate jdbc;
        JdbcProductBomMapper(JdbcTemplate jdbc) { this.jdbc = jdbc; }

        @Override
        public List<MdProductBom> selectMdProductBomList(MdProductBom query)
        {
            return jdbc.query("select * from md_product_bom where item_id=? order by bom_id", (rs, rowNum) -> {
                MdProductBom value = new MdProductBom();
                value.setBomItemId(rs.getLong("bom_item_id"));
                value.setBomItemCode(rs.getString("bom_item_code"));
                value.setBomItemName(rs.getString("bom_item_name"));
                value.setBomItemSpec(rs.getString("bom_item_spec"));
                value.setUnitOfMeasure(rs.getString("unit_of_measure"));
                value.setItemOrProduct(rs.getString("item_or_product"));
                value.setQuantity(rs.getBigDecimal("quantity"));
                return value;
            }, query.getItemId());
        }

        @Override public MdProductBom selectMdProductBomByBomId(Long id) { throw new UnsupportedOperationException(); }
        @Override public int insertMdProductBom(MdProductBom value) { throw new UnsupportedOperationException(); }
        @Override public int updateMdProductBom(MdProductBom value) { throw new UnsupportedOperationException(); }
        @Override public int deleteMdProductBomByBomId(Long id) { throw new UnsupportedOperationException(); }
        @Override public int deleteMdProductBomByBomIds(Long[] ids) { throw new UnsupportedOperationException(); }
    }

    static class JdbcItemService implements IMdItemService
    {
        private final JdbcTemplate jdbc;
        JdbcItemService(JdbcTemplate jdbc) { this.jdbc = jdbc; }

        @Override
        public MdItem selectMdItemById(Long id)
        {
            List<MdItem> rows = jdbc.query("select * from md_item where item_id=?", (rs, rowNum) -> {
                MdItem value = new MdItem();
                value.setItemId(rs.getLong("item_id"));
                value.setItemCode(rs.getString("item_code"));
                value.setItemName(rs.getString("item_name"));
                value.setSpecification(rs.getString("specification"));
                value.setUnitOfMeasure(rs.getString("unit_of_measure"));
                return value;
            }, id);
            return rows.isEmpty() ? null : rows.get(0);
        }

        @Override public List<MdItem> selectMdItemList(MdItem value) { return Collections.emptyList(); }
        @Override public List<MdItem> selectMdItemAll() { return Collections.emptyList(); }
        @Override public String checkItemCodeUnique(MdItem value) { throw new UnsupportedOperationException(); }
        @Override public String checkItemNameUnique(MdItem value) { throw new UnsupportedOperationException(); }
        @Override public int insertMdItem(MdItem value) { throw new UnsupportedOperationException(); }
        @Override public int updateMdItem(MdItem value) { throw new UnsupportedOperationException(); }
        @Override public int deleteByItemIds(Long[] ids) { throw new UnsupportedOperationException(); }
        @Override public int deleteByItemId(Long id) { throw new UnsupportedOperationException(); }
    }
}
