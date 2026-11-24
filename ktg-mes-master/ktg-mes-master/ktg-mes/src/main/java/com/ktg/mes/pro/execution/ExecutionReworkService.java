package com.ktg.mes.pro.execution;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.ktg.common.core.domain.AjaxResult;
import com.ktg.common.utils.SecurityUtils;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import javax.sql.DataSource;
import java.util.*;
@Service
public class ExecutionReworkService  {
    private final JdbcTemplate db;
    private final ExecutionTaskQueryService tasks;
    private final ExecutionOperationService operations;
    private final ObjectMapper json;
    public ExecutionReworkService(DataSource ds,ExecutionTaskQueryService tasks,ExecutionOperationService operations,ObjectMapper json) {
        db=new JdbcTemplate(ds);
        this.tasks=tasks;
        this.operations=operations;
        this.json=json;
    }
    public AjaxResult complete(Long taskId,String operationId) {
        Long order=tasks.orderId(taskId);
        return operations.execute(operationId,ExecutionAction.REWORK_COMPLETE,order,taskId,null,json.createObjectNode(),()-> {
            db.queryForList("select task_id from pro_execution_task where workorder_id=? order by task_id for update",order);
            Map<String,Object> task=tasks.taskRow(taskId,true);
            if(!"REWORK".equals(task.get("task_kind"))||!"STARTED".equals(task.get("status"))||!"CONFIRMED".equals(task.get("order_status"))||task.get("actual_started_at")==null||task.get("started_by")==null||task.get("actual_ended_at")!=null||!SecurityUtils.getUserId().equals(task.get("assigned_user_id"))||!tasks.legalWorkstation(task,true))throw ExecutionException.stateConflict("返工任务须已开工且由当前指派人员执行");
            List<Map<String,Object>> units=db.queryForList("select u.sn_id from pro_execution_unit u join pro_execution_disposition d on d.sn_id=u.sn_id join pro_execution_task e on e.source_disposition_id=d.disposition_id where e.task_id=? and u.current_task_id=e.task_id and u.unit_state='REWORK_IN_PROGRESS' and d.decision='REWORK' for update",taskId);
            if(units.size()!=1)throw ExecutionException.stateConflict("返工来源与单件状态不符");
            Long inspection=db.queryForObject("select task_id from pro_execution_task where workorder_id=? and task_kind='NORMAL' and step_kind='INSPECTION'",Long.class,order);
            ExecutionDispositionService.one(db.update("update pro_execution_unit set current_task_id=?,unit_state='WAIT_REINSPECTION',version=version+1,updated_at=current_timestamp(3) where sn_id=? and current_task_id=? and unit_state='REWORK_IN_PROGRESS'",inspection,units.get(0).get("sn_id"),taskId));
            ExecutionDispositionService.one(db.update("update pro_execution_task set actual_ended_at=current_timestamp(3),ended_by=? where task_id=? and actual_ended_at is null and actual_started_at<=current_timestamp(3)",SecurityUtils.getUserId(),taskId));
            ExecutionDispositionService.one(db.update("update pro_task set status='FINISHED',quantity_produced=1 where task_id=? and status='STARTED'",taskId));
            ExecutionDispositionService.one(db.update("update pro_workorder set version=version+1 where workorder_id=? and status='CONFIRMED'",order));
            return json.createObjectNode().put("state","WAIT_REINSPECTION").put("quantity",1);
        }
        );
    }
}
