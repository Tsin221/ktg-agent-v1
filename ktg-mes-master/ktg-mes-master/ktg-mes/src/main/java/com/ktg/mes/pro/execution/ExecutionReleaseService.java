package com.ktg.mes.pro.execution;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.ktg.common.core.domain.AjaxResult;
import com.ktg.common.utils.SecurityUtils;
import com.ktg.common.constant.UserConstants;
import com.ktg.system.strategy.AutoCodeUtil;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.support.GeneratedKeyHolder;
import org.springframework.stereotype.Service;
import javax.sql.DataSource;
import java.sql.Statement;
import java.math.BigDecimal;
import java.util.*;

/** All writes join the operation service's outer workorder lock and transaction. */
@Service
public class ExecutionReleaseService {
    private final JdbcTemplate db;
    private final ExecutionOperationService operations;
    private final ExecutionAccessService access;
    private final ObjectMapper json;
    private final AutoCodeUtil codes;
    public ExecutionReleaseService(DataSource source, ExecutionOperationService operations,
            ExecutionAccessService access, ObjectMapper json, AutoCodeUtil codes) {
        this.db = new JdbcTemplate(source); this.operations=operations; this.access=access;
        this.json=json; this.codes=codes;
    }
    public AjaxResult release(Long id, ExecutionReleaseRequest request) {
        validateRequest(request);
        return operations.execute(request.operationId, ExecutionAction.RELEASE, id, null, null,
                json.valueToTree(request), () -> create(id, request));
    }
    static void validateRequest(ExecutionReleaseRequest r) {
        if(r==null || r.expectedVersion==null || r.expectedVersion<0 || r.routeId==null || r.routeId<=0
                || r.steps==null || r.steps.size()!=3) throw ExecutionException.validation("下达须选择版本、路线及三道工位");
        Set<Long> ids=new HashSet<>();
        for(ExecutionReleaseRequest.Step s:r.steps) {
            if(s==null || s.routeProcessId==null || s.routeProcessId<=0 || s.workstationId==null
                    || s.workstationId<=0 || !ids.add(s.routeProcessId)) throw ExecutionException.validation("步骤或工位无效/重复");
        }
    }
    private ObjectNode create(Long id, ExecutionReleaseRequest r) {
        Map<String,Object> w=one("select * from pro_workorder where workorder_id=?",id);
        if(!"PREPARE".equals(w.get("status"))) throw ExecutionException.stateConflict("仅草稿工单允许下达");
        if(number(w.get("version"))!=r.expectedVersion) throw new ExecutionException(409,"VERSION_CONFLICT","工单版本已变化");
        if(!db.queryForList("select task_id from pro_task where workorder_id=?",id).isEmpty())
            throw ExecutionException.stateConflict("工单已有历史排产任务，不能重复生成");
        BigDecimal quantity=new BigDecimal(w.get("quantity").toString());
        int count;
        try { count=quantity.intValueExact(); } catch(ArithmeticException bad) { throw ExecutionException.validation("计划数量须为整数"); }
        if(count<=0) throw ExecutionException.validation("计划数量须为正数");
        one("select item_id from md_item where item_id=? and enable_flag='Y' and item_or_product='PRODUCT'",w.get("product_id"));
        Map<String,Object> route=one("select r.* from pro_route r where r.route_id=? and r.enable_flag='Y' and exists(select 1 from pro_route_product p where p.route_id=r.route_id and p.item_id=?) for update",r.routeId,w.get("product_id"));
        List<Map<String,Object>> steps=routeSteps(r.routeId,true);
        validateRoute(steps);
        List<Map<String,Object>> stations=new ArrayList<>();
        List<String> snapshots=new ArrayList<>();
        for(int i=0;i<3;i++) {
            Map<String,Object> s=steps.get(i); ExecutionReleaseRequest.Step choice=r.steps.get(i);
            if(number(s.get("record_id"))!=choice.routeProcessId) throw ExecutionException.validation("步骤顺序与路线不一致");
            one("select process_id from pro_process where process_id=? and enable_flag='Y' for update",s.get("process_id"));
            stations.add(one("select * from md_workstation where workstation_id=? and process_id=? and enable_flag='Y' for update",choice.workstationId,s.get("process_id")));
            if(i==1) snapshots.add(template(choice,w.get("product_id")));
            else { if(choice.templateId!=null || choice.ipqcType!=null) throw ExecutionException.validation("仅检测步骤可选择模板"); snapshots.add(null); }
        }
        List<Map<String,Object>> bom=db.queryForList("select * from pro_workorder_bom where workorder_id=? order by line_id for update",id);
        String bomSnapshot=encode(bom);
        try {
            ExecutionMaterialRequirements.parse(json,count,bomSnapshot);
        } catch (ExecutionException invalid) {
            throw ExecutionException.validation("请检查并修改工单 BOM："+invalid.getMessage());
        }
        db.update("insert into pro_execution_order(workorder_id,route_id,route_code,route_name,planned_units,release_user_id,released_at,material_requirements_snapshot) values(?,?,?,?,?,?,current_timestamp(3),?)",id,r.routeId,route.get("route_code"),route.get("route_name"),count,SecurityUtils.getUserId(),bomSnapshot);
        List<Long> tasks=new ArrayList<>();
        for(int i=0;i<3;i++) {
            Map<String,Object> s=steps.get(i), station=stations.get(i);
            long task=insert("insert into pro_task(task_code,task_name,workorder_id,workorder_code,workorder_name,workstation_id,workstation_code,workstation_name,process_id,process_code,process_name,item_id,item_code,item_name,specification,unit_of_measure,quantity,quantity_produced,quantity_quanlify,quantity_unquanlify,start_time,end_time,request_date,status,create_by,create_time) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,0,?,?,?,?,?,current_timestamp(3))",
                "EX-"+id+"-"+(i+1),s.get("process_name"),id,w.get("workorder_code"),w.get("workorder_name"),station.get("workstation_id"),station.get("workstation_code"),station.get("workstation_name"),s.get("process_id"),s.get("process_code"),s.get("process_name"),w.get("product_id"),w.get("product_code"),w.get("product_name"),w.get("product_spc"),w.get("unit_of_measure"),count,w.get("request_date"),w.get("request_date"),w.get("request_date"),i==0?"READY":"WAITING",SecurityUtils.getUsername());
            tasks.add(task); ExecutionReleaseRequest.Step c=r.steps.get(i);
            db.update("insert into pro_execution_task(task_id,workorder_id,normal_step_no,route_process_id,process_id,process_code,process_name,step_kind,task_kind,inspection_template_id,inspection_type,inspection_snapshot_json) values(?,?,?,?,?,?,?,?,'NORMAL',?,?,?)",task,id,i+1,s.get("record_id"),s.get("process_id"),s.get("process_code"),s.get("process_name"),s.get("execution_kind"),c.templateId,c.ipqcType,snapshots.get(i));
        }
        // Hold the shared allocator rule until this entire batch commits, including across workorders.
        one("select rule_id from sys_auto_code_rule where rule_code=? for update",UserConstants.SN_CODE);
        for(int i=0;i<count;i++) {
            String code=codes.genSerialCode(UserConstants.SN_CODE,String.valueOf(w.get("product_code")));
            if(code==null || code.trim().isEmpty() || code.length()>64) throw ExecutionException.validation("SN编码规则未生成有效编码");
            long sn=insert("insert into wm_sn(sn_code,item_id,item_code,item_name,specification,unit_of_measure,create_by,create_time) values(?,?,?,?,?,?,?,current_timestamp(3))",code,w.get("product_id"),w.get("product_code"),w.get("product_name"),w.get("product_spc"),w.get("unit_of_measure"),SecurityUtils.getUsername());
            db.update("insert into pro_execution_unit(sn_id,workorder_id,current_task_id,unit_state,version,created_at,updated_at) values(?,?,?,'WAIT_ASSEMBLY',0,current_timestamp(3),current_timestamp(3))",sn,id,tasks.get(0));
        }
        if(db.update("update pro_workorder set status='CONFIRMED',version=version+1,quantity_scheduled=?,quantity_produced=0 where workorder_id=? and version=? and status='PREPARE'",count,id,r.expectedVersion)!=1) throw ExecutionException.stateConflict("工单已变化");
        ObjectNode result=json.createObjectNode().put("state","CONFIRMED").put("version",r.expectedVersion+1).put("taskCount",3).put("snCount",count).put("allocated",count);
        tasks.forEach(result.putArray("taskIds")::add); return result;
    }
    public Map<String,Object> options(Long id) {
        access.requireOperation(ExecutionAction.RELEASE,id,null,null);
        Map<String,Object> w=one("select * from pro_workorder where workorder_id=?",id);
        return productOptions(w.get("product_id"));
    }
    public Map<String,Object> optionsForProduct(Long productId) {
        access.requireProductReleaseOptions();
        if(productId==null || productId<=0) throw ExecutionException.validation("产品 ID 无效");
        one("select item_id from md_item where item_id=? and enable_flag='Y' and item_or_product='PRODUCT'",productId);
        return productOptions(productId);
    }
    private Map<String,Object> productOptions(Object productId) {
        List<Map<String,Object>> routes=camel(db.queryForList("select r.route_id,r.route_code,r.route_name from pro_route r where r.enable_flag='Y' and exists(select 1 from pro_route_product p where p.route_id=r.route_id and p.item_id=?) order by r.route_id",productId));
        List<Map<String,Object>> valid=new ArrayList<>();
        for(Map<String,Object> route:routes) {
            List<Map<String,Object>> raw=routeSteps(((Number)route.get("routeId")).longValue(),false);
            try{validateRoute(raw);}catch(ExecutionException invalid){continue;}
            List<Map<String,Object>> steps=camel(raw);
            for(Map<String,Object> step:steps) {
                step.put("routeProcessId",step.get("recordId"));
                step.put("workstations",camel(db.queryForList("select workstation_id,workstation_code,workstation_name from md_workstation where process_id=? and enable_flag='Y' order by workstation_id",step.get("processId"))));
                step.put("templates", "INSPECTION".equals(step.get("executionKind")) ? camel(db.queryForList("select t.template_id,t.template_code,t.template_name,t.qc_types from qc_template t where t.enable_flag='Y' and exists(select 1 from qc_template_product p where p.template_id=t.template_id and p.item_id=?) and exists(select 1 from qc_template_index i where i.template_id=t.template_id) order by t.template_id",productId)) : Collections.emptyList());
            }
            for(Map<String,Object> step:steps) { if("INSPECTION".equals(step.get("executionKind"))) { for(Map<String,Object> t:(List<Map<String,Object>>)step.get("templates")) { t.put("qcTypes",Arrays.asList(String.valueOf(t.get("qcTypes")).replace(" ","").split(","))); } } }
            route.put("steps",steps);valid.add(route);
        }
        return Collections.singletonMap("routes",valid);
    }
    @org.springframework.transaction.annotation.Transactional(readOnly=true, isolation=org.springframework.transaction.annotation.Isolation.REPEATABLE_READ)
    public Map<String,Object> detail(Long id) {
        access.requireOrderQuery(id);
        Map<String,Object> result=new LinkedHashMap<>();
        result.put("order",camel(db.queryForList("select w.workorder_id,w.workorder_code,w.version,w.status,e.route_id,e.route_code,e.route_name,e.planned_units,e.material_requirements_snapshot from pro_workorder w left join pro_execution_order e on e.workorder_id=w.workorder_id where w.workorder_id=?",id)).get(0));
        List<Map<String,Object>> tasks=camel(db.queryForList("select e.*,t.task_code,t.status,t.workstation_id,t.workstation_code,t.workstation_name,t.quantity,t.quantity_produced,t.start_time,t.end_time from pro_execution_task e join pro_task t on t.task_id=e.task_id where e.workorder_id=? order by e.normal_step_no,e.task_id",id));
        List<Map<String,Object>> units=camel(db.queryForList("select u.sn_id,s.sn_code,u.unit_state,u.current_task_id,u.version from pro_execution_unit u join wm_sn s on s.sn_id=u.sn_id where u.workorder_id=? order by u.sn_id",id));
        // The compatibility summary is a whole-order projection, never a count of visible SNs.
        ExecutionStatistics statistics=ExecutionStatistics.fromStates(units.stream()
                .map(u->ExecutionState.valueOf(String.valueOf(u.get("unitState"))))
                .collect(java.util.stream.Collectors.toList()));
        // Query scope is checked per task and SN to prevent peer task disclosure.
        tasks.removeIf(t->{try{access.requireTaskQuery(id,((Number)t.get("taskId")).longValue());return false;}catch(ExecutionException denied){return true;}});
        units.removeIf(u->{try{access.requireUnitQuery(id,((Number)u.get("snId")).longValue());return false;}catch(ExecutionException denied){return true;}});
        result.put("tasks",tasks);result.put("units",units);
        Map<String,Integer> summary=new LinkedHashMap<>();
        summary.put("allocated",Math.toIntExact(statistics.getAllocated()));
        summary.put("assembled",Math.toIntExact(statistics.getAssembled()));
        summary.put("qualified",Math.toIntExact(statistics.getQualified()));
        summary.put("packed",Math.toIntExact(statistics.getPacked()));
        summary.put("scrapped",Math.toIntExact(statistics.getScrapped()));
        result.put("summary",summary);return result;
    }
    private List<Map<String,Object>> camel(List<Map<String,Object>> rows) {
        List<Map<String,Object>> out=new ArrayList<>();for(Map<String,Object> row:rows){Map<String,Object> c=new LinkedHashMap<>();row.forEach((key,value)->{StringBuilder name=new StringBuilder();boolean upper=false;for(char ch:key.toCharArray()){if(ch=='_'){upper=true;}else{name.append(upper?Character.toUpperCase(ch):ch);upper=false;}}c.put(name.toString(),value);});out.add(c);}return out;
    }
    private String template(ExecutionReleaseRequest.Step s,Object product) {
        if(s.templateId==null || !Arrays.asList("FIRST","FINAL","PATROL").contains(s.ipqcType)) throw ExecutionException.validation("请选择有效过程检验模板及类型");
        Map<String,Object> t=one("select t.* from qc_template t where template_id=? and enable_flag='Y' and find_in_set(?,replace(qc_types,' ',''))>0 and exists(select 1 from qc_template_product p where p.template_id=t.template_id and p.item_id=?) for update",s.templateId,s.ipqcType,product);
        List<Map<String,Object>> indices=db.queryForList("select index_id,index_code,index_name,index_type,qc_tool,check_method,stander_val,unit_of_measure,threshold_max,threshold_min from qc_template_index where template_id=? order by record_id for update",s.templateId);
        Map<String,Object> snapshot=new LinkedHashMap<>(); snapshot.put("schemaVersion",1); snapshot.put("template",t); snapshot.put("inspectionType",s.ipqcType); snapshot.put("indices",indices);
        com.fasterxml.jackson.databind.JsonNode frozen=json.valueToTree(snapshot);
        try {
            ExecutionInspectionRecordService.validateForFreeze(frozen);
        } catch(ExecutionException invalid) {
            throw ExecutionException.validation("请检查并修改检验模板："+invalid.getMessage());
        }
        return encode(frozen);
    }
    static void validateRoute(List<Map<String,Object>> steps) {
        String[] kinds={"ASSEMBLY","INSPECTION","PACKING"}; Set<Long> process=new HashSet<>(); long last=Long.MIN_VALUE;
        if(steps.size()!=3) throw ExecutionException.validation("路线必须包含生产、质检、包装三步");
        for(int i=0;i<3;i++) { Map<String,Object> s=steps.get(i); long order=number(s.get("order_num"));
            if(!kinds[i].equals(s.get("execution_kind")) || order<=last || !process.add(number(s.get("process_id")))) throw ExecutionException.validation("工艺类型、顺序或工序重复无效"); last=order;
        }
    }
    private List<Map<String,Object>> routeSteps(Long id,boolean lock) { return db.queryForList("select * from pro_route_process where route_id=? order by order_num,record_id"+(lock?" for update":""),id); }
    private Map<String,Object> one(String sql,Object... args) { List<Map<String,Object>> rows=db.queryForList(sql,args); if(rows.size()!=1) throw ExecutionException.validation("所选业务配置不存在或未启用"); return rows.get(0); }
    private String encode(Object value) { try{return json.writeValueAsString(value);}catch(Exception bad){throw new IllegalStateException(bad);} }
    private static long number(Object value) {return value==null?-1:((Number)value).longValue();}
    private long insert(String sql,Object... args) {GeneratedKeyHolder keys=new GeneratedKeyHolder(); db.update(c->{java.sql.PreparedStatement p=c.prepareStatement(sql,Statement.RETURN_GENERATED_KEYS);for(int i=0;i<args.length;i++)p.setObject(i+1,args[i]);return p;},keys);return keys.getKey().longValue();}
}
