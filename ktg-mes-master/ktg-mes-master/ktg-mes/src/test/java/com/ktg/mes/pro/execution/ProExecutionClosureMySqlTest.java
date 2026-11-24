package com.ktg.mes.pro.execution;

import com.ktg.common.core.domain.model.LoginUser;
import com.ktg.common.core.domain.entity.SysUser;
import com.ktg.framework.web.service.PermissionService;
import com.ktg.system.service.ISysMenuService;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;

import java.math.BigDecimal;
import java.sql.Timestamp;
import java.util.*;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

/** Service-level integration with the shared isolated B01 fixture; live HTTP/UI acceptance is separate. */
@EnabledIfEnvironmentVariable(named = "MES_B01_MYSQL_URL", matches = ".+")
class ProExecutionClosureMySqlTest extends ExecutionInspectionFixture {
    private long packingTask;

    @Test void independentReworkOrderFinishesTenPackingAndPreservesFirstFailure() {
        closeOrder(true, false);
    }

    @Test void independentScrapOrderFinishesNinePackingAndNeverProducesScrap() {
        closeOrder(false, false);
    }

    @Test void inspectionPackingAndReworkCanAllBeHandedOverAndCloseOrder() {
        closeOrder(true, true);
    }

    private void closeOrder(boolean rework, boolean transfer) {
        ((LoginUser) SecurityContextHolder.getContext().getAuthentication().getPrincipal()).getPermissions()
                .addAll(Arrays.asList("mes:pro:execution:disposition", "mes:pro:execution:manageAll",
                        "mes:pro:execution:rework", "mes:pro:execution:start", "mes:pro:execution:pack",
                        "mes:pro:execution:complete", "mes:pro:execution:assign"));
        ExecutionTaskQueryService tasks = new ExecutionTaskQueryService(source, access, new PermissionService());
        ExecutionTaskStartService starts = new ExecutionTaskStartService(source, tasks, operations, json);
        ExecutionOrderProgressService progress = new ExecutionOrderProgressService(source, access);
        ExecutionCompletionService completion = new ExecutionCompletionService(source, operations, progress, tasks, json);
        ExecutionOrderController orders = new ExecutionOrderController(released.service, progress, completion);
        assertEquals(200, reports.complete(task, operation()).get("code"));
        assertEquals(0, outputCount());
        Long replacement=transfer?db.queryForObject("select user_id from sys_user where user_id<>1 and user_id<>77 and status='0' and del_flag='0' order by user_id limit 1",Long.class):77L;
        if(transfer) handoff(tasks, inspectionTask, replacement);
        long failedSn = sns.get(2);
        for (Long sn : sns) {
            ExecutionInspectionRequest check = check(sn != failedSn);
            assertEquals(200, inspections.inspect(sn, check).get("code"));
            assertEquals(200, inspections.inspect(sn, check).get("code"));
        }
        assertEquals(9, outputCount());
        assertEquals(1, countUnits("WAIT_DISPOSITION"));
        packingTask = db.queryForObject("select task_id from pro_execution_task where workorder_id=? and step_kind='PACKING'", Long.class, released.order);
        // Fixture coordinates task assignment. The actual start, pack, disposition and finish use production services.
        db.update("update pro_execution_task set assigned_user_id=77 where task_id=?", packingTask);
        switchActor(77L);
        assertEquals(200, starts.start(packingTask, start()).get("code"));
        if(transfer) handoff(tasks,packingTask,replacement);
        ExecutionPackingService packing = new ExecutionPackingService(source, operations, ownership, json);
        List<Long> accepted = new ArrayList<>(sns);
        accepted.remove(Long.valueOf(failedSn));
        assertEquals(200, packing.pack(packingTask, pack(accepted)).get("code"));
        assertNotEquals(200, packing.pack(packingTask, pack(Collections.singletonList(failedSn))).get("code"));
        assertEquals(409, completion.complete(released.order, completeRequest()).get("code"));
        assertEquals(9, countUnits("PACKED"));
        assertOrderProgress(orders,"CONFIRMED",9,9,0,1,0,0,false);

        ExecutionDispositionRequest decision = new ExecutionDispositionRequest();
        decision.operationId = operation();
        decision.inspectionId = db.queryForObject("select inspection_id from pro_execution_inspection where sn_id=? and round_no=1", Long.class, failedSn);
        decision.decision = rework ? "REWORK" : "SCRAP";
        decision.reason = "B06 independent closure verification";
        assertEquals(200, new ExecutionDispositionService(source, operations, json).dispose(failedSn, decision).get("code"));
        if (rework) {
            long rw = db.queryForObject("select task_id from pro_execution_task where workorder_id=? and task_kind='REWORK'", Long.class, released.order);
            db.update("update pro_execution_task set assigned_user_id=77 where task_id=?", rw);
            assertNotEquals(200, inspections.inspect(failedSn, check(true)).get("code"));
            switchActor(77L);
            assertEquals(200, starts.start(rw, start()).get("code"));
            if(transfer) handoff(tasks,rw,replacement);
            assertEquals(200, new ExecutionReworkService(source, tasks, operations, json).complete(rw, operation()).get("code"));
            assertEquals(200, inspections.inspect(failedSn, check(true)).get("code"));
            assertEquals(200, packing.pack(packingTask, pack(Collections.singletonList(failedSn))).get("code"));
        } else {
            assertNotEquals(200, packing.pack(packingTask, pack(Collections.singletonList(failedSn))).get("code"));
            assertEquals(1, countUnits("SCRAPPED"));
        }
        assertEquals(200, inspections.complete(inspectionTask, operation()).get("code"));
        assertEquals(200, completion.completePacking(packingTask, operation()).get("code"));
        int qualified = rework ? 10 : 9;
        assertOrderProgress(orders,"CONFIRMED",qualified,qualified,0,0,10-qualified,rework?1:0,true);
        ExecutionCompletionRequest request = completeRequest();
        assertEquals(200, completion.complete(released.order, request).get("code"));
        assertEquals(200, completion.complete(released.order, request).get("code"));

        assertOrderProgress(orders,"FINISHED",qualified,qualified,0,0,10-qualified,rework?1:0,false);
        assertEquals(qualified, countUnits("PACKED"));
        assertEquals(10 - qualified, countUnits("SCRAPPED"));
        assertEquals(qualified, outputCount());
        assertEquals(new BigDecimal(qualified).setScale(2), db.queryForObject(
                "select quantity_produced from pro_workorder where workorder_id=?", BigDecimal.class, released.order).setScale(2));
        assertEquals("FINISHED", db.queryForObject("select status from pro_workorder where workorder_id=?", String.class, released.order));
        assertEquals(0, db.queryForObject("select count(*) from pro_task where workorder_id=? and status<>'FINISHED'", Integer.class, released.order));
        assertEquals(Arrays.asList("B06:2", "B07:3", "B08:5"), db.queryForList("select concat(source_batch_code,':',count(*)) from pro_execution_input where task_id=? group by source_batch_code order by source_batch_code", String.class, task));
        assertEquals(rework ? Arrays.asList("REJECT", "ACCEPT") : Collections.singletonList("REJECT"),
                db.queryForList("select q.check_result from pro_execution_inspection e join qc_ipqc q on q.ipqc_id=e.ipqc_id where e.sn_id=? order by e.round_no", String.class, failedSn));
        assertBalance(b06, "0.00"); assertBalance(b07, "2.00"); assertBalance(b08, "0.00"); assertBalance(source06, "5.00");
        if(transfer) {
            assertEquals(3,db.queryForObject("select count(*) from pro_execution_task where workorder_id=? and started_by=77 and ended_by=? and assigned_user_id=?",Integer.class,released.order,replacement,replacement));
            assertEquals(10,db.queryForObject("select count(*) from pro_execution_pack where task_id=? and packed_by=?",Integer.class,packingTask,replacement));
        }
    }

