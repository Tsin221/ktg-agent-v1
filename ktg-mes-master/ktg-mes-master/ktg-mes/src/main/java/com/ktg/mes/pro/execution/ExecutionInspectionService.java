package com.ktg.mes.pro.execution;
import com.fasterxml.jackson.databind.*;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.ktg.common.core.domain.AjaxResult;
import com.ktg.common.utils.SecurityUtils;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import javax.sql.DataSource;
import java.util.*;
@Service public class ExecutionInspectionService  {
    private final JdbcTemplate db;
    private final ExecutionOperationService operations;
    private final ExecutionOwnershipService ownership;
    private final ExecutionInspectionRecordService records;
    private final ExecutionQualifiedOutputService output;
    private final ObjectMapper json;
    public ExecutionInspectionService(DataSource ds,ExecutionOperationService operations,ExecutionOwnershipService ownership,ExecutionInspectionRecordService records,ExecutionQualifiedOutputService output,ObjectMapper json) {
        db=new JdbcTemplate(ds);
        this.operations=operations;
        this.ownership=ownership;
        this.records=records;
        this.output=output;
        this.json=json;
    }
    public AjaxResult inspect(Long sn,ExecutionInspectionRequest r) {
        if(r==null||r.taskId==null||r.taskId<=0||sn==null||sn<=0)throw ExecutionException.validation("检验目标无效");
        ExecutionOwnershipService.TaskScope task=ownership.task(r.taskId);
        if(task==null)throw ExecutionException.notFound();
        JsonNode snapshot=records.snapshot(db.queryForObject("select inspection_snapshot_json from pro_execution_task where task_id=?",String.class,r.taskId));
        boolean accepted=records.validate(snapshot,r,false);
        r.indices=new ArrayList<>(r.indices);
        r.indices.sort(Comparator.comparing(i->i.indexId));
        return operations.execute(r.operationId,accepted?ExecutionAction.INSPECT_ACCEPT:ExecutionAction.INSPECT_REJECT,task.workorderId,r.taskId,sn,json.valueToTree(r),()->record(task.workorderId,sn,r));
    }
    private ObjectNode record(long order,long sn,ExecutionInspectionRequest r) {
        Map<String,Object> task=lockTask(order,r.taskId);
        JsonNode snapshot=records.snapshot(task.get("inspection_snapshot_json"));
        boolean accepted=records.validate(snapshot,r);
        r.indices=new ArrayList<>(r.indices);
        r.indices.sort(Comparator.comparing(i->i.indexId));
        List<Map<String,Object>> units=db.queryForList("select * from pro_execution_unit where workorder_id=? order by sn_id for update",order);
        Map<String,Object> unit=units.stream().filter(u->num(u,"sn_id")==sn).findFirst().orElseThrow(ExecutionException::notFound);
        String state=String.valueOf(unit.get("unit_state"));
        if(num(unit,"current_task_id")!=r.taskId||unit.get("assembly_feedback_id")==null||!("WAIT_INSPECTION".equals(state)||"WAIT_REINSPECTION".equals(state)))throw ExecutionException.stateConflict("单件尚未到达当前检测任务");
        List<Map<String,Object>> previous=db.queryForList("select * from pro_execution_inspection where sn_id=? order by round_no desc for update",sn);
        int round=previous.size()+1;
        Long source=null;
        if("WAIT_INSPECTION".equals(state)&&!previous.isEmpty())throw ExecutionException.stateConflict("首检已提交");
        if("WAIT_REINSPECTION".equals(state)) {
            if(previous.isEmpty())throw ExecutionException.stateConflict("缺少首检失败事实");
            List<Long> dispositions=db.queryForList("select d.disposition_id from pro_execution_disposition d join pro_execution_task e on e.source_disposition_id=d.disposition_id join pro_task t on t.task_id=e.task_id where d.inspection_id=? and d.sn_id=? and d.decision='REWORK' and t.status='FINISHED' and e.actual_ended_at is not null",Long.class,previous.get(0).get("inspection_id"),sn);
            if(dispositions.size()!=1)throw ExecutionException.stateConflict("返工尚未完成");
            source=dispositions.get(0);
        }
        long ipqc=records.record(task,snapshot,r,accepted);
        Long line=accepted?output.produce(task,sn):null;
        long inspection=ExecutionFactWriter.insert(db,"pro_execution_inspection","inspection_id",ExecutionFactWriter.values("sn_id",sn,"task_id",r.taskId,"ipqc_id",ipqc,"round_no",round,"source_disposition_id",source,"produce_line_id",line));
        long target=r.taskId;
        if(accepted) {
            target=db.queryForObject("select task_id from pro_execution_task where workorder_id=? and step_kind='PACKING' and task_kind='NORMAL'",Long.class,order);
            db.update("update pro_task set status='READY' where task_id=? and status='WAITING'",target);
        }
        String next=accepted?"WAIT_PACKING":"WAIT_DISPOSITION";
        if(db.update("update pro_execution_unit set current_task_id=?,unit_state=?,version=version+1,updated_at=current_timestamp(3) where sn_id=? and unit_state=? and current_task_id=?",target,next,sn,state,r.taskId)!=1)throw ExecutionException.stateConflict("单件状态已变化");
        long qualified=db.queryForObject("select count(*) from pro_execution_unit where workorder_id=? and unit_state in ('WAIT_PACKING','PACKED')",Long.class,order);
        db.update("update pro_task set quantity_produced=(select count(distinct sn_id) from pro_execution_inspection where task_id=?),quantity_quanlify=?,quantity_unquanlify=(select count(distinct i.sn_id) from pro_execution_inspection i join pro_execution_unit u on u.sn_id=i.sn_id where i.task_id=? and u.unit_state not in ('WAIT_PACKING','PACKED')) where task_id=?",r.taskId,qualified,r.taskId,r.taskId);
        long version=updateOrder(order,qualified);
        ObjectNode result=json.createObjectNode().put("inspectionId",inspection).put("ipqcId",ipqc).put("roundNo",round).put("state",next).put("qualified",qualified).put("version",version);
        if(line!=null)result.put("produceLineId",line);
        return result;
    }
    public AjaxResult complete(Long task,String operationId) {
        ExecutionOwnershipService.TaskScope scope=ownership.task(task);
        if(scope==null)throw ExecutionException.notFound();
        return operations.execute(operationId,ExecutionAction.TASK_COMPLETE,scope.workorderId,task,null,json.createObjectNode().put("operationId",operationId),()-> {
            lockTask(scope.workorderId,task);List<Map<String,Object>> units=db.queryForList("select unit_state from pro_execution_unit where workorder_id=? order by sn_id for update",scope.workorderId);if(units.isEmpty()||units.stream().anyMatch(u->!Arrays.asList("WAIT_PACKING","PACKED","SCRAPPED").contains(u.get("unit_state"))))throw ExecutionException.stateConflict("仍有待检或待处置单件"); if(db.update("update pro_execution_task set actual_ended_at=current_timestamp(3),ended_by=? where task_id=? and actual_ended_at is null and actual_started_at<=current_timestamp(3)",SecurityUtils.getUserId(),task)!=1)throw ExecutionException.stateConflict("检测任务计时无效");db.update("update pro_task set status='FINISHED' where task_id=? and status='STARTED'",task);long qualified=units.stream().filter(u->!"SCRAPPED".equals(u.get("unit_state"))).count();return json.createObjectNode().put("state","FINISHED").put("version",updateOrder(scope.workorderId,qualified));
        }
        );
    }
    private Map<String,Object> lockTask(long order,long task) {
        if(!"CONFIRMED".equals(db.queryForObject("select status from pro_workorder where workorder_id=?",String.class,order)))throw ExecutionException.stateConflict("工单未下达或已结束");
        List<Map<String,Object>> tasks=db.queryForList("select t.*,e.task_kind,e.step_kind,e.inspection_template_id,e.inspection_type,e.inspection_snapshot_json,e.actual_started_at,e.actual_ended_at,e.started_by,e.assigned_user_id from pro_execution_task e join pro_task t on t.task_id=e.task_id and t.workorder_id=e.workorder_id where e.workorder_id=? order by e.task_id for update",order);
        Map<String,Object> t=tasks.stream().filter(x->num(x,"task_id")==task).findFirst().orElseThrow(ExecutionException::notFound);
        if(!"INSPECTION".equals(t.get("step_kind"))||!"NORMAL".equals(t.get("task_kind"))||!"STARTED".equals(t.get("status"))||t.get("actual_started_at")==null||t.get("started_by")==null||t.get("actual_ended_at")!=null||!Objects.equals(SecurityUtils.getUserId(),t.get("assigned_user_id")))throw ExecutionException.stateConflict("检测任务须已开工且由当前指派人员执行");
        if(db.queryForList("select workstation_id from md_workstation where workstation_id=? and process_id=? and enable_flag='Y' for update",t.get("workstation_id"),t.get("process_id")).isEmpty())throw ExecutionException.stateConflict("工作站不可用");
        return t;
    }
    private long updateOrder(long id,long qualified) {
        if(db.update("update pro_workorder set quantity_produced=?,version=version+1 where workorder_id=? and status='CONFIRMED'",qualified,id)!=1)throw ExecutionException.stateConflict("工单已变化");
        return db.queryForObject("select version from pro_workorder where workorder_id=?",Long.class,id);
    }
    private static long num(Map<String,Object> m,String k) {
        return ((Number)m.get(k)).longValue();
    }
}
