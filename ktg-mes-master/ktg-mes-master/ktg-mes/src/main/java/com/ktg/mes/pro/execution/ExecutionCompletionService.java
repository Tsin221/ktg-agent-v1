package com.ktg.mes.pro.execution;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.ktg.common.core.domain.AjaxResult;
import com.ktg.common.utils.SecurityUtils;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import javax.sql.DataSource;
import java.util.*;
@Service
public class ExecutionCompletionService {
    private final JdbcTemplate db;
    private final ExecutionOperationService operations;
    private final ExecutionOrderProgressService progress;
    private final ExecutionTaskQueryService tasks;
    private final ObjectMapper json;
    public ExecutionCompletionService(DataSource ds,ExecutionOperationService operations,ExecutionOrderProgressService progress,ExecutionTaskQueryService tasks,ObjectMapper json) {db=new JdbcTemplate(ds);this.operations=operations;this.progress=progress;this.tasks=tasks;this.json=json;}
    public AjaxResult complete(Long id,ExecutionCompletionRequest r) {
        if(r==null||r.version==null||r.version<0)throw ExecutionException.validation("完结需要当前版本");
        return operations.execute(r.operationId,ExecutionAction.COMPLETE,id,null,null,json.createObjectNode().put("version",r.version),()->{
            lock(id);
            if(!r.version.equals(db.queryForObject("select version from pro_workorder where workorder_id=?",Long.class,id)))throw new ExecutionException(409,"VERSION_CONFLICT","工单版本已变化");
            Map<String,Object> p=progress.read(id);
            if(!Boolean.TRUE.equals(p.get("completionAllowed")))throw ExecutionException.stateConflict(String.join("；",(List<String>)p.get("completionReasons")));
            one(db.update("update pro_workorder set status='FINISHED',quantity_scheduled=?,quantity_produced=?,version=version+1 where workorder_id=? and version=? and status='CONFIRMED'",p.get("allocated"),p.get("qualified"),id,r.version));
            return json.createObjectNode().put("state","FINISHED").put("version",r.version+1).put("packed",((Number)p.get("packed")).longValue()).put("scrapped",((Number)p.get("scrapped")).longValue());
        });
    }
    public AjaxResult completePacking(Long taskId,String operationId) {
        Long id=tasks.orderId(taskId);
        return operations.execute(operationId,ExecutionAction.TASK_COMPLETE,id,taskId,null,json.createObjectNode(),()->{
            lock(id);
            Map<String,Object> task=tasks.taskRow(taskId,true);
            if(!"CONFIRMED".equals(task.get("order_status"))||!"NORMAL".equals(task.get("task_kind"))||!"PACKING".equals(task.get("step_kind")))throw ExecutionException.stateConflict("包装任务无效");
            if(!tasks.legalWorkstation(task,true))throw ExecutionException.stateConflict("包装工作站已失效");
            Map<String,Object> p=progress.read(id);
            long allocated=((Number)p.get("allocated")).longValue(),packed=((Number)p.get("packed")).longValue(),scrapped=((Number)p.get("scrapped")).longValue();
            if(!Boolean.TRUE.equals(p.get("terminalFactsValid")))throw ExecutionException.stateConflict("终态缺少合法包装或报废事实");
            if(allocated!=((Number)p.get("planned")).longValue()||allocated==0||packed+scrapped!=allocated)throw ExecutionException.stateConflict("仍有待检、待处置或待包装单件");
            boolean zero=packed==0&&scrapped==allocated&&Arrays.asList("WAITING","READY").contains(task.get("status"));
            if(!zero&&(!"STARTED".equals(task.get("status"))||task.get("actual_started_at")==null||!SecurityUtils.getUserId().equals(task.get("started_by"))))throw ExecutionException.stateConflict("包装任务尚未合法开工");
            one(db.update("update pro_execution_task set actual_ended_at=current_timestamp(3),ended_by=? where task_id=? and actual_ended_at is null and (actual_started_at is null or actual_started_at<=current_timestamp(3))",SecurityUtils.getUserId(),taskId));
            one(db.update("update pro_task set status='FINISHED',quantity_produced=? where task_id=? and status=?",packed,taskId,task.get("status")));
            one(db.update("update pro_workorder set version=version+1 where workorder_id=? and status='CONFIRMED'",id));
            return json.createObjectNode().put("state","FINISHED").put("packed",packed);
        });
    }
    private void lock(Long id) {db.queryForList("select e.task_id from pro_execution_task e join pro_task t on t.task_id=e.task_id and t.workorder_id=e.workorder_id where e.workorder_id=? order by e.task_id for update",id);db.queryForList("select sn_id from pro_execution_unit where workorder_id=? order by sn_id for update",id);}
    private void one(int count) {if(count!=1)throw ExecutionException.stateConflict("状态已变化");}
}
