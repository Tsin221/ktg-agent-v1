package com.ktg.mes.pro.execution;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.ktg.common.core.domain.AjaxResult;
import com.ktg.common.utils.SecurityUtils;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.support.GeneratedKeyHolder;
import org.springframework.stereotype.Service;

import javax.sql.DataSource;
import java.sql.PreparedStatement;
import java.sql.Statement;
import java.util.*;
import java.util.stream.Collectors;

/** B03 assembly reports and completion never invoke the legacy feedback approval path. */
@Service
public class ExecutionReportService {
    private final JdbcTemplate db;
    private final ExecutionOperationService operations;
    private final ExecutionOwnershipService ownership;
    private final ExecutionAccessService access;
    private final ExecutionConsumptionService consumption;
    private final ObjectMapper json;

    public ExecutionReportService(DataSource dataSource, ExecutionOperationService operations,
            ExecutionOwnershipService ownership, ExecutionAccessService access,
            ExecutionConsumptionService consumption, ObjectMapper json) {
        this.db = new JdbcTemplate(dataSource);
        this.operations = operations;
        this.ownership = ownership;
        this.access = access;
        this.consumption = consumption;
        this.json = json;
    }

    public AjaxResult report(Long taskId, ExecutionReportRequest request) {
        ExecutionReportValidator.validate(request);
        long orderId = orderForTask(taskId);
        return operations.execute(request.operationId, ExecutionAction.REPORT, orderId, taskId, null,
                json.valueToTree(request), () -> record(orderId, taskId, request));
    }

    public AjaxResult complete(Long taskId, String operationId) {
        long orderId = orderForTask(taskId);
        return operations.execute(operationId, ExecutionAction.TASK_COMPLETE, orderId, taskId, null,
                json.createObjectNode().put("operationId", operationId), () -> finish(orderId, taskId));
    }

    private long orderForTask(Long taskId) {
        ExecutionOwnershipService.TaskScope scope = ownership.task(taskId);
        if (scope == null) throw ExecutionException.notFound();
        return scope.workorderId;
    }

    private ObjectNode record(long orderId, long taskId, ExecutionReportRequest request) {
        // The operation service holds the original order row and claim; acquire task, then unit locks.
        List<Map<String, Object>> tasks = lockTasks(orderId);
        Map<String, Object> assembly = requireStartedAssembly(tasks, taskId);
        Map<String, Object> next = tasks.stream().filter(t -> "NORMAL".equals(t.get("task_kind"))
                && "INSPECTION".equals(t.get("step_kind")) && number(t, "normal_step_no") == number(assembly, "normal_step_no") + 1)
                .findFirst().orElseThrow(() -> ExecutionException.stateConflict("缺少后续检测任务"));
        long nextTask = number(next, "task_id");
        TaskExecutionState nextState = TaskExecutionState.valueOf(String.valueOf(next.get("status")));
        if (nextState == TaskExecutionState.FINISHED) throw ExecutionException.stateConflict("检测任务已结束");
        List<Map<String, Object>> units = lockUnits(orderId);
        Set<Long> requested = ExecutionReportValidator.positiveUnique(request.snIds);
        access.requireTaskUnits(ExecutionAction.REPORT, orderId, taskId, requested);
        Map<Long, Map<String, Object>> selected = units.stream().filter(u -> requested.contains(number(u, "sn_id")))
                .collect(Collectors.toMap(u -> number(u, "sn_id"), u -> u));
        if (selected.size() != requested.size()) throw ExecutionException.notFound();
        for (Map<String, Object> unit : selected.values()) {
            if (number(unit, "current_task_id") != taskId || unit.get("assembly_feedback_id") != null)
                throw ExecutionException.stateConflict("所选单件不属于当前待装配任务");
            ExecutionState.valueOf(String.valueOf(unit.get("unit_state"))).advance(ExecutionAction.REPORT);
        }

        long feedbackId = insertFeedback(taskId, requested.size());
        long consumeId = consumption.consume(orderId, taskId, feedbackId, request);
        // Consumption and per-SN facts are in this transaction; only now move the selected SNs.
        for (Long snId : requested) {
            exactlyOne(db.update("update pro_execution_unit set current_task_id=?,unit_state='WAIT_INSPECTION',assembly_feedback_id=?,version=version+1,updated_at=current_timestamp(3) "
                    + "where sn_id=? and workorder_id=? and current_task_id=? and unit_state='WAIT_ASSEMBLY' and assembly_feedback_id is null",
                    nextTask, feedbackId, snId, orderId, taskId));
        }
        if (nextState == TaskExecutionState.WAITING) {
            nextState.ready();
            exactlyOne(db.update("update pro_task set status='READY' where task_id=? and status='WAITING'", nextTask));
        }
        List<ExecutionState> states = units.stream().map(u -> requested.contains(number(u, "sn_id"))
                ? ExecutionState.WAIT_INSPECTION : ExecutionState.valueOf(String.valueOf(u.get("unit_state"))))
                .collect(Collectors.toList());
        ExecutionStatistics totals = ExecutionStatistics.fromStates(states);
        exactlyOne(db.update("update pro_task set quantity_produced=? where task_id=? and status='STARTED'", totals.getAssembled(), taskId));
        long version = updateOrder(orderId, totals);
        return json.createObjectNode().put("feedbackId", feedbackId).put("consumeId", consumeId)
                .put("snCount", requested.size()).put("assembled", totals.getAssembled()).put("qualified", totals.getQualified())
                .put("state", ExecutionState.WAIT_INSPECTION.name()).put("version", version);
    }