    private void assertOrderProgress(ExecutionOrderController controller,String status,long qualified,long packed,
                                     long packable,long disposition,long scrapped,long reinspections,boolean completionAllowed) {
        Map<?,?> detail=(Map<?,?>)controller.detail(released.order).get("data");
        Map<?,?> progress=(Map<?,?>)detail.get("progress"), summary=(Map<?,?>)detail.get("summary");
        assertEquals(status,((Map<?,?>)detail.get("order")).get("status"));
        assertEquals(status,progress.get("status"));
        Map<String,Long> counts=new LinkedHashMap<>();
        counts.put("planned",10L);counts.put("allocated",10L);counts.put("assembled",10L);
        counts.put("waitingAssembly",0L);counts.put("waitingInspection",0L);
        counts.put("waitingReinspection",0L);counts.put("reworkInProgress",0L);
        counts.put("qualified",qualified);counts.put("packed",packed);counts.put("packable",packable);
        counts.put("pendingDisposition",disposition);counts.put("scrapped",scrapped);
        counts.put("firstInspectionRejected",1L);counts.put("reinspectionCount",reinspections);
        counts.forEach((key,value)->assertEquals(value.longValue(),((Number)progress.get(key)).longValue(),key));
        for(String key:Arrays.asList("allocated","assembled","qualified","packed","scrapped")) {
            assertEquals(counts.get(key).longValue(),((Number)summary.get(key)).longValue(),key);
        }
        assertEquals(completionAllowed,progress.get("completionAllowed"));
    }

