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
public class ExecutionDispositionService  {
    private final JdbcTemplate db;
    private final ExecutionOperationService operations;
    private final ObjectMapper json;
    public ExecutionDispositionService(DataSource ds,ExecutionOperationService operations,ObjectMapper json) {
        db=new JdbcTemplate(ds);
        this.operations=operations;
        this.json=json;
    }
    public AjaxResult dispose(Long snId,ExecutionDispositionRequest r) {
        ExecutionTaskQueryService.positive(snId);
        if(r==null)throw ExecutionException.validation("处置请求不能为空");
        r.validate();
        List<Map<String,Object>> units=db.queryForList("select workorder_id from pro_execution_unit where sn_id=?",snId);
        if(units.size()!=1)throw ExecutionException.notFound();
        Long order=((Number)units.get(0).get("workorder_id")).longValue();
        return operations.execute(r.operationId,"REWORK".equals(r.decision)?ExecutionAction.DISPOSITION_REWORK:ExecutionAction.DISPOSITION_SCRAP,order,null,snId,json.valueToTree(r),()-> {
            if(!"CONFIRMED".equals(db.queryForObject("select status from pro_workorder where workorder_id=?",String.class,order)))throw ExecutionException.stateConflict("工单不可执行");
            db.queryForList("select task_id from pro_execution_task where workorder_id=? order by task_id for update",order);
            Map<String,Object> u=db.queryForMap("select * from pro_execution_unit where sn_id=? for update",snId);
            if(!"WAIT_DISPOSITION".equals(u.get("unit_state")))throw ExecutionException.stateConflict("单件不在待处置状态");
            List<Map<String,Object>> failures=db.queryForList("select i.inspection_id,i.task_id,i.produce_line_id,q.workorder_id,q.task_id as qc_task_id,q.check_result from pro_execution_inspection i join qc_ipqc q on q.ipqc_id=i.ipqc_id where i.sn_id=? order by i.round_no desc limit 1 for update",snId);
            if(failures.size()!=1) throw ExecutionException.stateConflict("缺少最新失败检验");
            Map<String,Object> failure=failures.get(0);
            if(!order.equals(((Number)failure.get("workorder_id")).longValue()) || !failure.get("task_id").equals(failure.get("qc_task_id")) || !failure.get("task_id").equals(u.get("current_task_id")) || failure.get("produce_line_id")!=null) throw ExecutionException.stateConflict("检验与单件来源不一致");
            if(!r.inspectionId.equals(((Number)failure.get("inspection_id")).longValue())||!"REJECT".equals(failure.get("check_result"))||!db.queryForList("select disposition_id from pro_execution_disposition where inspection_id=?",r.inspectionId).isEmpty())throw ExecutionException.stateConflict("必须处置最新尚未处置的不合格检验");
            db.update("insert into pro_execution_disposition(inspection_id,sn_id,decision,reason,decided_by,decided_at) values(?,?,?,?,?,current_timestamp(3))",r.inspectionId,snId,r.decision,r.reason,SecurityUtils.getUserId());
            Long disposition=db.queryForObject("select disposition_id from pro_execution_disposition where inspection_id=?",Long.class,r.inspectionId);
            ObjectNode result=json.createObjectNode().put("dispositionId",disposition);
            if("REWORK".equals(r.decision)) {
                // Reuse the frozen assembly workstation and process identity; never a parallel workorder.
                Long assembly=db.queryForObject("select task_id from pro_execution_task where workorder_id=? and task_kind='NORMAL' and step_kind='ASSEMBLY'",Long.class,order);
                String code="RW-"+disposition;
                db.update("insert into pro_task(task_code,task_name,workorder_id,workorder_code,workorder_name,workstation_id,workstation_code,workstation_name,process_id,process_code,process_name,item_id,item_code,item_name,specification,unit_of_measure,quantity,quantity_produced,quantity_quanlify,quantity_unquanlify,status,create_by,create_time) select ?,?,workorder_id,workorder_code,workorder_name,workstation_id,workstation_code,workstation_name,process_id,process_code,process_name,item_id,item_code,item_name,specification,unit_of_measure,1,0,0,0,'READY',?,current_timestamp(3) from pro_task where task_id=?",code,"单件返工 "+snId,SecurityUtils.getUsername(),assembly);
                Long task=db.queryForObject("select task_id from pro_task where task_code=?",Long.class,code);
                db.update("insert into pro_execution_task(task_id,workorder_id,process_id,process_code,process_name,step_kind,task_kind,source_disposition_id) select ?,workorder_id,process_id,process_code,process_name,step_kind,'REWORK',? from pro_execution_task where task_id=?",task,disposition,assembly);
                one(db.update("update pro_execution_unit set current_task_id=?,unit_state='REWORK_IN_PROGRESS',version=version+1,updated_at=current_timestamp(3) where sn_id=? and unit_state='WAIT_DISPOSITION'",task,snId));
                result.put("createdTaskId",task).put("state","REWORK_IN_PROGRESS");
            }
            else {
                one(db.update("update pro_execution_unit set unit_state='SCRAPPED',version=version+1,updated_at=current_timestamp(3) where sn_id=? and unit_state='WAIT_DISPOSITION'",snId));
                result.put("state","SCRAPPED");
            }
            one(db.update("update pro_workorder set version=version+1 where workorder_id=? and status='CONFIRMED'",order));
            return result;
        });
    }
    static void one(int n) {
        if(n!=1)throw ExecutionException.stateConflict("执行事实已变化");
    }
}
