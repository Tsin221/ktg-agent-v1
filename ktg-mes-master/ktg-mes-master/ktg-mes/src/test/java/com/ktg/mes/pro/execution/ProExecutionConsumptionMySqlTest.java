package com.ktg.mes.pro.execution;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.ktg.common.core.domain.AjaxResult;
import com.ktg.common.core.domain.model.LoginUser;
import com.ktg.mes.md.mapper.MdItemMapper;
import com.ktg.mes.wm.domain.WmTransaction;
import com.ktg.mes.wm.mapper.WmMaterialStockMapper;
import com.ktg.mes.wm.mapper.WmTransactionMapper;
import com.ktg.mes.wm.service.IWmTransactionService;
import com.ktg.mes.wm.service.impl.WmTransactionServiceImpl;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.SqlSessionTemplate;
import org.springframework.core.io.ClassPathResource;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.transaction.support.TransactionTemplate;

import javax.sql.DataSource;
import java.math.BigDecimal;
import java.util.*;
import java.util.concurrent.*;

import static org.junit.jupiter.api.Assertions.*;

/** The same migrated B01 MySQL fixture as release, with real stock ledger mapper/strict deduction. */
abstract class ExecutionConsumptionFixture {
    final ProExecutionReleaseMySqlTest released = new ProExecutionReleaseMySqlTest();
    JdbcTemplate db;
    ExecutionReportService reports;
    ExecutionMaterialQueryService materials;
    WmTransactionServiceImpl ledger;
    TransactionTemplate tx;
    long raw, task, b06, b07, b08, unissued, source06, foreignStock, foreignOrder;
    List<Long> sns;
    final List<Long> stocks = new ArrayList<>();
    final ObjectMapper json = new ObjectMapper();
    DataSource source;
    ExecutionOwnershipService ownership;
    ExecutionAccessService access;
    ExecutionOperationService operations;

    @BeforeEach void prepare() throws Exception {
        released.setup();
        db = released.db;
        source = db.getDataSource();
        tx = new TransactionTemplate(new DataSourceTransactionManager(source));
        db.update("insert into md_item(item_code,item_name,unit_of_measure,item_or_product,enable_flag,item_type_id) values(?,?,'EA','ITEM','Y',1)", released.tag+"-raw", released.tag+"-raw");
        raw = db.queryForObject("select item_id from md_item where item_code=?",Long.class,released.tag+"-raw");
        db.update("update pro_workorder_bom set item_id=?,item_code=?,item_name=?,quantity=10 where workorder_id=?",raw,released.tag+"-raw",released.tag+"-raw",released.order);
        assertEquals(200,released.service.release(released.order,released.request()).get("code"));
        task = db.queryForObject("select task_id from pro_execution_task where workorder_id=? and step_kind='ASSEMBLY'",Long.class,released.order);
        sns = db.queryForList("select sn_id from pro_execution_unit where workorder_id=? order by sn_id",Long.class,released.order);

        // Unit tests of assignment/start separately exercise those APIs; this fixture arranges a legitimate started task.
        db.update("update pro_execution_task set assigned_user_id=77,started_by=77,actual_started_at=current_timestamp(3) where task_id=?",task);
        db.update("update pro_task set status='STARTED' where task_id=?",task);
        login();

        SqlSessionFactoryBean factory = new SqlSessionFactoryBean();
        factory.setDataSource(source); factory.setTypeAliasesPackage("com.ktg.mes.wm.domain,com.ktg.mes.md.domain");
        factory.setMapperLocations(new ClassPathResource("mapper/wm/WmMaterialStockMapper.xml"),new ClassPathResource("mapper/wm/WmTransactionMapper.xml"),new ClassPathResource("mapper/md/MdItemMapper.xml"));
        SqlSessionTemplate sessions = new SqlSessionTemplate(factory.getObject());
        ledger = new WmTransactionServiceImpl();
        ReflectionTestUtils.setField(ledger,"wmMaterialStockMapper",sessions.getMapper(WmMaterialStockMapper.class));
        ReflectionTestUtils.setField(ledger,"wmTransactionMapper",sessions.getMapper(WmTransactionMapper.class));
        ReflectionTestUtils.setField(ledger,"mdItemMapper",sessions.getMapper(MdItemMapper.class));
        ownership = new ExecutionOwnershipService(source);
        access = new ExecutionAccessService(ownership,new com.ktg.framework.web.service.PermissionService());
        operations = new ExecutionOperationService(ProExecutionOperationMapperTest.mapper(source),access,new DataSourceTransactionManager(source),json);
        materials = new ExecutionMaterialQueryService(source,ownership,access,json);
        reports = newReportService(ledger);

        b06 = issue("B06",2); b07 = issue("B07",5); b08 = issue("B08",5);
        unissued = fixtureStock("UNISSUED",virtual(),released.order,99);
        db.update("insert into pro_workorder(workorder_code,workorder_name,order_source,product_id,product_code,product_name,unit_of_measure,quantity,request_date,ancestors,status,version,create_by) values(?,?,'MANUAL',?,?,?,'EA',1,NOW(),'0','CONFIRMED',0,'B03-test')",released.tag+"-foreign",released.tag,released.product,released.tag,released.tag);
        foreignOrder=db.queryForObject("select workorder_id from pro_workorder where workorder_code=?",Long.class,released.tag+"-foreign");
        foreignStock=issue("FOREIGN",10,foreignOrder,released.tag+"-foreign");
    }

