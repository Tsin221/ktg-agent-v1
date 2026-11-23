package com.ktg.mes.pro.execution;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.ktg.common.core.domain.AjaxResult;
import com.ktg.common.utils.SecurityUtils;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import javax.sql.DataSource;
import java.util.*;
@Service
public class ExecutionPackingService {
    private final JdbcTemplate db;
    private final ExecutionOperationService operations;
    private final ExecutionOwnershipService ownership;
    private final ObjectMapper json;
    public ExecutionPackingService(DataSource source,ExecutionOperationService operations,ExecutionOwnershipService ownership,ObjectMapper json) {
        db=new JdbcTemplate(source);this.operations=operations;this.ownership=ownership;this.json=json;
    }
    public AjaxResult pack(Long taskId,ExecutionPackingRequest request) {
        ExecutionTaskQueryService.positive(taskId);
        if(request==null||request.snIds==null||request.snIds.isEmpty()||request.snIds.size()>200)throw ExecutionException.validation("请选择1至200件SN");
        TreeSet<Long> ids=new TreeSet<>();
        for(Long id:request.snIds) {ExecutionTaskQueryService.positive(id);if(!ids.add(id))throw ExecutionException.validation("SN不能重复");}
        if(request.packageLineId!=null)ExecutionTaskQueryService.positive(request.packageLineId);
        request.snIds=new ArrayList<>(ids);
        ExecutionOwnershipService.TaskScope scope=ownership.task(taskId);
        if(scope==null)throw ExecutionException.notFound();
        return operations.execute(request.operationId,ExecutionAction.PACK,scope.workorderId,taskId,null,json.valueToTree(request),()->record(scope.workorderId,taskId,request));
    }
    private ObjectNode record(long order,long taskId,ExecutionPackingRequest request) {
        if(!"CONFIRMED".equals(db.queryForObject("select status from pro_workorder where workorder_id=?",String.class,order)))throw ExecutionException.stateConflict("工单尚未下达或已结束");
        List<Map<String,Object>> tasks=db.queryForList("select t.*,e.step_kind,e.task_kind,e.started_by,e.actual_started_at,e.actual_ended_at from pro_execution_task e join pro_task t on t.task_id=e.task_id and t.workorder_id=e.workorder_id where e.workorder_id=? order by e.task_id for update",order);
        Map<String,Object> task=tasks.stream().filter(t->number(t,"task_id")==taskId).findFirst().orElseThrow(ExecutionException::notFound);
        if(!"NORMAL".equals(task.get("task_kind"))||!"PACKING".equals(task.get("step_kind"))||!"STARTED".equals(task.get("status"))||task.get("actual_started_at")==null||task.get("actual_ended_at")!=null||!Objects.equals(SecurityUtils.getUserId(),task.get("started_by")))throw ExecutionException.stateConflict("包装任务须由受权人员开工");
        if(db.queryForList("select workstation_id from md_workstation where workstation_id=? and process_id=? and enable_flag='Y' for update",task.get("workstation_id"),task.get("process_id")).isEmpty())throw ExecutionException.stateConflict("包装工作站不可用");
        List<Map<String,Object>> units=db.queryForList("select * from pro_execution_unit where workorder_id=? order by sn_id for update",order);
        for(Long sn:request.snIds) {
            Map<String,Object> u=units.stream().filter(x->number(x,"sn_id")==sn).findFirst().orElseThrow(ExecutionException::notFound);
            if(!"WAIT_PACKING".equals(u.get("unit_state"))||number(u,"current_task_id")!=taskId)throw ExecutionException.stateConflict("所选单件尚未合格待包装或已包装");
            if(db.queryForObject("select count(*) from pro_execution_inspection i join qc_ipqc q on q.ipqc_id=i.ipqc_id join wm_product_produce_line l on l.line_id=i.produce_line_id join wm_product_produce p on p.record_id=l.record_id where i.sn_id=? and q.check_result='ACCEPT' and q.workorder_id=? and p.workorder_id=?",Integer.class,sn,order,order)!=1)throw ExecutionException.stateConflict("单件缺少合法质量放行产出事实");
            if(db.queryForObject("select count(*) from pro_execution_pack where sn_id=?",Integer.class,sn)!=0)throw ExecutionException.stateConflict("单件已经包装");
        }
        if(request.packageLineId!=null && db.queryForList("select l.line_id from wm_package_line l join wm_package p on p.package_id=l.package_id where l.line_id=? and l.workorder_id=? and l.item_id=? for update",request.packageLineId,order,task.get("item_id")).size()!=1)throw ExecutionException.stateConflict("箱明细必须属于当前工单及产品");
        for(Long sn:request.snIds) {
            db.update("insert into pro_execution_pack(sn_id,task_id,packed_by,packed_at,package_line_id) values(?,?,?,current_timestamp(3),?)",sn,taskId,SecurityUtils.getUserId(),request.packageLineId);
            if(db.update("update pro_execution_unit set unit_state='PACKED',version=version+1,updated_at=current_timestamp(3) where sn_id=? and current_task_id=? and unit_state='WAIT_PACKING'",sn,taskId)!=1)throw ExecutionException.stateConflict("单件状态已变化");
        }
        long packed=db.queryForObject("select count(*) from pro_execution_pack where task_id=?",Long.class,taskId);
        db.update("update pro_task set quantity_produced=?,quantity_quanlify=? where task_id=?",packed,packed,taskId);
        if(db.update("update pro_workorder set version=version+1 where workorder_id=? and status='CONFIRMED'",order)!=1)throw ExecutionException.stateConflict("工单已变化");
        return json.createObjectNode().put("state","PACKED").put("packed",packed).put("snCount",request.snIds.size()).put("version",db.queryForObject("select version from pro_workorder where workorder_id=?",Long.class,order));
    }
    private static long number(Map<String,Object> row,String key) {return ((Number)row.get(key)).longValue();}
}
