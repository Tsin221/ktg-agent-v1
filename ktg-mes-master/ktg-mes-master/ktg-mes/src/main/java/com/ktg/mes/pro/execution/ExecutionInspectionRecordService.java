package com.ktg.mes.pro.execution;
import com.fasterxml.jackson.databind.*;
import com.ktg.common.utils.SecurityUtils;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import javax.sql.DataSource;
import java.math.BigDecimal;
import java.util.*;
@Service public class ExecutionInspectionRecordService  {
    private final JdbcTemplate db;
    private final ObjectMapper json;
    public ExecutionInspectionRecordService(DataSource ds,ObjectMapper json) {
        db=new JdbcTemplate(ds);
        this.json=json;
    }
    public JsonNode snapshot(Object raw) {
        try {
            JsonNode n=json.readTree(String.valueOf(raw));
            if(n.path("schemaVersion").asInt()!=1||!n.path("indices").isArray()||n.path("indices").size()==0)throw new IllegalArgumentException();
            return n;
        }
        catch(Exception e) {
            throw ExecutionException.stateConflict("冻结检验要求无效");
        }
    }
    public boolean validate(JsonNode snapshot,ExecutionInspectionRequest r) { return validate(snapshot,r,true); }
    public boolean validate(JsonNode snapshot,ExecutionInspectionRequest r,boolean checkDictionary) {
        if(r==null||r.taskId==null||r.taskId<=0||r.indices==null||r.indices.size()!=snapshot.path("indices").size())throw ExecutionException.validation("须完整填写所有冻结指标");
        Map<Long,ExecutionInspectionRequest.IndexResult> results=new HashMap<>();
        for(ExecutionInspectionRequest.IndexResult i:r.indices) {
            if(i==null||i.indexId==null||results.put(i.indexId,i)!=null)throw ExecutionException.validation("指标重复或无效");
        }
        boolean accept=true;
        for(JsonNode index:snapshot.path("indices")) {
            ExecutionInspectionRequest.IndexResult i=results.get(index.path("index_id").asLong());
            if(i==null)throw ExecutionException.validation("缺少冻结指标");
            boolean pass=passes(index,i);
            if(!pass) {
                accept=false;
                if(i.defectId==null||i.defectId<=0||i.reason==null||i.reason.trim().isEmpty()||i.reason.length()>500)throw ExecutionException.validation("失败指标须填写真实缺陷及原因");
                if(checkDictionary && db.queryForList("select defect_id from qc_defect where defect_id=? and index_type=?",i.defectId,index.path("index_type").asText()).size()!=1)throw ExecutionException.validation("缺陷与指标类型不匹配");
            }
            else if(i.defectId!=null||(i.reason!=null&&!i.reason.trim().isEmpty()))throw ExecutionException.validation("合格指标不能填写不合格缺陷");
        }
        return accept;
    }
    static boolean passes(JsonNode index,ExecutionInspectionRequest.IndexResult i) {
        String type=index.path("index_type").asText();
        if("QUANTITATIVE".equals(type)) {
            if(i.value==null||i.passed!=null||i.value.precision()>30||Math.abs(i.value.scale())>12)throw ExecutionException.validation("定量指标须填写有效测量值");
            if(!index.hasNonNull("threshold_min")||!index.hasNonNull("threshold_max"))throw ExecutionException.stateConflict("冻结定量指标缺少阈值");
            BigDecimal min=index.get("threshold_min").decimalValue(),max=index.get("threshold_max").decimalValue();
            if(min.compareTo(max)>0)throw ExecutionException.stateConflict("冻结指标阈值无效");
            return i.value.compareTo(min)>=0&&i.value.compareTo(max)<=0;
        }
        if(!"QUALITATIVE".equals(type)||i.passed==null||i.value!=null)throw ExecutionException.validation("定性指标须明确判定");
        return i.passed;
    }
    public long record(Map<String,Object> task,JsonNode snapshot,ExecutionInspectionRequest request,boolean accepted) {
        Map<String,Object> header=ExecutionFactWriter.copy(task,"workorder_id,workorder_code,workorder_name,task_id,task_code,task_name,workstation_id,workstation_code,workstation_name,process_id,process_code,process_name,item_id,item_code,item_name,specification,unit_of_measure");
        header.putAll(ExecutionFactWriter.values("ipqc_code","EX-QC-"+request.operationId,"ipqc_name","单件检验","ipqc_type",task.get("inspection_type"),"template_id",task.get("inspection_template_id"),"quantity_check",1,"quantity_qualified",accepted?1:0,"quantity_unqualified",accepted?0:1,"check_result",accepted?"ACCEPT":"REJECT","status","FINISHED","inspect_date",new Date(),"inspector",SecurityUtils.getUsername(),"create_by",SecurityUtils.getUsername(),"create_time",new Date()));
        long ipqc=ExecutionFactWriter.insert(db,"qc_ipqc","ipqc_id",header);
        for(JsonNode index:snapshot.path("indices")) {
            ExecutionInspectionRequest.IndexResult result=request.indices.stream().filter(i->i.indexId==index.path("index_id").asLong()).findFirst().get();
            Map<String,Object> fields=json.convertValue(index,Map.class);
            fields.putAll(ExecutionFactWriter.values("ipqc_id",ipqc,"remark",json.valueToTree(ExecutionFactWriter.values("value",result.value,"passed",result.passed,"defectId",result.defectId)).toString(),"cr_quantity",0,"maj_quantity",0,"min_quantity",0));
            long line=ExecutionFactWriter.insert(db,"qc_ipqc_line","line_id",fields);
            if(!passes(index,result)) {
                Map<String,Object> defect=db.queryForMap("select defect_name,defect_level from qc_defect where defect_id=?",result.defectId);
                String level=String.valueOf(defect.get("defect_level"));
                if(!Arrays.asList("CR","MAJ","MIN").contains(level))throw ExecutionException.validation("缺陷等级无效");
                String column=level.toLowerCase(java.util.Locale.ROOT)+"_quantity";
                db.update("update qc_ipqc_line set "+column+"=1 where line_id=?",line);
                db.update("update qc_ipqc set "+column+"=1,"+level.toLowerCase(java.util.Locale.ROOT)+"_rate=100 where ipqc_id=?",ipqc);
                defect.putAll(ExecutionFactWriter.values("qc_type","IPQC","qc_id",ipqc,"line_id",line,"defect_quantity",1,"remark",result.reason,"create_by",SecurityUtils.getUsername(),"create_time",new Date()));
                ExecutionFactWriter.insert(db,"qc_defect_record","record_id",defect);
            }
        }
        return ipqc;
    }
}
