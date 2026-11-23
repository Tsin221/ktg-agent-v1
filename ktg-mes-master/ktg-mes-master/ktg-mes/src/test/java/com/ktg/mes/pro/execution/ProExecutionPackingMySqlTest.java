package com.ktg.mes.pro.execution;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import com.ktg.common.core.domain.model.LoginUser;
import org.springframework.security.core.context.SecurityContextHolder;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;
@EnabledIfEnvironmentVariable(named="MES_B01_MYSQL_URL",matches=".+")
class ProExecutionPackingMySqlTest extends ExecutionInspectionFixture {
    long packingTask;
    ExecutionPackingService packing;
    @BeforeEach void packingSetup() {
        for(int i=0;i<10;i++) assertEquals(200,inspections.inspect(sns.get(i),check(i!=2)).get("code"));
        packingTask=db.queryForObject("select task_id from pro_execution_task where workorder_id=? and step_kind='PACKING'",Long.class,released.order);
        db.update("update pro_task set status='STARTED' where task_id=?",packingTask);
        db.update("update pro_execution_task set assigned_user_id=77,started_by=77,actual_started_at=current_timestamp(3) where task_id=?",packingTask);
        packingLogin();packing=new ExecutionPackingService(source,operations,ownership,json);
    }
    @AfterEach void packingCleanup() { if(db!=null && released.safe) db.update("delete from pro_execution_pack where task_id=?",packingTask); }
    void packingLogin() { qualityLogin();((LoginUser)SecurityContextHolder.getContext().getAuthentication().getPrincipal()).getPermissions().add("mes:pro:execution:pack"); }
    ExecutionPackingRequest packRequest(List<Long> ids) { ExecutionPackingRequest r=new ExecutionPackingRequest();r.operationId=UUID.randomUUID().toString();r.snIds=ids;return r; }
    @Test void ninePackWhileFailurePendingReplayAndNoInventoryChange() {
        long ledgerBefore=db.queryForObject("select count(*) from wm_transaction",Long.class);
        List<Long> ids=new ArrayList<>(sns);ids.remove(2);ExecutionPackingRequest r=packRequest(ids);
        assertEquals(200,packing.pack(packingTask,r).get("code"));assertEquals(200,packing.pack(packingTask,r).get("code"));
        assertEquals(9,countUnits("PACKED"));assertEquals(1,countUnits("WAIT_DISPOSITION"));
        assertEquals(9,db.queryForObject("select count(*) from pro_execution_pack where task_id=? and packed_by=77 and packed_at is not null",Integer.class,packingTask));
        assertEquals(ledgerBefore,db.queryForObject("select count(*) from wm_transaction",Long.class));
        assertEquals(409,packing.pack(packingTask,packRequest(ids)).get("code"));
    }
    @Test void mixedBatchRollsBackEverything() {
        ExecutionPackingRequest r=packRequest(Arrays.asList(sns.get(0),sns.get(2)));
        assertEquals(409,packing.pack(packingTask,r).get("code"));assertEquals(0,countUnits("PACKED"));
        assertEquals(0,db.queryForObject("select count(*) from pro_execution_operation where operation_id=?",Integer.class,r.operationId));
    }
    @Test void optionalBoxMustExistAndBelongToSameOrder() {
        ExecutionPackingRequest r=packRequest(Collections.singletonList(sns.get(0)));r.packageLineId=Long.MAX_VALUE;
        assertEquals(409,packing.pack(packingTask,r).get("code"));assertEquals(0,countUnits("PACKED"));
    }
    @Test void competingPackingCannotPackTwice() throws Exception {
        java.util.concurrent.ExecutorService pool=java.util.concurrent.Executors.newFixedThreadPool(2);
        try {
            java.util.concurrent.Callable<Object> call=()->{packingLogin();return packing.pack(packingTask,packRequest(Collections.singletonList(sns.get(0)))).get("code");};
            java.util.concurrent.Future<Object> a=pool.submit(call),b=pool.submit(call);
            List<Object> codes=Arrays.asList(a.get(),b.get());assertTrue(codes.contains(200));assertTrue(codes.contains(409));
            assertEquals(1,db.queryForObject("select count(*) from pro_execution_pack where sn_id=?",Integer.class,sns.get(0)));
        } finally {pool.shutdownNow();}
    }
}
