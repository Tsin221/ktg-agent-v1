package com.ktg.mes.pro.execution;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.ktg.common.core.domain.AjaxResult;
import com.ktg.common.utils.SecurityUtils;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

import javax.sql.DataSource;
import java.util.Map;

/** Task-wide elapsed time is persisted once, independently of planned schedule data. */
@Service
public class ExecutionTaskStartService {
    private final JdbcTemplate db;
    private final ExecutionTaskQueryService tasks;
    private final ExecutionOperationService operations;
    private final ObjectMapper json;

    public ExecutionTaskStartService(DataSource source,ExecutionTaskQueryService tasks,ExecutionOperationService operations,ObjectMapper json) {
        this.db=new JdbcTemplate(source);this.tasks=tasks;this.operations=operations;this.json=json;
    }

    public AjaxResult start(Long taskId,ExecutionTaskStartRequest request) {
        if(request==null) throw ExecutionException.validation("开工请求不能为空");
        Long orderId=tasks.orderId(taskId);
        return operations.execute(request.operationId,ExecutionAction.START,orderId,taskId,null,json.createObjectNode(),()->{
            Map<String,Object> task=tasks.taskRow(taskId,true);
            Long actor=SecurityUtils.getUserId();
            if(!orderId.equals(ExecutionTaskQueryService.number(task.get("workorder_id"))) || !actor.equals(task.get("assigned_user_id")))
                throw ExecutionException.forbidden();
            if(!startable(task,tasks.availableUnits(task))) throw ExecutionException.stateConflict("前序未满足或任务已经开工");
            if(!tasks.legalWorkstation(task,true)) throw ExecutionException.stateConflict("任务的工作站已失效");
            TaskExecutionState.valueOf((String)task.get("status")).start();
            if(db.update("update pro_execution_task set started_by=?,actual_started_at=current_timestamp(3) "
                    +"where task_id=? and actual_started_at is null and actual_ended_at is null and assigned_user_id=?",actor,taskId,actor)!=1
                    || db.update("update pro_task set status='STARTED' where task_id=? and workorder_id=? and status='READY'",taskId,orderId)!=1
                    || db.update("update pro_workorder set version=version+1 where workorder_id=? and status='CONFIRMED'",orderId)!=1)
                throw ExecutionException.stateConflict("任务开工状态已变化");
            return json.createObjectNode().put("state","STARTED");
        });
    }

    static boolean startable(Map<String,Object> task,int count) {
        return "CONFIRMED".equals(task.get("order_status")) && "READY".equals(task.get("status")) && count>0
                && task.get("actual_started_at")==null && task.get("actual_ended_at")==null;
    }
}