    private void handoff(ExecutionTaskQueryService tasks, Long target, Long replacement) {
        Timestamp start=db.queryForObject("select actual_started_at from pro_execution_task where task_id=?",Timestamp.class,target);
        Set<String> rights=((LoginUser)SecurityContextHolder.getContext().getAuthentication().getPrincipal()).getPermissions();
        ISysMenuService menus=mock(ISysMenuService.class);
        when(menus.selectMenuPermsByUserId(replacement)).thenReturn(rights);
        ExecutionAssignmentService assignments=new ExecutionAssignmentService(source,tasks,operations,access,menus,json);
        ExecutionTaskAssignmentRequest r=new ExecutionTaskAssignmentRequest();r.operationId=operation();r.assignedUserId=replacement;
        assertEquals(200,assignments.assign(target,r).get("code"));
        assertEquals(start,db.queryForObject("select actual_started_at from pro_execution_task where task_id=?",Timestamp.class,target));
        assertEquals(77L,db.queryForObject("select started_by from pro_execution_task where task_id=?",Long.class,target));
        switchActor(replacement);
    }

    private void switchActor(Long actor) {
        Set<String> rights=new HashSet<>(((LoginUser)SecurityContextHolder.getContext().getAuthentication().getPrincipal()).getPermissions());
        SysUser user=new SysUser();user.setUserId(actor);user.setUserName("B06-test");user.setNickName("B06 tester");
        LoginUser login=new LoginUser(actor,1L,user,rights);
        SecurityContextHolder.getContext().setAuthentication(new UsernamePasswordAuthenticationToken(login,null,Collections.emptyList()));
    }

    private int outputCount() {
        return db.queryForObject("select count(*) from wm_transaction where workorder_id=? and transaction_type='PRODUCT_PRODUCE'", Integer.class, released.order);
    }

    private static String operation() { return UUID.randomUUID().toString(); }

    private ExecutionTaskStartRequest start() {
        ExecutionTaskStartRequest r = new ExecutionTaskStartRequest(); r.operationId = operation(); return r;
    }

    private ExecutionCompletionRequest completeRequest() {
        ExecutionCompletionRequest r = new ExecutionCompletionRequest(); r.operationId = operation();
        r.version = db.queryForObject("select version from pro_workorder where workorder_id=?", Long.class, released.order); return r;
    }

    private ExecutionPackingRequest pack(List<Long> sns) {
        ExecutionPackingRequest r = new ExecutionPackingRequest(); r.operationId = operation(); r.snIds = sns; return r;
    }

    @AfterEach void removePackingFacts() {
        if (db != null && released.safe && packingTask != 0) db.update("delete from pro_execution_pack where task_id=?", packingTask);
    }
}
