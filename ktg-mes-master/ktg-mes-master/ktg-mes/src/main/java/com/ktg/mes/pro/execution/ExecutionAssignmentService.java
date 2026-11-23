package com.ktg.mes.pro.execution;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.ktg.common.core.domain.AjaxResult;
import com.ktg.common.core.domain.entity.SysUser;
import com.ktg.system.service.ISysMenuService;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import javax.sql.DataSource;
import java.util.*;

/** Coordinator assignment, independent of untrusted legacy self-service workstation bindings. */
@Service
public class ExecutionAssignmentService {
    private static final String ACTIVE_USERS = "select user_id,user_name,nick_name from sys_user where status='0' and del_flag='0'";
    private final JdbcTemplate db;
    private final ExecutionTaskQueryService tasks;
    private final ExecutionOperationService operations;
    private final ExecutionAccessService access;
    private final ISysMenuService menus;
    private final ObjectMapper json;

    public ExecutionAssignmentService(DataSource source,ExecutionTaskQueryService tasks,ExecutionOperationService operations,
                                      ExecutionAccessService access, ISysMenuService menus,ObjectMapper json) {
        this.db=new JdbcTemplate(source);this.tasks=tasks;this.operations=operations;this.access=access;this.menus=menus;this.json=json;
    }

    @Transactional(readOnly=true)
    public List<Map<String,Object>> options(Long taskId) {
        Map<String,Object> task=tasks.authorizedTask(taskId);
        Long orderId=ExecutionTaskQueryService.number(task.get("workorder_id"));
        access.requireOperation(ExecutionAction.ASSIGN,orderId,taskId,null);
        if(!assignable(task)||!tasks.legalWorkstation(task,false)) throw ExecutionException.stateConflict("任务当前不可分派");
        String required=ExecutionTaskQueryService.stepPermission(task);
        List<Map<String,Object>> users=db.queryForList(ACTIVE_USERS+" order by user_id");
        users.removeIf(user->!canExecute(ExecutionTaskQueryService.number(user.get("user_id")),required));
        return ExecutionTaskQueryService.camelRows(users);
    }

    public AjaxResult assign(Long taskId,ExecutionTaskAssignmentRequest request) {
        if(request==null || request.assignedUserId==null || request.assignedUserId<=0)
            throw ExecutionException.validation("请选择合法的执行人员");
        Long orderId=tasks.orderId(taskId);
        ObjectNode payload=json.createObjectNode().put("assignedUserId",request.assignedUserId);
        return operations.execute(request.operationId,ExecutionAction.ASSIGN,orderId,taskId,null,payload,()->{
            Map<String,Object> task=tasks.taskRow(taskId,true);
            if(!orderId.equals(ExecutionTaskQueryService.number(task.get("workorder_id"))) || !assignable(task))
                throw ExecutionException.stateConflict("仅待开工任务可分派");
            if(!tasks.legalWorkstation(task,true)) throw ExecutionException.validation("工作站不存在、停用或不属于该工序");
            Long user=request.assignedUserId;
            if(db.queryForList(ACTIVE_USERS+" and user_id=? for update",user).isEmpty()
                    || !canExecute(user,ExecutionTaskQueryService.stepPermission(task)))
                throw ExecutionException.validation("执行人员无有效账号或对应工序权限");
            if(!user.equals(task.get("assigned_user_id"))) {
                if(db.update("update pro_execution_task set assigned_user_id=? where task_id=? and actual_started_at is null and actual_ended_at is null",user,taskId)!=1)
                    throw ExecutionException.stateConflict("任务分派状态已变化");
                if(db.update("update pro_workorder set version=version+1 where workorder_id=? and status='CONFIRMED'",orderId)!=1)
                    throw ExecutionException.stateConflict("工单状态已变化");
            }
            return json.createObjectNode().put("assignedUserId",user).put("state",(String)task.get("status"));
        });
    }

    private boolean canExecute(Long user,String stepPermission) {
        if(SysUser.isAdmin(user)) return true;
        Set<String> perms=menus.selectMenuPermsByUserId(user);
        return perms!=null && (perms.contains("*:*:*") || (perms.contains("mes:pro:execution:query")
                && perms.contains("mes:pro:execution:start") && perms.contains(stepPermission)));
    }

    static boolean assignable(Map<String,Object> task) {
        return "CONFIRMED".equals(task.get("order_status")) && ("WAITING".equals(task.get("status"))||"READY".equals(task.get("status")))
                && task.get("actual_started_at")==null && task.get("actual_ended_at")==null;
    }
}