    private ObjectNode finish(long orderId, long taskId) {
        requireStartedAssembly(lockTasks(orderId), taskId);
        List<Map<String, Object>> units = lockUnits(orderId);
        ExecutionStatistics totals = ExecutionStatistics.fromStates(units.stream()
                .map(row -> ExecutionState.valueOf(String.valueOf(row.get("unit_state")))).collect(Collectors.toList()));
        if (totals.getWaitingAssembly() != 0 || units.stream().anyMatch(u -> u.get("assembly_feedback_id") == null))
            throw ExecutionException.stateConflict("仍有未完成装配报工的单件");
        long actor = SecurityUtils.getUserId();
        exactlyOne(db.update("update pro_execution_task set actual_ended_at=current_timestamp(3),ended_by=? where task_id=? and actual_started_at is not null and actual_ended_at is null and actual_started_at<=current_timestamp(3)", actor, taskId));
        exactlyOne(db.update("update pro_task set status='FINISHED' where task_id=? and status='STARTED'", taskId));
        return json.createObjectNode().put("state", "FINISHED").put("assembled", totals.getAssembled())
                .put("version", updateOrder(orderId, totals));
    }

    private List<Map<String, Object>> lockTasks(long orderId) {
        List<Map<String, Object>> rows = db.queryForList("select e.task_id,e.normal_step_no,e.task_kind,e.step_kind,e.process_id,e.assigned_user_id,e.started_by,e.actual_started_at,e.actual_ended_at,t.status,t.workstation_id "
                + "from pro_execution_task e join pro_task t on t.task_id=e.task_id and t.workorder_id=e.workorder_id where e.workorder_id=? order by e.task_id for update", orderId);
        if (!"CONFIRMED".equals(db.queryForObject("select status from pro_workorder where workorder_id=?", String.class, orderId)))
            throw ExecutionException.stateConflict("工单未下达或已结束");
        return rows;
    }

    private Map<String, Object> requireStartedAssembly(List<Map<String, Object>> tasks, long taskId) {
        Map<String, Object> assembly = tasks.stream().filter(t -> number(t, "task_id") == taskId).findFirst().orElseThrow(ExecutionException::notFound);
        if (!"NORMAL".equals(assembly.get("task_kind")) || !"ASSEMBLY".equals(assembly.get("step_kind"))
                || !"STARTED".equals(assembly.get("status")) || assembly.get("actual_started_at") == null
                || assembly.get("started_by") == null || assembly.get("actual_ended_at") != null || !Objects.equals(SecurityUtils.getUserId(), assembly.get("assigned_user_id")))
            throw ExecutionException.stateConflict("仅已开工的正常装配任务可以报工或结束");
        if (db.queryForList("select workstation_id from md_workstation where workstation_id=? and process_id=? and enable_flag='Y' for update",
                assembly.get("workstation_id"), assembly.get("process_id")).isEmpty())
            throw ExecutionException.stateConflict("当前工作站不可执行该工序");
        return assembly;
    }

    private List<Map<String, Object>> lockUnits(long orderId) {
        List<Map<String, Object>> rows = db.queryForList("select sn_id,current_task_id,unit_state,assembly_feedback_id from pro_execution_unit where workorder_id=? order by sn_id for update", orderId);
        long planned = db.queryForObject("select planned_units from pro_execution_order where workorder_id=?", Long.class, orderId);
        if (rows.size() != planned || planned <= 0) throw ExecutionException.stateConflict("工单单件数与下达事实不符");
        return rows;
    }

    private long updateOrder(long orderId, ExecutionStatistics totals) {
        exactlyOne(db.update("update pro_workorder set quantity_scheduled=?,quantity_produced=?,version=version+1 where workorder_id=? and status='CONFIRMED'",
                totals.getAllocated(), totals.getQualified(), orderId));
        return db.queryForObject("select version from pro_workorder where workorder_id=?", Long.class, orderId);
    }

    private long insertFeedback(long taskId, int count) {
        String sql = "insert into pro_feedback(feedback_type,workstation_id,workstation_code,workstation_name,process_id,process_code,process_name,workorder_id,workorder_code,workorder_name,task_id,task_code,item_id,item_code,item_name,specification,unit_of_measure,quantity,quantity_feedback,quantity_qualified,quantity_unquanlified,user_name,nick_name,feedback_channel,feedback_time,status,create_by,create_time) "
                + "select 'SELF',workstation_id,workstation_code,workstation_name,process_id,process_code,process_name,workorder_id,workorder_code,workorder_name,task_id,task_code,item_id,item_code,item_name,specification,unit_of_measure,quantity,?,0,0,?,?, 'WEB',current_timestamp(3),'FINISHED',?,current_timestamp(3) from pro_task where task_id=?";
        GeneratedKeyHolder keys = new GeneratedKeyHolder();
        String username = SecurityUtils.getUsername();
        exactlyOne(db.update(connection -> {
            PreparedStatement p = connection.prepareStatement(sql, Statement.RETURN_GENERATED_KEYS);
            p.setInt(1, count); p.setString(2, username);
            p.setString(3, SecurityUtils.getLoginUser().getUser().getNickName());
            p.setString(4, username); p.setLong(5, taskId);
            return p;
        }, keys));
        return Objects.requireNonNull(keys.getKey()).longValue();
    }

    private static long number(Map<String, Object> row, String field) { return ((Number) row.get(field)).longValue(); }
    private static void exactlyOne(int count) { if (count != 1) throw ExecutionException.stateConflict("生产事实已变化，请刷新"); }
}
