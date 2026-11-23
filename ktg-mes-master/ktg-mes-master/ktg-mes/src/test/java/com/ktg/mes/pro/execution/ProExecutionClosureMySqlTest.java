package com.ktg.mes.pro.execution;

import com.ktg.common.core.domain.model.LoginUser;
import com.ktg.framework.web.service.PermissionService;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.security.core.context.SecurityContextHolder;

import java.math.BigDecimal;
import java.util.*;

import static org.junit.jupiter.api.Assertions.*;

/** Service-level integration with the shared isolated B01 fixture; live HTTP/UI acceptance is separate. */
@EnabledIfEnvironmentVariable(named = "MES_B01_MYSQL_URL", matches = ".+")
class ProExecutionClosureMySqlTest extends ExecutionInspectionFixture {
    private long packingTask;

    @Test void independentReworkOrderFinishesTenPackingAndPreservesFirstFailure() {
        closeOrder(true);
    }

    @Test void independentScrapOrderFinishesNinePackingAndNeverProducesScrap() {
        closeOrder(false);
    }

    private void closeOrder(boolean rework) {
        ((LoginUser) SecurityContextHolder.getContext().getAuthentication().getPrincipal()).getPermissions()
                .addAll(Arrays.asList("mes:pro:execution:disposition", "mes:pro:execution:manageAll",
                        "mes:pro:execution:rework", "mes:pro:execution:start", "mes:pro:execution:pack",
                        "mes:pro:execution:complete"));
        ExecutionTaskQueryService tasks = new ExecutionTaskQueryService(source, access, new PermissionService());
        ExecutionTaskStartService starts = new ExecutionTaskStartService(source, tasks, operations, json);
        ExecutionCompletionService completion = new ExecutionCompletionService(source, operations,
                new ExecutionOrderProgressService(source, access), tasks, json);
        assertEquals(200, reports.complete(task, operation()).get("code"));
        assertEquals(0, outputCount());
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
        assertEquals(200, starts.start(packingTask, start()).get("code"));
        ExecutionPackingService packing = new ExecutionPackingService(source, operations, ownership, json);
        List<Long> accepted = new ArrayList<>(sns);
        accepted.remove(Long.valueOf(failedSn));
        assertEquals(200, packing.pack(packingTask, pack(accepted)).get("code"));
        assertNotEquals(200, packing.pack(packingTask, pack(Collections.singletonList(failedSn))).get("code"));
        assertEquals(409, completion.complete(released.order, completeRequest()).get("code"));
        assertEquals(9, countUnits("PACKED"));

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
            assertEquals(200, starts.start(rw, start()).get("code"));
            assertEquals(200, new ExecutionReworkService(source, tasks, operations, json).complete(rw, operation()).get("code"));
            assertEquals(200, inspections.inspect(failedSn, check(true)).get("code"));
            assertEquals(200, packing.pack(packingTask, pack(Collections.singletonList(failedSn))).get("code"));
        } else {
            assertNotEquals(200, packing.pack(packingTask, pack(Collections.singletonList(failedSn))).get("code"));
            assertEquals(1, countUnits("SCRAPPED"));
        }
        assertEquals(200, inspections.complete(inspectionTask, operation()).get("code"));
        assertEquals(200, completion.completePacking(packingTask, operation()).get("code"));
        ExecutionCompletionRequest request = completeRequest();
        assertEquals(200, completion.complete(released.order, request).get("code"));
        assertEquals(200, completion.complete(released.order, request).get("code"));

        int qualified = rework ? 10 : 9;
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
