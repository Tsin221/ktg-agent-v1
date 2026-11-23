package com.ktg.mes.pro.execution;

import com.ktg.common.core.page.TableDataInfo;
import com.ktg.common.utils.SecurityUtils;
import com.ktg.framework.web.service.PermissionService;
import org.springframework.jdbc.core.ColumnMapRowMapper;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import javax.sql.DataSource;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.LocalDateTime;
import java.util.*;

/** Scope-filtered read model; execution state remains in the authoritative task/unit tables. */
@Service
public class ExecutionTaskQueryService {
    private static final String PREFIX = "mes:pro:execution:";
    private static final String FROM_TASK = " from pro_execution_task e join pro_task t on t.task_id=e.task_id "
            + "and t.workorder_id=e.workorder_id join pro_execution_order o on o.workorder_id=e.workorder_id "
            + "join pro_workorder w on w.workorder_id=e.workorder_id ";
    private static final String TASK_COLUMNS = "select e.task_id,e.workorder_id,e.normal_step_no,e.process_id,e.process_code,"
            + "e.process_name,e.step_kind,e.task_kind,e.assigned_user_id,e.started_by,e.actual_started_at,e.ended_by,"
            + "e.actual_ended_at,t.task_code,t.task_name,t.status,t.workstation_id,t.workstation_code,t.workstation_name,"
            + "t.item_id,t.item_code,t.item_name,t.specification,t.unit_of_measure,t.quantity,t.quantity_produced,"
            + "t.start_time,t.end_time,t.workorder_code,t.workorder_name,w.status as order_status";
    private final JdbcTemplate db;
    private final ExecutionAccessService access;
    private final PermissionService permissions;

    public ExecutionTaskQueryService(DataSource source, ExecutionAccessService access, PermissionService permissions) {
        this.db = new JdbcTemplate(source);
        this.access = access;
        this.permissions = permissions;
    }

    @Transactional(readOnly = true)
    public TableDataInfo list(int pageNum, int pageSize, Long workorderId, String taskKind, String status) {
        checkPage(pageNum,pageSize);
        requireQueryPermission();
        Long actor=SecurityUtils.getUserId();
        if(actor==null) throw ExecutionException.forbidden();
        StringBuilder where=new StringBuilder(" where 1=1");
        List<Object> params=new ArrayList<>();
        if (!permissions.hasPermi(PREFIX+"manageAll")) {
            where.append(" and (o.release_user_id=? or e.assigned_user_id=?)");
            params.add(actor); params.add(actor);
        }
        if(workorderId!=null) { positive(workorderId); where.append(" and e.workorder_id=?"); params.add(workorderId); }
        if(taskKind!=null && !taskKind.isEmpty()) {
            if(!Arrays.asList("NORMAL","REWORK").contains(taskKind)) throw ExecutionException.validation("任务类型无效");
            where.append(" and e.task_kind=?"); params.add(taskKind);
        }
        if(status!=null && !status.isEmpty()) {
            try { TaskExecutionState.valueOf(status); } catch(IllegalArgumentException invalid) { throw ExecutionException.validation("任务状态无效"); }
            where.append(" and t.status=?"); params.add(status);
        }
        long total=db.queryForObject("select count(*)"+FROM_TASK+where, Long.class, params.toArray());
        params.add(pageSize); params.add(offset(pageNum,pageSize));
        List<Map<String,Object>> out=new ArrayList<>();
        for(Map<String,Object> row:taskRows(TASK_COLUMNS+FROM_TASK+where+" order by e.workorder_id desc,e.normal_step_no,e.task_id limit ? offset ?",params.toArray())) {
            out.add(decorate(row));
        }
        return page(out,total);
    }

    @Transactional(readOnly = true)
    public Map<String,Object> detail(Long taskId) {
        Map<String,Object> task=authorizedTask(taskId);
        Map<String,Object> result=decorate(task);
        result.put("processContents", camelRows(db.queryForList("select content_id,order_num,content_text,device,material,doc_url "
                + "from pro_process_content where process_id=? order by order_num,content_id",task.get("process_id"))));
        result.put("productSops",camelRows(db.queryForList("select sop_id,order_num,sop_title,sop_description,sop_url "
                + "from md_product_sop where item_id=? and (process_id=? or process_id is null) order by order_num,sop_id",task.get("item_id"),task.get("process_id"))));
        return result;
    }

