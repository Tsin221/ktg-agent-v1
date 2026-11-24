package com.ktg.mes.pro.execution;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import javax.sql.DataSource;
import java.util.*;
import java.util.stream.Collectors;

/** Whole-order aggregates use execution facts, independently of task list visibility. */
@Service
public class ExecutionOrderProgressService {
    private final JdbcTemplate db;
    private final ExecutionAccessService access;
    public ExecutionOrderProgressService(DataSource ds, ExecutionAccessService access) {db=new JdbcTemplate(ds);this.access=access;}
    @org.springframework.transaction.annotation.Transactional(readOnly=true, isolation=org.springframework.transaction.annotation.Isolation.REPEATABLE_READ)
    public Map<String,Object> progress(Long id) {
        access.requireOrderQuery(id);
        Map<String,Object> result=read(id);
        List<Map<String,Object>> tasks=db.queryForList("select e.task_id as taskId,e.step_kind as stepKind,e.task_kind as taskKind,t.status,e.actual_started_at as actualStartedAt,e.actual_ended_at as actualEndedAt from pro_execution_task e join pro_task t on t.task_id=e.task_id where e.workorder_id=? order by e.normal_step_no,e.task_id",id);
        tasks.removeIf(t->{try{access.requireTaskQuery(id,((Number)t.get("taskId")).longValue());return false;}catch(ExecutionException denied){return true;}});
        result.put("tasks",tasks);
        return result;
    }
    Map<String,Object> read(Long id) {
        List<Map<String,Object>> orders=db.queryForList("select e.planned_units,w.status from pro_execution_order e join pro_workorder w on w.workorder_id=e.workorder_id where e.workorder_id=?",id);
        if(orders.size()!=1)throw ExecutionException.notFound();
        List<ExecutionState> states=db.queryForList("select unit_state from pro_execution_unit where workorder_id=? order by sn_id",String.class,id).stream().map(ExecutionState::valueOf).collect(Collectors.toList());
        ExecutionStatistics s=ExecutionStatistics.fromStates(states);
        Map<String,Object> result=new LinkedHashMap<>();
        long planned=((Number)orders.get(0).get("planned_units")).longValue();
        result.put("workorderId",id);result.put("status",orders.get(0).get("status"));
        result.put("planned",planned);result.put("allocated",s.getAllocated());result.put("assembled",s.getAssembled());
        result.put("waitingAssembly",s.getWaitingAssembly());result.put("waitingInspection",s.getWaitingInspection());result.put("pendingDisposition",s.getPendingDisposition());result.put("reworkInProgress",s.getReworkInProgress());result.put("waitingReinspection",s.getWaitingReinspection());result.put("qualified",s.getQualified());result.put("packable",s.getPackable());result.put("packed",s.getPacked());result.put("scrapped",s.getScrapped());
        result.put("firstInspectionRejected",db.queryForObject("select count(*) from pro_execution_inspection i join qc_ipqc q on q.ipqc_id=i.ipqc_id join pro_execution_unit u on u.sn_id=i.sn_id where u.workorder_id=? and i.round_no=1 and q.check_result='REJECT'",Long.class,id));
        result.put("reinspectionCount",db.queryForObject("select count(*) from pro_execution_inspection i join pro_execution_unit u on u.sn_id=i.sn_id where u.workorder_id=? and i.round_no>1",Long.class,id));
        List<String> reasons=new ArrayList<>();
        if(!"CONFIRMED".equals(orders.get(0).get("status")))reasons.add("工单未处于已下达状态");
        if(s.getAllocated()!=planned)reasons.add("分配数量与计划不一致");
        if(!s.allTerminal())reasons.add("仍有未包装或未明确报废的单件");
        long invalid=db.queryForObject("select count(*) from pro_execution_unit u where u.workorder_id=? and ((u.unit_state='PACKED' and not exists(select 1 from pro_execution_pack p where p.sn_id=u.sn_id and exists(select 1 from pro_execution_task t where t.task_id=p.task_id and t.workorder_id=u.workorder_id and t.step_kind='PACKING'))) or (u.unit_state='SCRAPPED' and not exists(select 1 from pro_execution_disposition d where d.sn_id=u.sn_id and d.decision='SCRAP')))",Long.class,id);
        result.put("terminalFactsValid",invalid==0);
        if(invalid>0)reasons.add("终态缺少合法包装或报废事实");
        long taskCount=db.queryForObject("select count(*) from pro_execution_task where workorder_id=? and task_kind='NORMAL'",Long.class,id);
        long unfinished=db.queryForObject("select count(*) from pro_execution_task e join pro_task t on t.task_id=e.task_id where e.workorder_id=? and (t.workorder_id<>e.workorder_id or t.status<>'FINISHED' or e.actual_ended_at is null or (e.actual_started_at is null and not(e.step_kind='PACKING' and e.task_kind='NORMAL' and ?=?)) or e.actual_ended_at<e.actual_started_at)",Long.class,id,s.getScrapped(),planned);
        if(taskCount!=3||unfinished>0)reasons.add("仍有未完成或计时无效的任务");
        result.put("completionAllowed",reasons.isEmpty());result.put("completionReasons",reasons);return result;
    }
}