    ExecutionReportService newReportService(IWmTransactionService stockTransactions) {
        return new ExecutionReportService(source,operations,ownership,access,new ExecutionConsumptionService(source,materials,stockTransactions),json);
    }

    void login() {
        released.login();
        LoginUser actor=(LoginUser)SecurityContextHolder.getContext().getAuthentication().getPrincipal();
        actor.getUser().setNickName("B03 tester");
        actor.setPermissions(new HashSet<>(Arrays.asList("mes:pro:execution:query","mes:pro:execution:release","mes:pro:execution:report")));
    }

    private Map<String,Object> virtual() { return dimension("w.warehouse_code='XBK_VIRTUAL' and l.location_code='XBKKQ_VIRTUAL' and a.area_code='XBKKW_VIRTUAL'"); }
    private Map<String,Object> physical() { return dimension("w.warehouse_code<>'XBK_VIRTUAL'"); }
    private Map<String,Object> dimension(String fixedFilter) {
        return db.queryForMap("select w.warehouse_id,w.warehouse_code,w.warehouse_name,l.location_id,l.location_code,l.location_name,a.area_id,a.area_code,a.area_name from wm_warehouse w join wm_storage_location l on l.warehouse_id=w.warehouse_id join wm_storage_area a on a.location_id=l.location_id where "+fixedFilter+" order by a.area_id limit 1");
    }
    private long fixtureStock(String batch,Map<String,Object> dim,Long order,int balance) {
        db.update("insert into wm_material_stock(item_type_id,item_id,item_code,item_name,unit_of_measure,batch_code,warehouse_id,warehouse_code,warehouse_name,location_id,location_code,location_name,area_id,area_code,area_name,quantity_onhand,workorder_id,workorder_code) values(1,?,?,?,'EA',?,?,?,?,?,?,?,?,?,?,?,?,?)",raw,released.tag+"-raw",released.tag+"-raw",batch,dim.get("warehouse_id"),dim.get("warehouse_code"),dim.get("warehouse_name"),dim.get("location_id"),dim.get("location_code"),dim.get("location_name"),dim.get("area_id"),dim.get("area_code"),dim.get("area_name"),balance,order,order==null?null:released.tag);
        long id=db.queryForObject("select material_stock_id from wm_material_stock where item_id=? and batch_code=? and warehouse_id=?",Long.class,raw,batch,dim.get("warehouse_id"));
        stocks.add(id); return id;
    }
    private long issue(String batch,int quantity) {
        return issue(batch,quantity,released.order,released.tag);
    }
    private long issue(String batch,int quantity,long orderId,String orderCode) {
        Map<String,Object> src=physical(),dest=virtual();
        long sourceId=fixtureStock(batch,src,null,quantity+5);
        if("B06".equals(batch))source06=sourceId;
        String issueCode=released.tag+"-"+batch;
        db.update("insert into wm_issue_header(issue_code,issue_name,workorder_id,workorder_code,warehouse_id,location_id,area_id,issue_date,status) values(?,?,?,?,?,?,?,now(),'APPROVED')",issueCode,issueCode,orderId,orderCode,src.get("warehouse_id"),src.get("location_id"),src.get("area_id"));
        long issueId=db.queryForObject("select issue_id from wm_issue_header where issue_code=?",Long.class,issueCode);
        db.update("insert into wm_issue_line(issue_id,material_stock_id,item_id,item_code,item_name,unit_of_measure,quantity_issued,batch_code,warehouse_id,location_id,area_id) values(?,?,?,?,?,'EA',?,?,?,?,?)",issueId,sourceId,raw,released.tag+"-raw",released.tag+"-raw",quantity,batch,src.get("warehouse_id"),src.get("location_id"),src.get("area_id"));
        long line=db.queryForObject("select line_id from wm_issue_line where issue_id=?",Long.class,issueId);
        Long id=tx.execute(status->{
            WmTransaction out=issueTx(batch,quantity,src,issueId,issueCode,line,-1);out.setMaterialStockId(sourceId);out.setWorkorderId(orderId);out.setWorkorderCode(orderCode);ledger.processTransaction(out);
            WmTransaction in=issueTx(batch,quantity,dest,issueId,issueCode,line,1);in.setWorkorderId(orderId);in.setWorkorderCode(orderCode);in.setRelatedTransactionId(out.getTransactionId());ledger.processTransaction(in);
            db.update("update wm_issue_header set status='FINISHED' where issue_id=?",issueId);
            return in.getMaterialStockId();
        });
        assertNotNull(id);stocks.add(id);return id;
    }
    private WmTransaction issueTx(String batch,int quantity,Map<String,Object> dim,long issueId,String code,long line,int flag) {
        WmTransaction row=new WmTransaction();row.setTransactionType(flag==1?"ITEM_ISSUE_IN":"ITEM_ISSUE_OUT");row.setTransactionFlag(flag);row.setTransactionQuantity(BigDecimal.valueOf(quantity));row.setStorageCheckFlag(true);row.setSourceDocType("ISSUE");row.setSourceDocId(issueId);row.setSourceDocCode(code);row.setSourceDocLineId(line);row.setItemId(raw);row.setItemCode(released.tag+"-raw");row.setItemName(released.tag+"-raw");row.setUnitOfMeasure("EA");row.setBatchCode(batch);row.setWorkorderId(released.order);row.setWorkorderCode(released.tag);
        row.setWarehouseId(((Number)dim.get("warehouse_id")).longValue());row.setWarehouseCode((String)dim.get("warehouse_code"));row.setWarehouseName((String)dim.get("warehouse_name"));row.setLocationId(((Number)dim.get("location_id")).longValue());row.setLocationCode((String)dim.get("location_code"));row.setLocationName((String)dim.get("location_name"));row.setAreaId(((Number)dim.get("area_id")).longValue());row.setAreaCode((String)dim.get("area_code"));row.setAreaName((String)dim.get("area_name"));return row;
    }

