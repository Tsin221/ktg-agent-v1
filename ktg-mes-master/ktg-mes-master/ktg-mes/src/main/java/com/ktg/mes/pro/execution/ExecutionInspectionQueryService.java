package com.ktg.mes.pro.execution;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import javax.sql.DataSource;
import java.util.*;
@Service public class ExecutionInspectionQueryService  {
    private final JdbcTemplate db;
    private final ExecutionAccessService access;
    private final ExecutionInspectionRecordService records;
    public ExecutionInspectionQueryService(DataSource ds,ExecutionAccessService a,ExecutionInspectionRecordService r) {
        db=new JdbcTemplate(ds);
        access=a;
        records=r;
    }
    public Map<String,Object> requirements(Long task) {
        List<Map<String,Object>> rows=db.queryForList("select * from pro_execution_task where task_id=? and task_kind='NORMAL' and step_kind='INSPECTION'",task);
        if(rows.size()!=1)throw ExecutionException.notFound();
        Map<String,Object> t=rows.get(0);
        access.requireTaskQuery(((Number)t.get("workorder_id")).longValue(),task);
        return ExecutionFactWriter.values("template",records.snapshot(t.get("inspection_snapshot_json")),"defects",camel(db.queryForList("select defect_id,defect_name,defect_level,index_type from qc_defect order by defect_id")));
    }
    public List<Map<String,Object>> history(Long sn) {
        List<Long> orders=db.queryForList("select workorder_id from pro_execution_unit where sn_id=?",Long.class,sn);
        if(orders.size()!=1)throw ExecutionException.notFound();
        try {
            access.requireUnitQuery(orders.get(0),sn);
        } catch (ExecutionException currentTaskDenied) {
            boolean historicalTaskAllowed = false;
            for (Long task : db.queryForList("select distinct task_id from pro_execution_inspection where sn_id=?",Long.class,sn)) {
                try {
                    access.requireTaskQuery(orders.get(0),task);
                    historicalTaskAllowed = true;
                    break;
                } catch (ExecutionException historicalTaskDenied) {
                    // A previous task does not grant access unless it is still in the caller's scope.
                }
            }
            if (!historicalTaskAllowed) throw ExecutionException.notFound();
        }
        List<Map<String,Object>> history=camel(db.queryForList("select i.*,q.check_result,q.inspect_date,q.quantity_check,q.quantity_qualified,q.quantity_unqualified from pro_execution_inspection i join qc_ipqc q on q.ipqc_id=i.ipqc_id where i.sn_id=? order by i.round_no",sn));
        for(Map<String,Object> i:history) {
            i.put("lines",camel(db.queryForList("select * from qc_ipqc_line where ipqc_id=? order by line_id",i.get("ipqcId"))));
            i.put("defects",camel(db.queryForList("select * from qc_defect_record where qc_type='IPQC' and qc_id=? order by record_id",i.get("ipqcId"))));
        }
        return history;
    }
    static List<Map<String,Object>> camel(List<Map<String,Object>> rows) {
        List<Map<String,Object>> out=new ArrayList<>();
        for(Map<String,Object> row:rows) {
            Map<String,Object> m=new LinkedHashMap<>();
            row.forEach((k,v)-> {
                StringBuilder s=new StringBuilder();boolean up=false;for(char c:k.toCharArray()) {
                    if(c=='_')up=true;else  {
                        s.append(up?Character.toUpperCase(c):c);up=false;
                    }
                }
                m.put(s.toString(),v);
            }
            );
            out.add(m);
        }
        return out;
    }
}
