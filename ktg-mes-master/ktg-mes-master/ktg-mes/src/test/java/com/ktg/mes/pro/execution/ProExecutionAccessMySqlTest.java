package com.ktg.mes.pro.execution;

import com.ktg.common.core.domain.entity.SysUser;
import com.ktg.common.core.domain.model.LoginUser;
import com.ktg.framework.web.service.PermissionService;
import com.ktg.mes.wm.controller.WmProductProduceController;
import com.ktg.mes.wm.domain.*;
import com.ktg.mes.wm.service.IWmProductProduceLineService;
import com.ktg.mes.wm.service.IWmProductProduceService;
import com.ktg.mes.wm.service.impl.*;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.aop.aspectj.annotation.AspectJProxyFactory;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.transaction.support.TransactionTemplate;

import java.util.Arrays;
import java.util.Collections;
import java.util.HashSet;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

/** Actual isolated MySQL 5.7 joins, guards, and Spring AOP service/controller proxies. */
@EnabledIfEnvironmentVariable(named = "MES_B01_MYSQL_URL", matches = ".+")
class ProExecutionAccessMySqlTest {
    @Test
    void protectedFactsAndOriginalParentsRejectLegacyWritesWithoutTouchingHistoricalRows() {
        DriverManagerDataSource source = new DriverManagerDataSource(System.getenv("MES_B01_MYSQL_URL"),
                System.getenv("MES_B01_MYSQL_USER"), System.getenv("MES_B01_MYSQL_PASSWORD"));
        source.setDriverClassName("com.mysql.cj.jdbc.Driver");
        JdbcTemplate jdbc = new JdbcTemplate(source);
        String database = jdbc.queryForObject("SELECT DATABASE()", String.class);
        assertTrue(database != null && database.matches("(ktg_mes_b01_[a-z0-9_]+|ktg_mes_b04_test)"),
                "Requires dedicated B01 database");
        assertTrue(jdbc.queryForObject("SELECT VERSION()", String.class).startsWith("5.7."));
        String fixture = "B01-ACCESS-" + UUID.randomUUID();
        DataSourceTransactionManager manager = new DataSourceTransactionManager(source);
        ExecutionOwnershipService ownership = new ExecutionOwnershipService(source);
        LegacyExecutionGuard guard = new LegacyExecutionGuard(ownership, manager);
        try {
            new TransactionTemplate(manager).execute(status -> {
                try {
                    long order = order(jdbc, fixture);
                    long old = order(jdbc, fixture + "-old");
                    jdbc.update("INSERT INTO pro_execution_order VALUES (?,1,'R','R',1,10,NOW(3),'{}')", order);
                    long task = insert(jdbc, "INSERT INTO pro_task(task_code,task_name,workorder_id,workorder_code,workorder_name,workstation_id,workstation_code,workstation_name,process_id,item_id,item_code,item_name,unit_of_measure) VALUES (?,'T',?,?,'WO',1,'WS','WS',1,1,'P','P','EA')", fixture, order, fixture);
                    jdbc.update("INSERT INTO pro_execution_task(task_id,workorder_id,normal_step_no,process_id,process_code,process_name,step_kind,task_kind,assigned_user_id) VALUES (?,?,1,1,'P','P','ASSEMBLY','NORMAL',30)", task, order);
                    long sn = insert(jdbc, "INSERT INTO wm_sn(sn_code,item_id) VALUES (?,1)", fixture);
                    jdbc.update("INSERT INTO pro_execution_unit(sn_id,workorder_id,current_task_id,unit_state,created_at,updated_at) VALUES (?,?,?,'WAIT_ASSEMBLY',NOW(3),NOW(3))", sn, order, task);
                    login(30L, "producer", "query", "report");
                    ExecutionAccessService access = new ExecutionAccessService(ownership, new PermissionService());
                    assertDoesNotThrow(() -> access.requireOperation(ExecutionAction.REPORT, order, task, sn));
                    assertDoesNotThrow(() -> access.requireReceipt(ExecutionAction.REPORT, order, task, sn));
                    login(31L, "outsider", "query", "report");
                    assertThrows(ExecutionException.class,
                            () -> access.requireOperation(ExecutionAction.REPORT, order, task, sn));

                    long feedback = insert(jdbc, "INSERT INTO pro_feedback(feedback_type,workstation_id,workorder_id,process_id,task_id,item_id,item_code,item_name) VALUES ('TEST',1,?,1,?,1,'P','P')", order, task);
                    long stock = insert(jdbc, "INSERT INTO wm_material_stock(item_id,warehouse_id,quantity_onhand,workorder_id,workorder_code,unit_of_measure,batch_code) VALUES(1,1,10,?,?, 'EA',?)", order, fixture, fixture);
                    long oldStock = insert(jdbc, "INSERT INTO wm_material_stock(item_id,warehouse_id,quantity_onhand,workorder_id,workorder_code,unit_of_measure,batch_code) VALUES(1,1,10,?,?, 'EA',?)", old, fixture+"-old", fixture+"-old");

                    // Inconsistent old headers are intentional: a protected durable fact,
                    // not just an old workorder_id, must protect the entire parent.
                    long consume = insert(jdbc, "INSERT INTO wm_item_consume(workorder_id) VALUES (?)", old);
                    long consumeLine = insert(jdbc, "INSERT INTO wm_item_consume_line(record_id,item_id,quantity_consume,material_stock_id) VALUES (?,1,1,?)", consume, stock);
                    long consumeSibling = insert(jdbc, "INSERT INTO wm_item_consume_line(record_id,item_id,quantity_consume) VALUES (?,1,1)", consume);
                    jdbc.update("INSERT INTO pro_execution_input(sn_id,task_id,feedback_id,item_id,material_stock_id,source_batch_code,consume_line_id,quantity,unit_of_measure,used_by,used_at) VALUES (?,?,?,1,?,?,?,1,'EA',30,NOW(3))", sn, task, feedback, stock, fixture, consumeLine);

                    long produce = insert(jdbc, "INSERT INTO wm_product_produce(workorder_id) VALUES (?)", old);
                    long oldProduce = insert(jdbc, "INSERT INTO wm_product_produce(workorder_id) VALUES (?)", old);
                    long produceLine = insert(jdbc, "INSERT INTO wm_product_produce_line(record_id,item_id,quantity_produce) VALUES (?,1,1)", produce);
                    long produceSibling = insert(jdbc, "INSERT INTO wm_product_produce_line(record_id,item_id,quantity_produce) VALUES (?,1,1)", produce);
                    long ipqc = insert(jdbc, "INSERT INTO qc_ipqc(ipqc_code,ipqc_type,template_id,workorder_id,workstation_id,item_id) VALUES (?,'TEST',1,?,1,1)", fixture, order);
                    jdbc.update("INSERT INTO pro_execution_inspection(sn_id,task_id,ipqc_id,round_no,produce_line_id) VALUES (?,?,?,1,?)", sn, task, ipqc, produceLine);

                    long packParent = insert(jdbc, "INSERT INTO wm_package(package_code,package_date) VALUES (?,NOW())", fixture);
                    long packChild = insert(jdbc, "INSERT INTO wm_package(parent_id,ancestors,package_code,package_date) VALUES (?,?,?,NOW())", packParent, "0,"+packParent, fixture+"-child");
                    long packLine = insert(jdbc, "INSERT INTO wm_package_line(package_id,item_id,quantity_package,workorder_id) VALUES (?,1,1,?)", packChild, old);
                    long packSibling = insert(jdbc, "INSERT INTO wm_package_line(package_id,item_id,quantity_package,workorder_id) VALUES (?,1,1,?)", packChild, old);
                    jdbc.update("INSERT INTO pro_execution_pack(sn_id,task_id,packed_by,packed_at,package_line_id) VALUES (?,?,30,NOW(3),?)", sn, task, packLine);

                    WmItemConsumeServiceImpl consumeService = proxy(guard, new WmItemConsumeServiceImpl() {
                        @Override public int updateWmItemConsume(WmItemConsume d) { fail("protected header reached legacy mutation"); return 0; }
                    });
                    WmItemConsume consumeDto = new WmItemConsume(); consumeDto.setRecordId(consume);
                    rejected(() -> consumeService.updateWmItemConsume(consumeDto));

                    WmItemConsumeLineServiceImpl consumeLines = proxy(guard, new WmItemConsumeLineServiceImpl() {
                        @Override public int updateWmItemConsumeLine(WmItemConsumeLine d) { fail("protected sibling reached legacy mutation"); return 0; }
                    });
                    WmItemConsumeLine consumeLineDto = new WmItemConsumeLine(); consumeLineDto.setLineId(consumeSibling);
                    rejected(() -> consumeLines.updateWmItemConsumeLine(consumeLineDto));

                    // Whole-array guard should reject before the controller's first line deletion.
                    WmProductProduceController controller = new WmProductProduceController();
                    IWmProductProduceLineService oldLines = mock(IWmProductProduceLineService.class);
                    IWmProductProduceService oldHeaders = mock(IWmProductProduceService.class);
                    ReflectionTestUtils.setField(controller, "wmProductProduceLineService", oldLines);
                    ReflectionTestUtils.setField(controller, "wmProductProduceService", oldHeaders);
                    WmProductProduceController guardedController = proxy(guard, controller);
                    rejected(() -> guardedController.remove(new Long[]{oldProduce, produce}));
                    verifyNoInteractions(oldLines, oldHeaders);

                    WmProductProduceLineServiceImpl produceService = proxy(guard, new WmProductProduceLineServiceImpl() {
                        @Override public int updateWmProductProduceLine(WmProductProduceLine d) { fail("protected sibling reached legacy mutation"); return 0; }
                    });
                    WmProductProduceLine produceDto = new WmProductProduceLine(); produceDto.setLineId(produceSibling);
                    rejected(() -> produceService.updateWmProductProduceLine(produceDto));

                    WmPackageServiceImpl packages = proxy(guard, new WmPackageServiceImpl() {
                        @Override public int updateWmPackage(WmPackage d) { fail("protected ancestor reached legacy mutation"); return 0; }
                    });
                    WmPackage parentDto = new WmPackage(); parentDto.setPackageId(packParent);
                    rejected(() -> packages.updateWmPackage(parentDto));
                    WmPackageLineServiceImpl packageLines = proxy(guard, new WmPackageLineServiceImpl() {
                        @Override public int updateWmPackageLine(WmPackageLine d) { fail("protected package sibling reached legacy mutation"); return 0; }
                    });
                    WmPackageLine packDto = new WmPackageLine(); packDto.setLineId(packSibling);
                    rejected(() -> packageLines.updateWmPackageLine(packDto));

                    WmMaterialStockServiceImpl inventory = proxy(guard, new WmMaterialStockServiceImpl() {
                        @Override public int updateWmMaterialStock(WmMaterialStock d) { return 1; }
                    });
                    WmMaterialStock stockDto = new WmMaterialStock(); stockDto.setMaterialStockId(stock);
                    rejected(() -> inventory.updateWmMaterialStock(stockDto));
                    WmMaterialStock historicDto = new WmMaterialStock(); historicDto.setMaterialStockId(oldStock);
                    assertEquals(1, inventory.updateWmMaterialStock(historicDto));
                    historicDto.setWorkorderId(order);
                    rejected(() -> inventory.updateWmMaterialStock(historicDto));
                    return null;
                } finally { status.setRollbackOnly(); }
            });
            assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM pro_workorder WHERE workorder_code IN (?,?)",
                    Integer.class, fixture, fixture+"-old"));
        } finally { SecurityContextHolder.clearContext(); }
    }

    private static long order(JdbcTemplate jdbc, String code) {
        return insert(jdbc, "INSERT INTO pro_workorder(workorder_code,workorder_name,order_source,product_id,product_code,product_name,unit_of_measure,request_date,ancestors,quantity,status,create_by) VALUES (?,'guard-test','MANUAL',1,'P','P','EA',NOW(),'0',1,'PREPARE','creator')", code);
    }

    private static long insert(JdbcTemplate jdbc, String sql, Object... values) {
        assertEquals(1, jdbc.update(sql, values));
        return jdbc.queryForObject("SELECT LAST_INSERT_ID()", Long.class);
    }

    private static void rejected(org.junit.jupiter.api.function.Executable write) {
        ExecutionException error = assertThrows(ExecutionException.class, write);
        assertEquals("LEGACY_WRITE_PROTECTED", error.getErrorCode());
    }

    private static <T> T proxy(LegacyExecutionGuard guard, T target) {
        AspectJProxyFactory factory = new AspectJProxyFactory(target);
        factory.setProxyTargetClass(true);
        factory.addAspect(guard);
        return factory.getProxy();
    }

    private static void login(long id, String username, String... suffixes) {
        SysUser user = new SysUser(); user.setUserName(username);
        HashSet<String> permissions = new HashSet<>();
        Arrays.stream(suffixes).forEach(s -> permissions.add("mes:pro:execution:" + s));
        SecurityContextHolder.getContext().setAuthentication(new UsernamePasswordAuthenticationToken(
                new LoginUser(id, 1L, user, permissions), null, Collections.emptyList()));
    }
}