    void assertPartialThenFinalReport() {
        assertEquals(3,materials.available(task,null,null,1,200).getTotal());
        assertEquals(1,materials.available(task,raw,"B07",1,200).getTotal());
        assertEquals(0,materials.available(task,raw,"UNISSUED",1,200).getTotal());
        assertEquals(409,reports.complete(task,UUID.randomUUID().toString()).get("code"));
        ExecutionReportRequest first=request(0,5,group(b06,0,2),group(b07,2,5));
        assertEquals(200,reports.report(task,first).get("code"));
        assertEquals(200,reports.report(task,first).get("code"));
        assertEquals(5,countUnits("WAIT_INSPECTION"));assertEquals(5,countUnits("WAIT_ASSEMBLY"));
        assertBalance(b06,"0.00");assertBalance(b07,"2.00");assertBalance(b08,"5.00");assertBalance(source06,"5.00");
        assertEquals(2,materials.available(task,null,null,1,200).getTotal());
        assertEquals(200,reports.report(task,request(5,10,group(b08,5,10))).get("code"));
        assertEquals(10,countUnits("WAIT_INSPECTION"));assertEquals(0,countUnits("WAIT_ASSEMBLY"));assertBalance(b08,"0.00");
        assertEquals(10,db.queryForObject("select count(*) from pro_execution_input where task_id=?",Integer.class,task));
        assertEquals(Arrays.asList("B06:2","B07:3","B08:5"),db.queryForList("select concat(source_batch_code,':',count(*)) from pro_execution_input where task_id=? group by source_batch_code order by source_batch_code",String.class,task));
        assertEquals(2,db.queryForObject("select count(*) from pro_feedback where task_id=? and status='FINISHED'",Integer.class,task));
        assertEquals(3,db.queryForObject("select count(*) from wm_transaction where workorder_id=? and transaction_type='ITEM_CONSUME'",Integer.class,released.order));
        assertEquals(new BigDecimal("3.00"),db.queryForObject("select abs(transaction_quantity) from wm_transaction where workorder_id=? and transaction_type='ITEM_CONSUME' and batch_code='B07'",BigDecimal.class,released.order).setScale(2));
        assertEquals(0,db.queryForObject("select count(*) from wm_transaction where workorder_id=? and transaction_type='PRODUCT_PRODUCE'",Integer.class,released.order));
        assertEquals(0,db.queryForObject("select quantity_produced from pro_workorder where workorder_id=?",BigDecimal.class,released.order).signum());
        String op=UUID.randomUUID().toString();assertEquals(200,reports.complete(task,op).get("code"));assertEquals(200,reports.complete(task,op).get("code"));
        assertEquals("FINISHED",db.queryForObject("select status from pro_task where task_id=?",String.class,task));
        assertEquals(1,db.queryForObject("select count(*) from pro_execution_task where task_id=? and ended_by=77 and actual_ended_at>=actual_started_at",Integer.class,task));
    }