    @Transactional(readOnly = true)
    public TableDataInfo units(Long taskId, int pageNum, int pageSize, String state,String snCode) {
        checkPage(pageNum,pageSize);
        Map<String,Object> task=authorizedTask(taskId);
        List<String> states=eligibleStates((String)task.get("task_kind"),(String)task.get("step_kind"));
        if(states.isEmpty()) return page(Collections.emptyList(),0);
        String from=" from pro_execution_unit u join wm_sn s on s.sn_id=u.sn_id where u.current_task_id=? and u.workorder_id=?";
        List<Object> params=new ArrayList<>(Arrays.asList(taskId,task.get("workorder_id")));
        StringBuilder where=new StringBuilder(from).append(" and u.unit_state in (").append(placeholders(states.size())).append(")");
        params.addAll(states);
        if(state!=null && !state.isEmpty()) {
            try { ExecutionState.valueOf(state); } catch(IllegalArgumentException invalid) { throw ExecutionException.validation("单件状态无效"); }
            where.append(" and u.unit_state=?"); params.add(state);
        }
        if(snCode!=null && !snCode.isEmpty()) {
            if(snCode.length()>64) throw ExecutionException.validation("SN 搜索超过长度限制");
            where.append(" and instr(s.sn_code,?)>0"); params.add(snCode);
        }
        long total=db.queryForObject("select count(*)"+where,Long.class,params.toArray());
        params.add(pageSize); params.add(offset(pageNum,pageSize));
        List<Map<String,Object>> rows=db.queryForList("select u.sn_id,s.sn_code,u.unit_state,u.current_task_id,u.version"
                +where+" order by u.sn_id limit ? offset ?",params.toArray());
        return page(camelRows(rows),total);
    }

    Map<String,Object> authorizedTask(Long taskId) {
        requireQueryPermission();
        Map<String,Object> row=taskRow(taskId,false);
        access.requireTaskQuery(number(row.get("workorder_id")),taskId);
        return row;
    }

    private void requireQueryPermission() {
        if(!permissions.hasPermi(PREFIX+"query")) throw ExecutionException.forbidden();
    }

    Map<String,Object> taskRow(Long taskId, boolean lock) {
        positive(taskId);
        List<Map<String,Object>> rows=taskRows(TASK_COLUMNS+FROM_TASK+" where e.task_id=?"+(lock?" for update":""),taskId);
        if(rows.size()!=1) throw ExecutionException.notFound();
        return rows.get(0);
    }

    private List<Map<String,Object>> taskRows(String sql,Object... args) {
        // Connector/J maps DATETIME to LocalDateTime in generic result maps. Use
        // JDBC Timestamp for those fields so serverTime and task times share the
        // same timezone conversion and Jackson wire format.
        return db.query(sql,new ColumnMapRowMapper() {
            @Override protected Object getColumnValue(ResultSet rs,int index) throws SQLException {
                Object value=super.getColumnValue(rs,index);
                return value instanceof LocalDateTime ? rs.getTimestamp(index) : value;
            }
        },args);
    }

    Long orderId(Long taskId) { return number(taskRow(taskId,false).get("workorder_id")); }

    int availableUnits(Map<String,Object> task) {
        List<String> states=eligibleStates((String)task.get("task_kind"),(String)task.get("step_kind"));
        if(states.isEmpty()) return 0;
        List<Object> params=new ArrayList<>(Arrays.asList(task.get("task_id"),task.get("workorder_id")));
        params.addAll(states);
        return db.queryForObject("select count(*) from pro_execution_unit where current_task_id=? and workorder_id=? and unit_state in ("
                + placeholders(states.size())+")",Integer.class,params.toArray());
    }

    boolean legalWorkstation(Map<String,Object> task,boolean lock) {
        return !db.queryForList("select workstation_id from md_workstation where workstation_id=? and process_id=? and enable_flag='Y'"
                +(lock?" for update":""),task.get("workstation_id"),task.get("process_id")).isEmpty();
    }

