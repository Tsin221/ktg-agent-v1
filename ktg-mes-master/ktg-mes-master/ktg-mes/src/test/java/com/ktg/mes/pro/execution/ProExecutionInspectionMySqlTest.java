package com.ktg.mes.pro.execution;
import com.ktg.common.core.domain.model.LoginUser;
import com.ktg.mes.wm.domain.WmTransaction;
import com.ktg.mes.wm.domain.tx.ProductProductTxBean;
import com.ktg.mes.wm.service.IStorageCoreService;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.beans.BeanUtils;
import org.springframework.security.core.context.SecurityContextHolder;
import java.math.BigDecimal;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;
abstract class ExecutionInspectionFixture extends ExecutionConsumptionFixture {    long inspectionTask,defect,batchRule;
    ExecutionInspectionService inspections;
    boolean fault;
    @BeforeEach void quality() throws Exception {
        assertEquals(200,reports.report(task,request(0,10,group(b06,0,2),group(b07,2,5),group(b08,5,10))).get("code"));
        inspectionTask=db.queryForObject("select task_id from pro_execution_task where workorder_id=? and step_kind='INSPECTION'",Long.class,released.order);

        db.update("update pro_task set status='STARTED' where task_id=?",inspectionTask);
        db.update("update pro_execution_task set assigned_user_id=77,started_by=77,actual_started_at=current_timestamp(3) where task_id=?",inspectionTask);
        db.update("insert into qc_defect(defect_code,defect_name,index_type,defect_level) values(?,?,'QUANTITATIVE','MAJ')",released.tag,released.tag);
        defect=db.queryForObject("select defect_id from qc_defect where defect_code=?",Long.class,released.tag);
        qualityLogin();
        IStorageCoreService store=mock(IStorageCoreService.class);
        doAnswer(call-> {
            List<ProductProductTxBean> lines=call.getArgument(0);for(ProductProductTxBean line:lines) {
                WmTransaction t=new WmTransaction();BeanUtils.copyProperties(line,t);t.setTransactionType("PRODUCT_PRODUCE");t.setTransactionFlag(1);t.setTransactionDate(new Date());Map<String,Object> dim=db.queryForMap("select warehouse_id,warehouse_code,warehouse_name,location_id,location_code,location_name,area_id,area_code,area_name from wm_material_stock where material_stock_id=?",b07);t.setWarehouseId(((Number)dim.get("warehouse_id")).longValue());t.setWarehouseCode((String)dim.get("warehouse_code"));t.setWarehouseName((String)dim.get("warehouse_name"));t.setLocationId(((Number)dim.get("location_id")).longValue());t.setLocationCode((String)dim.get("location_code"));t.setLocationName((String)dim.get("location_name"));t.setAreaId(((Number)dim.get("area_id")).longValue());t.setAreaCode((String)dim.get("area_code"));t.setAreaName((String)dim.get("area_name"));ledger.processTransaction(t);if(fault)throw new IllegalStateException("injected after ledger");
            }
            return null;
        }
        ).when(store).processProductProduce(anyList());
        inspections=new ExecutionInspectionService(source,operations,ownership,new ExecutionInspectionRecordService(source,json),new ExecutionQualifiedOutputService(source,store,batchCodes()),json);
    }
    com.ktg.system.strategy.AutoCodeUtil batchCodes() throws Exception {
        org.mybatis.spring.SqlSessionFactoryBean factory=new org.mybatis.spring.SqlSessionFactoryBean();
        factory.setDataSource(source);factory.setTypeAliasesPackage("com.ktg.common.core.domain.entity");
        factory.setMapperLocations(new org.springframework.core.io.ClassPathResource("mapper/system/SysAutoCodeResultMapper.xml"));
        com.ktg.system.service.impl.SysAutoCodeResultServiceImpl results=new com.ktg.system.service.impl.SysAutoCodeResultServiceImpl();
        org.springframework.test.util.ReflectionTestUtils.setField(results,"sysAutoCodeResultMapper",new org.mybatis.spring.SqlSessionTemplate(factory.getObject()).getMapper(com.ktg.system.mapper.SysAutoCodeResultMapper.class));
        batchRule=710000000000L+Math.abs(UUID.randomUUID().getLeastSignificantBits()%1000000000L);
        com.ktg.common.core.domain.entity.SysAutoCodeRule rule=new com.ktg.common.core.domain.entity.SysAutoCodeRule();
        rule.setRuleId(batchRule);rule.setRuleCode("BATCH_CODE");rule.setIsPadded("N");
        com.ktg.common.core.domain.entity.SysAutoCodePart part=new com.ktg.common.core.domain.entity.SysAutoCodePart();
        part.setRuleId(batchRule);part.setPartType("FIXCHAR");part.setFixCharacter("BAT");
        com.ktg.common.core.domain.entity.SysAutoCodePart date=new com.ktg.common.core.domain.entity.SysAutoCodePart();date.setRuleId(batchRule);date.setPartType("NOWDATE");date.setDateFormat("yyyyMMdd");
        com.ktg.system.service.IAutoCodeRuleService rules=mock(com.ktg.system.service.IAutoCodeRuleService.class);
        when(rules.getOne("BATCH_CODE")).thenReturn(rule);
        com.ktg.system.service.IAutoCodePartService parts=mock(com.ktg.system.service.IAutoCodePartService.class);
        when(parts.listPart(any())).thenReturn(Arrays.asList(part,date));
        com.ktg.system.strategy.PartTypeSerialNoHandler serial=new com.ktg.system.strategy.PartTypeSerialNoHandler();
        org.springframework.test.util.ReflectionTestUtils.setField(serial,"iAutoCodeResultService",results);
        com.ktg.system.strategy.PartTypeHandler handler=mock(com.ktg.system.strategy.PartTypeHandler.class);
        when(handler.choiceExecute(any())).thenAnswer(call->{com.ktg.common.core.domain.entity.SysAutoCodePart selected=call.getArgument(0);return "FIXCHAR".equals(selected.getPartType())?new com.ktg.system.strategy.PartTypeFixCharHandler().partHandle(selected):new com.ktg.system.strategy.PartTypeNowDateHandler().partHandle(selected);});
        com.ktg.system.strategy.AutoCodeUtil code=new com.ktg.system.strategy.AutoCodeUtil();
        com.ktg.system.mapper.SysAutoCodeRuleMapper locks=mock(com.ktg.system.mapper.SysAutoCodeRuleMapper.class);
        when(locks.lockRule("BATCH_CODE")).thenReturn(batchRule);
        org.springframework.test.util.ReflectionTestUtils.setField(code,"ruleLocks",locks);
        org.springframework.test.util.ReflectionTestUtils.setField(code,"iAutoCodeRuleService",rules);
        org.springframework.test.util.ReflectionTestUtils.setField(code,"iAutoCodePartService",parts);
        org.springframework.test.util.ReflectionTestUtils.setField(code,"iAutoCodeResultService",results);
        org.springframework.test.util.ReflectionTestUtils.setField(code,"partTypeHandler",handler);
        return code;
    }
    void qualityLogin() {
        login();
        ((LoginUser)SecurityContextHolder.getContext().getAuthentication().getPrincipal()).getPermissions().add("mes:pro:execution:inspect");
    }
    ExecutionInspectionRequest check(boolean pass) {
        ExecutionInspectionRequest r=new ExecutionInspectionRequest();
        r.operationId=UUID.randomUUID().toString();
        r.taskId=inspectionTask;
        ExecutionInspectionRequest.IndexResult i=new ExecutionInspectionRequest.IndexResult();
        i.indexId=1L;
        i.value=new BigDecimal(pass?"10":"12");
        if(!pass) {
            i.defectId=defect;
            i.reason="dimension out of range";
        }
        r.indices=Collections.singletonList(i);
        return r;
    }
    @AfterEach void qualityCleanup() {
        if(db==null||!released.safe)return;
        db.update("delete from sys_auto_code_result where rule_id=?",batchRule);
        db.update("update pro_execution_task set source_disposition_id=null where workorder_id=? and task_kind='REWORK'",released.order);
        db.update("update pro_execution_inspection set source_disposition_id=null where task_id=?",inspectionTask);
        db.update("delete d from pro_execution_disposition d join pro_execution_unit u on u.sn_id=d.sn_id where u.workorder_id=?",released.order);
        db.update("delete from pro_execution_inspection where task_id=?",inspectionTask);
        db.update("delete d from qc_defect_record d join qc_ipqc q on q.ipqc_id=d.qc_id where d.qc_type='IPQC' and q.workorder_id=?",released.order);
        db.update("delete l from qc_ipqc_line l join qc_ipqc q on q.ipqc_id=l.ipqc_id where q.workorder_id=?",released.order);
        db.update("delete from qc_ipqc where workorder_id=?",released.order);
        db.update("delete from wm_transaction where workorder_id=? and transaction_type='PRODUCT_PRODUCE'",released.order);
        db.update("delete from wm_material_stock where item_id=?",released.product);
        db.update("delete l from wm_product_produce_line l join wm_product_produce p on p.record_id=l.record_id where p.workorder_id=?",released.order);
        db.update("delete from wm_product_produce where workorder_id=?",released.order);
        db.update("delete from qc_defect where defect_id=?",defect);
    }
}

