package com.ktg.mes.pro.execution;
import com.ktg.common.core.domain.model.LoginUser;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.security.core.context.SecurityContextHolder;
import com.ktg.framework.web.service.PermissionService;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;
@EnabledIfEnvironmentVariable(named="MES_B01_MYSQL_URL",matches=".+")
class ProExecutionDispositionMySqlTest extends ExecutionConsumptionFixture  {
    Long ipqc,inspection;
    ExecutionDispositionService dispositions;
    void failure() {
        LoginUser actor=(LoginUser)SecurityContextHolder.getContext().getAuthentication().getPrincipal();
        actor.getPermissions().addAll(Arrays.asList("mes:pro:execution:disposition","mes:pro:execution:manageAll","mes:pro:execution:rework","mes:pro:execution:start"));
        long check=db.queryForObject("select task_id from pro_execution_task where workorder_id=? and step_kind='INSPECTION'",Long.class,released.order);
        Map<String,Object> taskRow=db.queryForMap("select t.*,e.inspection_template_id,e.inspection_type from pro_task t join pro_execution_task e on e.task_id=t.task_id where t.task_id=?",check);
        Map<String,Object> quality=ExecutionFactWriter.copy(taskRow,"workorder_id,workorder_code,workorder_name,task_id,task_code,task_name,workstation_id,workstation_code,workstation_name,process_id,process_code,process_name,item_id,item_code,item_name,specification,unit_of_measure");
        quality.putAll(ExecutionFactWriter.values("ipqc_code",released.tag+"-fail","ipqc_name","首检失败","ipqc_type",taskRow.get("inspection_type"),"template_id",taskRow.get("inspection_template_id"),"quantity_check",1,"quantity_qualified",0,"quantity_unqualified",1,"check_result","REJECT","status","FINISHED","inspect_date",new Date(),"inspector","B04 tester"));
        ipqc=ExecutionFactWriter.insert(db,"qc_ipqc","ipqc_id",quality);
        db.update("insert into pro_execution_inspection(sn_id,task_id,ipqc_id,round_no) values(?,?,?,1)",sns.get(0),check,ipqc);
        inspection=db.queryForObject("select inspection_id from pro_execution_inspection where ipqc_id=?",Long.class,ipqc);
        db.update("update pro_execution_unit set current_task_id=?,unit_state='WAIT_DISPOSITION' where sn_id=?",check,sns.get(0));
        dispositions=new ExecutionDispositionService(source,operations,json);
    }
    ExecutionDispositionRequest decision(String decision) {
        ExecutionDispositionRequest r=new ExecutionDispositionRequest();
        r.operationId=UUID.randomUUID().toString();
        r.inspectionId=inspection;
        r.decision=decision;
        r.reason="实际检测故障";
        return r;
    }
    @Test void scrapIsExplicitAndReplayCannotBecomeRework() {
        failure();
        ExecutionDispositionRequest r=decision("SCRAP");
        assertEquals(200,dispositions.dispose(sns.get(0),r).get("code"));
        assertEquals(200,dispositions.dispose(sns.get(0),r).get("code"));
        assertEquals(409,dispositions.dispose(sns.get(0),decision("REWORK")).get("code"));
        assertEquals("SCRAPPED",db.queryForObject("select unit_state from pro_execution_unit where sn_id=?",String.class,sns.get(0)));
        assertEquals(1,db.queryForObject("select count(*) from pro_execution_disposition where inspection_id=?",Integer.class,inspection));
    }
    @Test void reworkRequiresStartAndCompletionReturnsOnlySourceSnForReinspection() {
        failure();
        ExecutionDispositionRequest r=decision("REWORK");
        assertEquals(200,dispositions.dispose(sns.get(0),r).get("code"));
        Long rw=db.queryForObject("select e.task_id from pro_execution_task e join pro_execution_disposition d on d.disposition_id=e.source_disposition_id where d.inspection_id=?",Long.class,inspection);
        db.update("update pro_execution_task set assigned_user_id=77 where task_id=?",rw);
        ExecutionTaskQueryService tasks=new ExecutionTaskQueryService(source,access,new PermissionService());
        Long normalInspection=db.queryForObject("select task_id from pro_execution_task where workorder_id=? and task_kind='NORMAL' and step_kind='INSPECTION'",Long.class,released.order);
        assertEquals(0L,tasks.units(normalInspection,1,20,null,null).getTotal());
        assertEquals(1L,tasks.units(normalInspection,1,20,null,null,true).getTotal());
        ExecutionReworkService service=new ExecutionReworkService(source,tasks,operations,json);
        assertEquals(409,service.complete(rw,UUID.randomUUID().toString()).get("code"));
        ExecutionTaskStartRequest start=new ExecutionTaskStartRequest();
        start.operationId=UUID.randomUUID().toString();
        assertEquals(200,new ExecutionTaskStartService(source,tasks,operations,json).start(rw,start).get("code"));
        String op=UUID.randomUUID().toString();
        assertEquals(200,service.complete(rw,op).get("code"));
        assertEquals(200,service.complete(rw,op).get("code"));
        assertEquals("WAIT_REINSPECTION",db.queryForObject("select unit_state from pro_execution_unit where sn_id=?",String.class,sns.get(0)));
        assertEquals(9,countUnits("WAIT_ASSEMBLY"));
        assertEquals(0,db.queryForObject("select count(*) from pro_execution_input where sn_id=?",Integer.class,sns.get(0)));
    }
    @Test void oppositeConcurrentDecisionsCannotBothCommit() throws Exception {
        failure();
        org.springframework.security.core.Authentication actor=SecurityContextHolder.getContext().getAuthentication();
        java.util.concurrent.ExecutorService workers=java.util.concurrent.Executors.newFixedThreadPool(2);
        java.util.concurrent.CountDownLatch gate=new java.util.concurrent.CountDownLatch(1);
        try {
            java.util.concurrent.Future<Integer> scrap=workers.submit(()->{
                SecurityContextHolder.getContext().setAuthentication(actor);
                try { gate.await(); return (Integer)dispositions.dispose(sns.get(0),decision("SCRAP")).get("code"); }
                finally { SecurityContextHolder.clearContext(); }
            });
            java.util.concurrent.Future<Integer> rework=workers.submit(()->{
                SecurityContextHolder.getContext().setAuthentication(actor);
                try { gate.await(); return (Integer)dispositions.dispose(sns.get(0),decision("REWORK")).get("code"); }
                finally { SecurityContextHolder.clearContext(); }
            });
            gate.countDown();
            List<Integer> codes=new ArrayList<>(Arrays.asList(scrap.get(20,java.util.concurrent.TimeUnit.SECONDS),rework.get(20,java.util.concurrent.TimeUnit.SECONDS)));
            Collections.sort(codes);
            assertEquals(Arrays.asList(200,409),codes);
            assertEquals(1,db.queryForObject("select count(*) from pro_execution_disposition where inspection_id=?",Integer.class,inspection));
            assertEquals(1,db.queryForObject("select count(*) from pro_execution_operation where workorder_id=? and action_type in ('DISPOSITION_SCRAP','DISPOSITION_REWORK')",Integer.class,released.order));
        } finally { workers.shutdownNow(); }
    }
    @AfterEach void removeQuality() {
        if(inspection==null)return;
        db.update("update pro_execution_task set source_disposition_id=null where workorder_id=? and task_kind='REWORK'",released.order);
        db.update("delete from pro_execution_disposition where inspection_id=?",inspection);
        db.update("delete from pro_execution_inspection where inspection_id=?",inspection);
        db.update("delete from qc_ipqc where ipqc_id=?",ipqc);
    }
}