    private Map<String,Object> decorate(Map<String,Object> task) {
        Map<String,Object> result=camel(task);
        Long taskId=number(task.get("task_id")), orderId=number(task.get("workorder_id"));
        int count=availableUnits(task);
        boolean legal=legalWorkstation(task,false);
        result.put("availableUnits",count);
        result.put("assignedUserName",null);
        if(task.get("assigned_user_id")!=null) {
            List<String> names=db.queryForList("select nick_name from sys_user where user_id=?",String.class,task.get("assigned_user_id"));
            if(!names.isEmpty()) result.put("assignedUserName",names.get(0));
        }
        result.put("canAssign",legal && ExecutionAssignmentService.assignable(task) && allowed(ExecutionAction.ASSIGN,orderId,taskId));
        result.put("canStart",legal && ExecutionTaskStartService.startable(task,count) && allowed(ExecutionAction.START,orderId,taskId));
        boolean started=legal && "CONFIRMED".equals(task.get("order_status")) && "STARTED".equals(task.get("status"))
                && task.get("actual_started_at")!=null && task.get("actual_ended_at")==null;
        boolean assembly="NORMAL".equals(task.get("task_kind")) && "ASSEMBLY".equals(task.get("step_kind"));
        result.put("canReport",started && assembly && count>0 && allowed(ExecutionAction.REPORT,orderId,taskId));
        result.put("canComplete",started && assembly && count==0 && allowed(ExecutionAction.TASK_COMPLETE,orderId,taskId));
        result.put("serverTime",db.queryForObject("select current_timestamp(3)",java.sql.Timestamp.class));
        return result;
    }

    private boolean allowed(ExecutionAction action,Long orderId,Long taskId) {
        try {access.requireOperation(action,orderId,taskId,null);return true;}
        catch(ExecutionException denied) {return false;}
    }

    static List<String> eligibleStates(String taskKind,String stepKind) {
        if("REWORK".equals(taskKind)) return Collections.singletonList("REWORK_IN_PROGRESS");
        if(!"NORMAL".equals(taskKind)) return Collections.emptyList();
        if("ASSEMBLY".equals(stepKind)) return Collections.singletonList("WAIT_ASSEMBLY");
        if("INSPECTION".equals(stepKind)) return Arrays.asList("WAIT_INSPECTION","WAIT_REINSPECTION");
        if("PACKING".equals(stepKind)) return Collections.singletonList("WAIT_PACKING");
        return Collections.emptyList();
    }

    static String stepPermission(Map<String,Object> task) {
        if("REWORK".equals(task.get("task_kind"))) return PREFIX+"rework";
        if("NORMAL".equals(task.get("task_kind"))) {
            if("ASSEMBLY".equals(task.get("step_kind"))) return PREFIX+"report";
            if("INSPECTION".equals(task.get("step_kind"))) return PREFIX+"inspect";
            if("PACKING".equals(task.get("step_kind"))) return PREFIX+"pack";
        }
        throw ExecutionException.validation("任务工序类型无效");
    }

    static Long number(Object value) { return ((Number)value).longValue(); }
    static void positive(Long value) { if(value==null||value<=0) throw ExecutionException.validation("目标 ID 无效"); }
    static void checkPage(int pageNum,int pageSize) {
        if(pageNum<1||pageSize<1||pageSize>200) throw ExecutionException.validation("分页参数无效");
    }
    private static long offset(int pageNum,int pageSize) {return ((long)pageNum-1)*pageSize;}
    private static String placeholders(int count) {return String.join(",",Collections.nCopies(count,"?"));}
    private static TableDataInfo page(List<?> rows,long total) {
        TableDataInfo page=new TableDataInfo();page.setRows(rows);page.setTotal(total);page.setCode(200);page.setMsg("查询成功");return page;
    }
    static Map<String,Object> camel(Map<String,Object> row) {
        Map<String,Object> out=new LinkedHashMap<>();row.forEach((key,value)->{
            StringBuilder name=new StringBuilder();boolean upper=false;
            for(char ch:key.toCharArray()) if(ch=='_') upper=true; else {name.append(upper?Character.toUpperCase(ch):ch);upper=false;}
            out.put(name.toString(),value);
        });return out;
    }
    static List<Map<String,Object>> camelRows(List<Map<String,Object>> rows) {
        List<Map<String,Object>> out=new ArrayList<>();for(Map<String,Object> row:rows)out.add(camel(row));return out;
    }
}
