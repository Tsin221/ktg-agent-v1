package com.ktg.mes.pro.execution;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import com.ktg.common.core.domain.model.LoginUser;
import org.springframework.security.core.context.SecurityContextHolder;
import com.ktg.framework.web.service.PermissionService;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;
@EnabledIfEnvironmentVariable(named="MES_B01_MYSQL_URL",matches=".+")
class ProExecutionCompletionMySqlTest extends ExecutionConsumptionFixture {
    ExecutionOrderProgressService progress;
    ExecutionCompletionService completion;
    long packing;
    void arrange() {
        LoginUser actor=(LoginUser)SecurityContextHolder.getContext().getAuthentication().getPrincipal();
        actor.getPermissions().addAll(Arrays.asList("mes:pro:execution:complete","mes:pro:execution:manageAll","mes:pro:execution:pack"));
        progress=new ExecutionOrderProgressService(source,access);
        completion=new ExecutionCompletionService(source,operations,progress,new ExecutionTaskQueryService(source,access,new PermissionService()),json);
        packing=db.queryForObject("select task_id from pro_execution_task where workorder_id=? and step_kind='PACKING'",Long.class,released.order);
        db.update("update pro_execution_task set assigned_user_id=77,started_by=77,actual_started_at=current_timestamp(3),ended_by=77,actual_ended_at=current_timestamp(3) where workorder_id=?",released.order);
        db.update("update pro_task set status='FINISHED' where workorder_id=?",released.order);
        db.update("update pro_execution_unit set unit_state='PACKED',current_task_id=? where workorder_id=?",packing,released.order);
        for(Long sn:sns)db.update("insert into pro_execution_pack(sn_id,task_id,packed_by,packed_at) values(?,?,77,current_timestamp(3))",sn,packing);
    }
    ExecutionCompletionRequest request() {ExecutionCompletionRequest r=new ExecutionCompletionRequest();r.operationId=UUID.randomUUID().toString();r.version=db.queryForObject("select version from pro_workorder where workorder_id=?",Long.class,released.order);return r;}
    @Test void onlyCompleteTerminalOrderAndReplayExactlyOnce() {
        arrange();
        db.update("update pro_execution_unit set unit_state='WAIT_DISPOSITION' where sn_id=?",sns.get(9));
        ExecutionCompletionRequest r=request();
        assertEquals(409,completion.complete(released.order,r).get("code"));
        assertEquals(0,db.queryForObject("select count(*) from pro_execution_operation where operation_id=?",Integer.class,r.operationId));
        db.update("update pro_execution_unit set unit_state='PACKED' where sn_id=?",sns.get(9));
        assertEquals(200,completion.complete(released.order,r).get("code"));
        assertEquals(200,completion.complete(released.order,r).get("code"));
        assertEquals(r.version+1,db.queryForObject("select version from pro_workorder where workorder_id=?",Long.class,released.order));
        assertEquals(409,completion.complete(released.order,request()).get("code"));
    }
    @Test void rejectUnfinishedTaskMissingPackAndStaleVersion() {
        arrange();ExecutionCompletionRequest stale=request();stale.version--;
        assertEquals("VERSION_CONFLICT",completion.complete(released.order,stale).get("errorCode"));
        db.update("delete from pro_execution_pack where sn_id=?",sns.get(0));
        assertEquals(409,completion.complete(released.order,request()).get("code"));
        db.update("insert into pro_execution_pack(sn_id,task_id,packed_by,packed_at) values(?,?,77,current_timestamp(3))",sns.get(0),packing);
        db.update("update pro_task set status='STARTED' where task_id=?",packing);
        assertEquals(409,completion.complete(released.order,request()).get("code"));
        db.update("update pro_execution_task set actual_ended_at=null,ended_by=null where task_id=?",packing);
        assertEquals(200,completion.completePacking(packing,UUID.randomUUID().toString()).get("code"));
        assertEquals(200,completion.complete(released.order,request()).get("code"));
    }
    @Test void concurrentDistinctCompletionOperationsCommitOnce() throws Exception {
        arrange();ExecutionCompletionRequest a=request(),b=request();
        org.springframework.security.core.Authentication actor=SecurityContextHolder.getContext().getAuthentication();
        java.util.concurrent.ExecutorService pool=java.util.concurrent.Executors.newFixedThreadPool(2);
        java.util.concurrent.CountDownLatch gate=new java.util.concurrent.CountDownLatch(1);
        try {
            java.util.List<java.util.concurrent.Future<Integer>> results=new ArrayList<>();
            for(ExecutionCompletionRequest r:Arrays.asList(a,b))results.add(pool.submit(()->{SecurityContextHolder.getContext().setAuthentication(actor);try{gate.await();return (Integer)completion.complete(released.order,r).get("code");}finally{SecurityContextHolder.clearContext();}}));
            gate.countDown();List<Integer> codes=new ArrayList<>();for(java.util.concurrent.Future<Integer> r:results)codes.add(r.get(20,java.util.concurrent.TimeUnit.SECONDS));Collections.sort(codes);assertEquals(Arrays.asList(200,409),codes);
            assertEquals(1,db.queryForObject("select count(*) from pro_execution_operation where workorder_id=? and action_type='COMPLETE'",Integer.class,released.order));
        } finally {pool.shutdownNow();}
    }
    @Test void unauthorizedCompletionCannotMutateOrder() {
        arrange();LoginUser actor=(LoginUser)SecurityContextHolder.getContext().getAuthentication().getPrincipal();actor.getPermissions().remove("mes:pro:execution:complete");
        assertEquals(403,completion.complete(released.order,request()).get("code"));
        assertEquals("CONFIRMED",db.queryForObject("select status from pro_workorder where workorder_id=?",String.class,released.order));
    }
    @AfterEach void packs() {if(db!=null)db.update("delete from pro_execution_pack where task_id in(select task_id from pro_execution_task where workorder_id=?)",released.order);}
}