@EnabledIfEnvironmentVariable(named="MES_B01_MYSQL_URL",matches=".+") class ProExecutionInspectionMySqlTest extends ExecutionInspectionFixture {
    @Test void nineAcceptOneFailureAndReplayProducesExactlyNine() {
        for(int i=0;i<10;i++) {
            ExecutionInspectionRequest r=check(i!=2);
            assertEquals(200,inspections.inspect(sns.get(i),r).get("code"));
            assertEquals(200,inspections.inspect(sns.get(i),r).get("code"));
        }
        assertEquals(9,countUnits("WAIT_PACKING"));
        assertEquals(1,countUnits("WAIT_DISPOSITION"));
        assertEquals(0,countUnits("SCRAPPED"));
        assertEquals(9,db.queryForObject("select count(*) from wm_transaction where workorder_id=? and transaction_type='PRODUCT_PRODUCE'",Integer.class,released.order));
        assertEquals(10,db.queryForObject("select count(*) from qc_ipqc where workorder_id=?",Integer.class,released.order));
    }
    @Test void inventoryFailureRollsBackQualityOutputAndReceipt() {
        fault=true;
        assertEquals(202,inspections.inspect(sns.get(0),check(true)).get("code"));
        assertEquals(10,countUnits("WAIT_INSPECTION"));
        assertNull(db.queryForObject("select batch_code from pro_workorder where workorder_id=?",String.class,released.order));
        assertEquals(0,db.queryForObject("select count(*) from sys_auto_code_result where rule_id=?",Integer.class,batchRule));
        assertNull(com.ktg.system.strategy.AutoCodeUtil.threadLocal.get());

        assertEquals(0,db.queryForObject("select count(*) from qc_ipqc where workorder_id=?",Integer.class,released.order));
        assertEquals(0,db.queryForObject("select count(*) from wm_transaction where workorder_id=? and transaction_type='PRODUCT_PRODUCE'",Integer.class,released.order));
    }
    @Test void failedOutputAllocatorCanRetryOnSameThreadWithoutLeakingBatchOrCache() {
        fault=true;assertEquals(202,inspections.inspect(sns.get(0),check(true)).get("code"));
        assertNull(db.queryForObject("select batch_code from pro_workorder where workorder_id=?",String.class,released.order));
        assertNull(com.ktg.system.strategy.AutoCodeUtil.threadLocal.get());
        fault=false;assertEquals(200,inspections.inspect(sns.get(0),check(true)).get("code"));
        assertTrue(db.queryForObject("select batch_code from pro_workorder where workorder_id=?",String.class,released.order).matches("BAT[0-9]{8}"));
        assertEquals(200,inspections.inspect(sns.get(1),check(true)).get("code"));
        assertEquals(1,db.queryForObject("select gen_index from sys_auto_code_result where rule_id=?",Integer.class,batchRule));
        assertEquals(1,db.queryForObject("select count(distinct batch_code) from wm_product_produce_line l join wm_product_produce p on p.record_id=l.record_id where p.workorder_id=?",Integer.class,released.order));
    }
    @Test void competingSameSnCannotProduceTwice() throws Exception  {
        java.util.concurrent.ExecutorService pool=java.util.concurrent.Executors.newFixedThreadPool(2);
        try {
            java.util.concurrent.Future<Object> a=pool.submit(()-> {
                qualityLogin();return inspections.inspect(sns.get(0),check(true)).get("code");
            }
            );
            java.util.concurrent.Future<Object> b=pool.submit(()-> {
                qualityLogin();return inspections.inspect(sns.get(0),check(true)).get("code");
            }
            );
            List<Object> codes=Arrays.asList(a.get(),b.get());
            assertTrue(codes.contains(200));
            assertTrue(codes.contains(404)||codes.contains(409));
            assertEquals(1,db.queryForObject("select count(*) from pro_execution_inspection where sn_id=?",Integer.class,sns.get(0)));
        }
        finally {
            pool.shutdownNow();
        }
    }
    @Test void failedReinspectionCanReworkAgainAndFirstAcceptanceProducesOnlyOnce() {
        ((LoginUser)SecurityContextHolder.getContext().getAuthentication().getPrincipal()).getPermissions().addAll(Arrays.asList("mes:pro:execution:disposition","mes:pro:execution:manageAll","mes:pro:execution:rework","mes:pro:execution:start"));
        long sn=sns.get(0);
        assertEquals(200,inspections.inspect(sn,check(false)).get("code"));
        for(int round=0;round<2;round++) {
            long failed=db.queryForObject("select max(inspection_id) from pro_execution_inspection where sn_id=?",Long.class,sn);
            ExecutionDispositionRequest decision=new ExecutionDispositionRequest();
            decision.operationId=UUID.randomUUID().toString();decision.inspectionId=failed;decision.decision="REWORK";decision.reason="repair and verify";
            assertEquals(200,new ExecutionDispositionService(source,operations,json).dispose(sn,decision).get("code"));
            long rw=db.queryForObject("select e.task_id from pro_execution_task e join pro_execution_disposition d on d.disposition_id=e.source_disposition_id where d.inspection_id=?",Long.class,failed);
            db.update("update pro_execution_task set assigned_user_id=77 where task_id=?",rw);
            ExecutionTaskQueryService tasks=new ExecutionTaskQueryService(source,access,new com.ktg.framework.web.service.PermissionService());
            ExecutionTaskStartRequest start=new ExecutionTaskStartRequest();start.operationId=UUID.randomUUID().toString();
            assertEquals(200,new ExecutionTaskStartService(source,tasks,operations,json).start(rw,start).get("code"));
            assertEquals(200,new ExecutionReworkService(source,tasks,operations,json).complete(rw,UUID.randomUUID().toString()).get("code"));
            ExecutionInspectionRequest r=check(round==1);
            assertEquals(200,inspections.inspect(sn,r).get("code"));assertEquals(200,inspections.inspect(sn,r).get("code"));
        }
        assertEquals(3,db.queryForObject("select count(*) from pro_execution_inspection where sn_id=?",Integer.class,sn));
        assertEquals(1,db.queryForObject("select count(*) from pro_execution_inspection where sn_id=? and produce_line_id is not null",Integer.class,sn));
        assertEquals(1,db.queryForObject("select count(*) from wm_transaction where workorder_id=? and transaction_type='PRODUCT_PRODUCE'",Integer.class,released.order));
    }
    @Test void committedFailureReceiptSurvivesLaterDefectDictionaryRemoval() {
        ExecutionInspectionRequest r=check(false);
        assertEquals(200,inspections.inspect(sns.get(0),r).get("code"));
        db.update("delete from qc_defect where defect_id=?",defect);
        assertEquals(200,inspections.inspect(sns.get(0),r).get("code"));
        assertEquals(1,db.queryForObject("select count(*) from pro_execution_inspection where sn_id=?",Integer.class,sns.get(0)));
    }
}