    void assertInvalidStockRollbacks() {
        assertEquals(409,reports.report(task,request(0,3,group(b06,0,3))).get("code"));
        assertEquals(400,reports.report(task,request(0,2,group(unissued,0,2))).get("code"));
        assertEquals(400,reports.report(task,request(0,2,group(source06,0,2))).get("code"));
        assertEquals(400,reports.report(task,request(0,2,group(foreignStock,0,2))).get("code"));
        assertEquals(10,countUnits("WAIT_ASSEMBLY"));assertBalance(b06,"2.00");
        assertEquals(0,db.queryForObject("select count(*) from pro_feedback where task_id=?",Integer.class,task));
        assertEquals(0,db.queryForObject("select count(*) from wm_item_consume where workorder_id=?",Integer.class,released.order));
        assertEquals(0,db.queryForObject("select count(*) from pro_execution_input where task_id=?",Integer.class,task));
        assertEquals(0,db.queryForObject("select count(*) from pro_execution_operation where workorder_id=? and action_type='REPORT'",Integer.class,released.order));
    }

    void assertCompetingReports() throws Exception {
        ExecutorService pool=Executors.newFixedThreadPool(2);
        try {
            CountDownLatch gate=new CountDownLatch(1);
            Future<AjaxResult> a=pool.submit(()->{login();gate.await();try{return reports.report(task,request(0,2,group(b06,0,2)));}finally{SecurityContextHolder.clearContext();}});
            Future<AjaxResult> b=pool.submit(()->{login();gate.await();try{return reports.report(task,request(2,4,group(b06,2,4)));}finally{SecurityContextHolder.clearContext();}});
            gate.countDown();List<Object> outcomes=Arrays.asList(a.get(30,TimeUnit.SECONDS).get("code"),b.get(30,TimeUnit.SECONDS).get("code"));
            assertTrue(outcomes.contains(200));assertTrue(outcomes.contains(409));assertBalance(b06,"0.00");assertEquals(2,countUnits("WAIT_INSPECTION"));assertEquals(8,countUnits("WAIT_ASSEMBLY"));
        }finally{pool.shutdownNow();}
    }

