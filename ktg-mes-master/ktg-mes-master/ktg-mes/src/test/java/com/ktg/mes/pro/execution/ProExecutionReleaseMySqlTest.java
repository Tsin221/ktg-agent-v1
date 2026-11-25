package com.ktg.mes.pro.execution;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.ktg.common.core.domain.AjaxResult;
import com.ktg.system.strategy.AutoCodeUtil;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.*;
import org.springframework.security.core.context.SecurityContextHolder;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

/** Real migrated schema and transactions; synthetic configuration is removed by exact fixture IDs. */
@EnabledIfEnvironmentVariable(named="MES_B01_MYSQL_URL", matches=".+")
class ProExecutionReleaseMySqlTest {
    JdbcTemplate db; ExecutionReleaseService service; AutoCodeUtil codes;
    long order, product, material, route, template; List<Long> processes=new ArrayList<>(); List<Long> stations=new ArrayList<>(), steps=new ArrayList<>();
    String tag="B02-"+UUID.randomUUID(); boolean safe;
    @BeforeEach void setup() throws Exception {
        DriverManagerDataSource source=new DriverManagerDataSource(System.getenv("MES_B01_MYSQL_URL"),System.getenv("MES_B01_MYSQL_USER"),System.getenv("MES_B01_MYSQL_PASSWORD"));
        source.setDriverClassName("com.mysql.cj.jdbc.Driver");db=new JdbcTemplate(source);
        assertTrue(db.queryForObject("select database()",String.class).matches("(ktg_mes_b01_[a-z0-9_]+|ktg_mes_b04_test)"));
        assertTrue(db.queryForObject("select version()",String.class).startsWith("5.7."));safe=true;
        db.update("insert into md_item(item_code,item_name,unit_of_measure,item_or_product,enable_flag) values(?,?,'EA','PRODUCT','Y')",tag,tag);
        product=id("md_item","item_id","item_code");
        db.update("insert into pro_workorder(workorder_code,workorder_name,order_source,product_id,product_code,product_name,unit_of_measure,quantity,request_date,ancestors,status,version,create_by) values(?,?,'MANUAL',?,?,?,'EA',10,NOW(),'0','PREPARE',0,'B02-test')",tag,tag,product,tag,tag);
        order=id("pro_workorder","workorder_id","workorder_code");
        db.update("insert into md_item(item_code,item_name,unit_of_measure,item_or_product,enable_flag) values(?,?,'EA','ITEM','Y')",tag+"-bom-item",tag+"-bom-item");
        material=db.queryForObject("select item_id from md_item where item_code=?",Long.class,tag+"-bom-item");
        db.update("insert into pro_workorder_bom(workorder_id,item_id,item_code,item_name,unit_of_measure,item_or_product,quantity) values(?,?,?,?,'EA','ITEM',10)",order,material,tag+"-bom-item",tag+"-bom-item");
        db.update("insert into pro_route(route_code,route_name,enable_flag) values(?,?,'Y')",tag,tag);route=id("pro_route","route_id","route_code");
        db.update("insert into pro_route_product(route_id,item_id,item_code,item_name,unit_of_measure) values(?,?,?,?,'EA')",route,product,tag,tag);
        String[] kinds={"ASSEMBLY","INSPECTION","PACKING"};
        for(int i=0;i<3;i++) {
            db.update("insert into pro_process(process_code,process_name,enable_flag) values(?,?,'Y')",tag+i,tag); long process=db.queryForObject("select process_id from pro_process where process_code=?",Long.class,tag+i);processes.add(process);
            db.update("insert into pro_route_process(route_id,process_id,process_code,process_name,order_num,next_process_id,execution_kind) values(?,?,?, ?,?,0,?)",route,process,tag,tag,i+1,kinds[i]);
            steps.add(db.queryForObject("select record_id from pro_route_process where route_id=? and order_num=?",Long.class,route,i+1));
            db.update("insert into md_workstation(workstation_code,workstation_name,warehouse_id,location_id,area_id,process_id,enable_flag) values(?,?,1,1,1,?,'Y')",tag+i,tag,process);
            stations.add(db.queryForObject("select workstation_id from md_workstation where workstation_code=?",Long.class,tag+i));
        }
        db.update("insert into qc_template(template_code,template_name,qc_types,enable_flag) values(?,?,'FINAL','Y')",tag,tag);template=id("qc_template","template_id","template_code");
        db.update("insert into qc_template_product(template_id,item_id) values(?,?)",template,product);
        db.update("insert into qc_template_index(template_id,index_id,index_code,index_name,index_type,stander_val,unit_of_measure,threshold_min,threshold_max) values(?,1,'I','I','QUANTITATIVE',10,'mm',9,11)",template);
        codes=mock(AutoCodeUtil.class);AtomicInteger seq=new AtomicInteger();when(codes.genSerialCode(anyString(),anyString())).thenAnswer(x->tag+"-"+seq.incrementAndGet());
        ObjectMapper json=new ObjectMapper();ExecutionAccessService access=new ExecutionAccessService(new ExecutionOwnershipService(source),new com.ktg.framework.web.service.PermissionService());
        ExecutionOperationService ops=new ExecutionOperationService(ProExecutionOperationMapperTest.mapper(source),access,new DataSourceTransactionManager(source),json);
        service=new ExecutionReleaseService(source,ops,access,json,codes);login();
    }
    void login(){com.ktg.common.core.domain.entity.SysUser user=new com.ktg.common.core.domain.entity.SysUser();user.setUserId(77L);user.setUserName("B02-test");com.ktg.common.core.domain.model.LoginUser login=new com.ktg.common.core.domain.model.LoginUser(77L,1L,user,new HashSet<String>(Arrays.asList("mes:pro:execution:query","mes:pro:execution:release")));SecurityContextHolder.getContext().setAuthentication(new org.springframework.security.authentication.UsernamePasswordAuthenticationToken(login,null,Collections.emptyList()));}
    long id(String table,String id,String code){return db.queryForObject("select "+id+" from "+table+" where "+code+"=?",Long.class,tag);}
    ExecutionReleaseRequest request(){ExecutionReleaseRequest r=new ExecutionReleaseRequest();r.operationId=UUID.randomUUID().toString();r.expectedVersion=0L;r.routeId=route;r.steps=new ArrayList<>();for(int i=0;i<3;i++){ExecutionReleaseRequest.Step s=new ExecutionReleaseRequest.Step();s.routeProcessId=steps.get(i);s.workstationId=stations.get(i);if(i==1){s.templateId=template;s.ipqcType="FINAL";}r.steps.add(s);}return r;}
    int count(String table){return db.queryForObject("select count(*) from "+table+" where workorder_id=?",Integer.class,order);}
    @Test void releaseReplayAndSnapshotDoNotProduceInventory() {
        long stock=db.queryForObject("select count(*) from wm_transaction",Long.class);
        ExecutionReleaseRequest r=request();assertEquals(200,service.release(order,r).get("code"));assertEquals(200,service.release(order,r).get("code"));
        assertEquals(3,count("pro_task"));assertEquals(10,count("pro_execution_unit"));assertEquals(1,count("pro_execution_operation"));
        assertEquals(Arrays.asList("READY","WAITING","WAITING"),db.queryForList("select status from pro_task where workorder_id=? order by task_id",String.class,order));
        assertEquals(10,db.queryForObject("select count(distinct sn_id) from pro_execution_unit where workorder_id=? and unit_state='WAIT_ASSEMBLY'",Integer.class,order));
        assertEquals(0d,db.queryForObject("select quantity_produced from pro_workorder where workorder_id=?",Double.class,order));assertEquals(stock,db.queryForObject("select count(*) from wm_transaction",Long.class));
        String snapshot=db.queryForObject("select material_requirements_snapshot from pro_execution_order where workorder_id=?",String.class,order);
        db.update("update pro_workorder_bom set item_name='changed' where workorder_id=?",order);
        assertEquals(snapshot,db.queryForObject("select material_requirements_snapshot from pro_execution_order where workorder_id=?",String.class,order));
        assertEquals(409,service.release(order,request()).get("code"));
    }
    @Test void failedSnGenerationRollsBackEveryBusinessRowAndClaim(){when(codes.genSerialCode(anyString(),anyString())).thenReturn(tag+"-duplicate");AjaxResult result=service.release(order,request());assertEquals(202,result.get("code"));assertEquals(0,count("pro_task"));assertEquals(0,count("pro_execution_order"));assertEquals(0,count("pro_execution_unit"));assertEquals(0,count("pro_execution_operation"));assertEquals(0,db.queryForObject("select count(*) from wm_sn where sn_code=?",Integer.class,tag+"-duplicate"));assertEquals("PREPARE",db.queryForObject("select status from pro_workorder where workorder_id=?",String.class,order));}
    @Test void mismatchedStationAndStaleVersionLeaveNoPartialRows(){ExecutionReleaseRequest r=request();r.steps.get(0).workstationId=stations.get(1);assertEquals(400,service.release(order,r).get("code"));r=request();r.expectedVersion=3L;assertEquals(409,service.release(order,r).get("code"));assertEquals(0,count("pro_task"));assertEquals(0,count("pro_execution_operation"));}
    @Test void changedBomAfterMaterialReferenceRejectsBeforeCreatingTasks(){
        ExecutionReleaseRequest r=request();
        r.expectedBomFingerprint=(String)service.options(order).get("bomFingerprint");
        assertTrue(r.expectedBomFingerprint.startsWith("sha256:"));
        db.update("update pro_workorder_bom set quantity=quantity*2 where workorder_id=?",order);
        assertEquals(409,service.release(order,r).get("code"));
        assertEquals(0,count("pro_task"));assertEquals(0,count("pro_execution_operation"));
    }
    @Test void concurrentSameOperationOnlyAllocatesOnce() throws Exception {ExecutionReleaseRequest r=request();ExecutorService pool=Executors.newFixedThreadPool(2);Callable<AjaxResult> call=()->{login();try{return service.release(order,r);}finally{SecurityContextHolder.clearContext();}};try{Future<AjaxResult>a=pool.submit(call),b=pool.submit(call);assertEquals(200,a.get(20,TimeUnit.SECONDS).get("code"));assertEquals(200,b.get(20,TimeUnit.SECONDS).get("code"));assertEquals(3,count("pro_task"));assertEquals(10,count("pro_execution_unit"));}finally{pool.shutdownNow();}}
    @Test void invalidQuantityRouteAndTemplateRejectWithoutClaim() {
        for(double q:new double[]{0,0.5,-1}) {db.update("update pro_workorder set quantity=? where workorder_id=?",q,order);assertEquals(400,service.release(order,request()).get("code"));}
        db.update("update pro_workorder set quantity=10 where workorder_id=?",order);
        db.update("update pro_route_process set execution_kind=null where record_id=?",steps.get(0));
        assertEquals(400,service.release(order,request()).get("code"));
        db.update("update pro_route_process set execution_kind='ASSEMBLY' where record_id=?",steps.get(0));
        ExecutionReleaseRequest r=request();r.steps.get(1).ipqcType="IQC";assertEquals(400,service.release(order,r).get("code"));
        assertEquals(0,count("pro_task"));assertEquals(0,count("pro_execution_operation"));
    }
    @Test void missingQuantitativeThresholdCannotFreezeOrderAndCanBeCorrected() {
        db.update("update qc_template_index set threshold_min=null where template_id=?",template);
        ExecutionReleaseRequest r=assertInvalidTemplateLeavesDraft("阈值");
        db.update("update qc_template_index set threshold_min=9,threshold_max=null where template_id=?",template);
        assertInvalidTemplateLeavesDraft("阈值");
        db.update("update qc_template_index set threshold_max=11 where template_id=?",template);
        assertEquals(200,service.release(order,r).get("code"));
        assertEquals(200,service.release(order,r).get("code"));
        assertEquals(3,count("pro_task"));assertEquals(10,count("pro_execution_unit"));assertEquals(1,count("pro_execution_operation"));
    }
    @Test void invertedQuantitativeThresholdCannotFreezeOrder() {
        db.update("update qc_template_index set threshold_min=12 where template_id=?",template);
        assertInvalidTemplateLeavesDraft("阈值");
    }
    @Test void unsupportedIndexTypeCannotFreezeOrder() {
        db.update("update qc_template_index set index_type='UNSUPPORTED' where template_id=?",template);
        assertInvalidTemplateLeavesDraft("类型");
    }
    @Test void duplicateIndexIdentityCannotFreezeOrder() {
        db.update("insert into qc_template_index(template_id,index_id,index_code,index_name,index_type) values(?,1,'I','I','QUALITATIVE')",template);
        assertInvalidTemplateLeavesDraft("重复");
    }
    @Test void missingIndexIdentityCannotFreezeOrder() {
        db.update("update qc_template_index set index_id=0 where template_id=?",template);
        assertInvalidTemplateLeavesDraft("ID");
        db.update("update qc_template_index set index_id=1,index_name='' where template_id=?",template);
        assertInvalidTemplateLeavesDraft("名称");
        db.update("update qc_template_index set index_name='I',index_code='' where template_id=?",template);
        assertInvalidTemplateLeavesDraft("编码");
    }
    @Test void emptyTemplateCannotFreezeOrder() {
        db.update("delete from qc_template_index where template_id=?",template);
        assertInvalidTemplateLeavesDraft("指标");
    }
    @Test void qualitativeIndexWithoutThresholdAndEqualQuantitativeBoundsAreExecutable() {
        db.update("update qc_template_index set threshold_min=10,threshold_max=10 where template_id=?",template);
        db.update("insert into qc_template_index(template_id,index_id,index_code,index_name,index_type) values(?,2,'Q','Q','QUALITATIVE')",template);
        assertEquals(200,service.release(order,request()).get("code"));
        String raw=db.queryForObject("select inspection_snapshot_json from pro_execution_task where workorder_id=? and step_kind='INSPECTION'",String.class,order);
        ExecutionInspectionRecordService records=new ExecutionInspectionRecordService(db.getDataSource(),new ObjectMapper());
        ExecutionInspectionRequest r=new ExecutionInspectionRequest();
        r.taskId=1L;
        ExecutionInspectionRequest.IndexResult quantitative=new ExecutionInspectionRequest.IndexResult();quantitative.indexId=1L;quantitative.value=new java.math.BigDecimal("10");
        ExecutionInspectionRequest.IndexResult qualitative=new ExecutionInspectionRequest.IndexResult();qualitative.indexId=2L;qualitative.passed=true;
        r.indices=Arrays.asList(quantitative,qualitative);
        assertTrue(records.validate(records.snapshot(raw),r));
    }
    private ExecutionReleaseRequest assertInvalidTemplateLeavesDraft(String expectedReason) {
        Long stockRows=db.queryForObject("select count(*) from wm_material_stock",Long.class);
        java.math.BigDecimal stockTotal=db.queryForObject("select coalesce(sum(quantity_onhand),0) from wm_material_stock",java.math.BigDecimal.class);
        Long ledgerRows=db.queryForObject("select count(*) from wm_transaction",Long.class);
        ExecutionReleaseRequest r=request();
        AjaxResult result=service.release(order,r);
        assertEquals(400,result.get("code"));
        assertEquals("VALIDATION_ERROR",result.get("errorCode"));
        assertTrue(String.valueOf(result.get("msg")).contains("检验模板"));
        assertTrue(String.valueOf(result.get("msg")).contains(expectedReason),String.valueOf(result.get("msg")));
        assertEquals("PREPARE",db.queryForObject("select status from pro_workorder where workorder_id=?",String.class,order));
        assertEquals(0L,db.queryForObject("select version from pro_workorder where workorder_id=?",Long.class,order));
        assertEquals(0,count("pro_execution_order"));assertEquals(0,count("pro_task"));assertEquals(0,count("pro_execution_task"));assertEquals(0,count("pro_execution_unit"));assertEquals(0,count("pro_execution_operation"));
        assertEquals(0,db.queryForObject("select count(*) from wm_sn where sn_code like ?",Integer.class,tag+"-%"));
        assertEquals(stockRows,db.queryForObject("select count(*) from wm_material_stock",Long.class));
        assertEquals(0,stockTotal.compareTo(db.queryForObject("select coalesce(sum(quantity_onhand),0) from wm_material_stock",java.math.BigDecimal.class)));
        assertEquals(ledgerRows,db.queryForObject("select count(*) from wm_transaction",Long.class));
        return r;
    }
    @Test void missingOrNonPositiveBomQuantityCannotFreezeOrder() {
        db.update("update pro_workorder_bom set quantity=default where workorder_id=?",order);
        assertInvalidBomLeavesDraft("预计使用量");
        db.update("update pro_workorder_bom set quantity=-1 where workorder_id=?",order);
        assertInvalidBomLeavesDraft("预计使用量");
    }
    @Test void emptyBomCannotFreezeOrder() {
        db.update("delete from pro_workorder_bom where workorder_id=?",order);
        assertInvalidBomLeavesDraft("至少需要一条用料明细");
    }
    @Test void unsupportedBomItemOrUnitCannotFreezeOrder() {
        db.update("update pro_workorder_bom set item_or_product='PRODUCT' where workorder_id=?",order);
        assertInvalidBomLeavesDraft("必须为原材料");
        db.update("update pro_workorder_bom set item_or_product='ITEM',unit_of_measure='' where workorder_id=?",order);
        assertInvalidBomLeavesDraft("单位不能为空");
        db.update("update pro_workorder_bom set unit_of_measure='EA' where workorder_id=?",order);
        db.update("insert into pro_workorder_bom(workorder_id,item_id,item_code,item_name,unit_of_measure,item_or_product,quantity) values(?,?,?,?,'KG','ITEM',10)",order,material,tag+"-bom-item",tag+"-bom-item");
        assertInvalidBomLeavesDraft("单位不一致");
    }
    @Test void bomNotExactlyDivisibleAtStockPrecisionCanBeCorrectedAndReleased() {
        db.update("update pro_workorder_bom set quantity=10.01 where workorder_id=?",order);
        ExecutionReleaseRequest r=assertInvalidBomLeavesDraft("精确分摊");
        db.update("update pro_workorder_bom set quantity=10.20 where workorder_id=?",order);
        assertEquals(200,service.release(order,r).get("code"));
        assertEquals(200,service.release(order,r).get("code"));
        assertEquals(3,count("pro_task"));assertEquals(10,count("pro_execution_unit"));assertEquals(1,count("pro_execution_operation"));
        String snapshot=db.queryForObject("select material_requirements_snapshot from pro_execution_order where workorder_id=?",String.class,order);
        ExecutionMaterialRequirements requirements=ExecutionMaterialRequirements.parse(new ObjectMapper(),10,snapshot);
        assertDoesNotThrow(()->requirements.validateUnit(Collections.singletonMap(material,new java.math.BigDecimal("1.02"))));
    }
    private ExecutionReleaseRequest assertInvalidBomLeavesDraft(String expectedReason) {
        Long stockRows=db.queryForObject("select count(*) from wm_material_stock",Long.class);
        java.math.BigDecimal stockTotal=db.queryForObject("select coalesce(sum(quantity_onhand),0) from wm_material_stock",java.math.BigDecimal.class);
        Long ledgerRows=db.queryForObject("select count(*) from wm_transaction",Long.class);
        ExecutionReleaseRequest r=request();
        AjaxResult result=service.release(order,r);
        assertEquals(400,result.get("code"));
        assertEquals("VALIDATION_ERROR",result.get("errorCode"));
        assertTrue(String.valueOf(result.get("msg")).contains("检查并修改工单 BOM"));
        assertTrue(String.valueOf(result.get("msg")).contains(expectedReason));
        assertEquals("PREPARE",db.queryForObject("select status from pro_workorder where workorder_id=?",String.class,order));
        assertEquals(0L,db.queryForObject("select version from pro_workorder where workorder_id=?",Long.class,order));
        assertEquals(0,count("pro_execution_order"));assertEquals(0,count("pro_task"));
        assertEquals(0,count("pro_execution_task"));assertEquals(0,count("pro_execution_unit"));assertEquals(0,count("pro_execution_operation"));
        assertEquals(0,db.queryForObject("select count(*) from wm_sn where sn_code like ?",Integer.class,tag+"-%"));
        assertEquals(stockRows,db.queryForObject("select count(*) from wm_material_stock",Long.class));
        assertEquals(0,stockTotal.compareTo(db.queryForObject("select coalesce(sum(quantity_onhand),0) from wm_material_stock",java.math.BigDecimal.class)));
        assertEquals(ledgerRows,db.queryForObject("select count(*) from wm_transaction",Long.class));
        return r;
    }
    @Test void optionsAndDetailReadPersistedTaskAndSnIdentities() {
        assertTrue(service.options(order).toString().contains("FINAL"));
        assertEquals(200,service.release(order,request()).get("code"));
        Map<String,Object> detail=service.detail(order);
        assertEquals(3,((List<?>)detail.get("tasks")).size());assertEquals(10,((List<?>)detail.get("units")).size());
        assertEquals(10,((Map<?,?>)detail.get("summary")).get("allocated"));assertEquals(0,((Map<?,?>)detail.get("summary")).get("qualified"));
    }
    @Test void wrongCreatorOrMissingPermissionCannotReleaseOrRead() {
        db.update("update pro_workorder set create_by='someone-else' where workorder_id=?",order);
        assertEquals(403,service.release(order,request()).get("code"));
        assertThrows(ExecutionException.class,()->service.detail(order));
        db.update("update pro_workorder set create_by='B02-test' where workorder_id=?",order);
        ((com.ktg.common.core.domain.model.LoginUser)SecurityContextHolder.getContext().getAuthentication().getPrincipal()).setPermissions(Collections.emptySet());
        assertEquals(403,service.release(order,request()).get("code"));assertEquals(0,count("pro_task"));
    }
    @Test void concurrentDifferentOperationsCannotDuplicateNormalSteps() throws Exception {
        ExecutorService pool=Executors.newFixedThreadPool(2);
        Callable<AjaxResult> call=()->{login();try{return service.release(order,request());}finally{SecurityContextHolder.clearContext();}};
        try{Future<AjaxResult>a=pool.submit(call),b=pool.submit(call);List<Object> results=Arrays.asList(a.get(20,TimeUnit.SECONDS).get("code"),b.get(20,TimeUnit.SECONDS).get("code"));assertTrue(results.contains(200));assertTrue(results.contains(409));assertEquals(3,count("pro_task"));assertEquals(10,count("pro_execution_unit"));}finally{pool.shutdownNow();}
    }
    @Test void routeKindMapperPersistsAndClearsHistoricalNull() throws Exception {
        org.mybatis.spring.SqlSessionFactoryBean factory=new org.mybatis.spring.SqlSessionFactoryBean();
        factory.setDataSource(db.getDataSource());factory.setTypeAliasesPackage("com.ktg.mes.pro.domain");
        factory.setMapperLocations(new org.springframework.core.io.ClassPathResource("mapper/pro/ProRouteProcessMapper.xml"));
        com.ktg.mes.pro.mapper.ProRouteProcessMapper mapper=new org.mybatis.spring.SqlSessionTemplate(factory.getObject()).getMapper(com.ktg.mes.pro.mapper.ProRouteProcessMapper.class);
        com.ktg.mes.pro.domain.ProRouteProcess step=mapper.selectProRouteProcessByRecordId(steps.get(0));
        assertEquals("ASSEMBLY",step.getExecutionKind());step.setExecutionKind(null);assertEquals(1,mapper.updateProRouteProcess(step));
        assertNull(mapper.selectProRouteProcessByRecordId(steps.get(0)).getExecutionKind());
        step.setExecutionKind("ASSEMBLY");mapper.updateProRouteProcess(step);assertEquals("ASSEMBLY",mapper.selectProRouteProcessByRecordId(steps.get(0)).getExecutionKind());
    }
    @AfterEach void cleanup(){SecurityContextHolder.clearContext();if(!safe)return;db.update("delete from pro_execution_operation where workorder_id=?",order);db.update("delete from pro_execution_unit where workorder_id=?",order);db.update("delete from wm_sn where sn_code like ?",tag+"-%");db.update("delete from pro_execution_task where workorder_id=?",order);db.update("delete from pro_task where workorder_id=?",order);db.update("delete from pro_execution_order where workorder_id=?",order);db.update("delete from pro_workorder_bom where workorder_id=?",order);db.update("delete from pro_workorder where workorder_id=?",order);db.update("delete from qc_template_index where template_id=?",template);db.update("delete from qc_template_product where template_id=?",template);db.update("delete from qc_template where template_id=?",template);for(Long s:stations)db.update("delete from md_workstation where workstation_id=?",s);db.update("delete from pro_route_process where route_id=?",route);db.update("delete from pro_route_product where route_id=?",route);db.update("delete from pro_route where route_id=?",route);for(Long p:processes)db.update("delete from pro_process where process_id=?",p);db.update("delete from md_item where item_id in (?,?)",product,material);}
}