    int countUnits(String state){return db.queryForObject("select count(*) from pro_execution_unit where workorder_id=? and unit_state=?",Integer.class,released.order,state);}
    void assertBalance(long id,String expected){assertEquals(new BigDecimal(expected),db.queryForObject("select quantity_onhand from wm_material_stock where material_stock_id=?",BigDecimal.class,id).setScale(2));}
    ExecutionReportRequest request(int from,int to,ExecutionInputGroup...groups){ExecutionReportRequest r=new ExecutionReportRequest();r.operationId=UUID.randomUUID().toString();r.snIds=new ArrayList<>(sns.subList(from,to));r.inputs=Arrays.asList(groups);return r;}
    ExecutionInputGroup group(long stock,int from,int to){ExecutionInputGroup g=new ExecutionInputGroup();g.materialStockId=stock;g.snIds=new ArrayList<>(sns.subList(from,to));g.quantityPerUnit=BigDecimal.ONE;return g;}

    @AfterEach void cleanup(){
        if(!released.safe)return;
        db=released.db;
        db.update("delete from pro_execution_input where task_id=?",task);
        db.update("update pro_execution_unit set assembly_feedback_id=null where workorder_id=?",released.order);
        db.update("delete from pro_feedback where workorder_id=?",released.order);
        db.update("delete from wm_transaction where workorder_id=? and item_id=?",released.order,raw);
        db.update("delete l from wm_item_consume_line l join wm_item_consume h on h.record_id=l.record_id where h.workorder_id=?",released.order);
        db.update("delete from wm_item_consume where workorder_id=?",released.order);
        db.update("delete l from wm_issue_line l join wm_issue_header h on h.issue_id=l.issue_id where h.workorder_id=?",released.order);
        db.update("delete from wm_issue_header where workorder_id=?",released.order);
        if(foreignOrder>0){
            db.update("delete from wm_transaction where workorder_id=? and item_id=?",foreignOrder,raw);
            db.update("delete l from wm_issue_line l join wm_issue_header h on h.issue_id=l.issue_id where h.workorder_id=?",foreignOrder);
            db.update("delete from wm_issue_header where workorder_id=?",foreignOrder);
        }
        for(Long stock:stocks)db.update("delete from wm_material_stock where material_stock_id=?",stock);
        if(foreignOrder>0)db.update("delete from pro_workorder where workorder_id=?",foreignOrder);
        released.cleanup();
        db.update("delete from md_item where item_id=?",raw);
    }
}

@EnabledIfEnvironmentVariable(named="MES_B01_MYSQL_URL", matches=".+")
class ProExecutionConsumptionMySqlTest extends ExecutionConsumptionFixture {
    @Test void insufficientUnissuedPhysicalAndForeignStocksLeaveAllFactsUnchanged() { assertInvalidStockRollbacks(); }
    @Test void competingDistinctSnReportsDoNotOverdrawSingleStock() throws Exception { assertCompetingReports(); }
    @Test void queryWithoutPermissionDoesNotRevealExistingOrMissingTask() {
        LoginUser actor=(LoginUser)SecurityContextHolder.getContext().getAuthentication().getPrincipal();
        actor.setPermissions(Collections.emptySet());
        assertEquals(403,assertThrows(ExecutionException.class,()->materials.available(task,null,null,1,20)).getCode());
        assertEquals(403,assertThrows(ExecutionException.class,()->materials.available(Long.MAX_VALUE,null,null,1,20)).getCode());
    }
}
