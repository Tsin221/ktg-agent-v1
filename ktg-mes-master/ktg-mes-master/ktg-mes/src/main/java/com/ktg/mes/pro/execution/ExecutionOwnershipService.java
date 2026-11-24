package com.ktg.mes.pro.execution;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.stereotype.Service;

import javax.sql.DataSource;
import java.util.Collection;
import java.util.List;
import java.util.Set;
import java.util.TreeSet;

/** Reads only durable object relationships, never identity fields supplied by a caller. */
@Service
public class ExecutionOwnershipService {
    private final JdbcTemplate jdbc;

    public ExecutionOwnershipService(DataSource dataSource) {
        this.jdbc = new JdbcTemplate(dataSource);
    }

    public OrderScope order(Long id) {
        if (id == null) return null;
        return first("select w.create_by,e.release_user_id from pro_workorder w "
                + "left join pro_execution_order e on e.workorder_id=w.workorder_id where w.workorder_id=?",
                (rs, i) -> new OrderScope(rs.getString(1), (Long) rs.getObject(2)), id);
    }

    public TaskScope task(Long id) {
        if (id == null) return null;
        return first("select e.workorder_id,e.assigned_user_id,e.step_kind,e.task_kind "
                + "from pro_execution_task e join pro_task t on t.task_id=e.task_id and t.workorder_id=e.workorder_id "
                + "where e.task_id=?", (rs, i) -> new TaskScope(rs.getLong(1), (Long) rs.getObject(2),
                rs.getString(3), rs.getString(4)), id);
    }

    public UnitScope unit(Long id) {
        if (id == null) return null;
        return first("select workorder_id,current_task_id from pro_execution_unit where sn_id=?",
                (rs, i) -> new UnitScope(rs.getLong(1), rs.getLong(2)), id);
    }

    public boolean hasAssignedTask(Long workorderId, Long actor) {
        return !jdbc.queryForList("select e.task_id from pro_execution_task e "
                        + "join pro_task t on t.task_id=e.task_id and t.workorder_id=e.workorder_id "
                        + "where e.workorder_id=? and e.assigned_user_id=? limit 1", Long.class, workorderId, actor).isEmpty();
    }

    private <T> T first(String sql, RowMapper<T> mapper, Object key) {
        List<T> rows = jdbc.query(sql, mapper, key);
        return rows.isEmpty() ? null : rows.get(0);
    }

    public static final class OrderScope {
        public final String creator;
        public final Long releaseUserId;
        OrderScope(String creator, Long releaseUserId) {
            this.creator = creator;
            this.releaseUserId = releaseUserId;
        }
    }

    public static final class TaskScope {
        public final long workorderId;
        public final Long assignedUserId;
        public final String stepKind;
        public final String taskKind;
        TaskScope(long workorderId, Long assignedUserId, String stepKind, String taskKind) {
            this.workorderId = workorderId;
            this.assignedUserId = assignedUserId;
            this.stepKind = stepKind;
            this.taskKind = taskKind;
        }
    }

    public static final class UnitScope {
        public final long workorderId;
        public final long currentTaskId;
        UnitScope(long workorderId, long currentTaskId) {
            this.workorderId = workorderId;
            this.currentTaskId = currentTaskId;
        }
    }

    /**
     * Explicit scope queries for legacy business objects. SQL is constant and all
     * values are bound parameters; the execution fact references are considered
     * separately from old header/line relations.
     */
    enum Ref {
        ORDER(),
        ORDER_CODE("select workorder_id from pro_workorder where workorder_code=?"),
        BOM("select workorder_id from pro_workorder_bom where line_id=?"),
        TASK("select workorder_id from pro_task where task_id=?",
             "select workorder_id from pro_execution_task where task_id=?"),
        FEEDBACK("select workorder_id from pro_feedback where record_id=?",
                 "select t.workorder_id from pro_feedback f join pro_task t on t.task_id=f.task_id where f.record_id=?",
                 "select workorder_id from pro_execution_unit where assembly_feedback_id=?",
                 "select u.workorder_id from pro_execution_input i join pro_execution_unit u on u.sn_id=i.sn_id where i.feedback_id=?"),
        IPQC("select workorder_id from qc_ipqc where ipqc_id=?",
             "select t.workorder_id from qc_ipqc q join pro_task t on t.task_id=q.task_id where q.ipqc_id=?",
             "select u.workorder_id from pro_execution_inspection i join pro_execution_unit u on u.sn_id=i.sn_id where i.ipqc_id=?"),
        IPQC_LINE("select q.workorder_id from qc_ipqc_line l join qc_ipqc q on q.ipqc_id=l.ipqc_id where l.line_id=?",
                  "select t.workorder_id from qc_ipqc_line l join qc_ipqc q on q.ipqc_id=l.ipqc_id join pro_task t on t.task_id=q.task_id where l.line_id=?",
                  "select u.workorder_id from qc_ipqc_line l join pro_execution_inspection i on i.ipqc_id=l.ipqc_id join pro_execution_unit u on u.sn_id=i.sn_id where l.line_id=?"),
        DEFECT("select q.workorder_id from qc_defect_record d join qc_ipqc q on q.ipqc_id=d.qc_id where d.qc_type='IPQC' and d.record_id=?",
               "select t.workorder_id from qc_defect_record d join qc_ipqc q on q.ipqc_id=d.qc_id join pro_task t on t.task_id=q.task_id where d.qc_type='IPQC' and d.record_id=?",
               "select u.workorder_id from qc_defect_record d join pro_execution_inspection i on i.ipqc_id=d.qc_id join pro_execution_unit u on u.sn_id=i.sn_id where d.qc_type='IPQC' and d.record_id=?",
               "select u.workorder_id from qc_defect_record d join qc_ipqc_line l on l.line_id=d.line_id join pro_execution_inspection i on i.ipqc_id=l.ipqc_id join pro_execution_unit u on u.sn_id=i.sn_id where d.qc_type='IPQC' and d.record_id=?"),
        SN("select workorder_id from pro_execution_unit where sn_id=?"),
        CONSUME("select workorder_id from wm_item_consume where record_id=?",
                "select t.workorder_id from wm_item_consume h join pro_task t on t.task_id=h.task_id where h.record_id=?",
                "select u.workorder_id from wm_item_consume_line l join pro_execution_input i on i.consume_line_id=l.line_id join pro_execution_unit u on u.sn_id=i.sn_id where l.record_id=?"),
        CONSUME_LINE("select h.workorder_id from wm_item_consume_line l join wm_item_consume h on h.record_id=l.record_id where l.line_id=?",
                     "select t.workorder_id from wm_item_consume_line l join wm_item_consume h on h.record_id=l.record_id join pro_task t on t.task_id=h.task_id where l.line_id=?",
                     "select s.workorder_id from wm_item_consume_line l join wm_material_stock s on s.material_stock_id=l.material_stock_id where l.line_id=?",
                     "select u.workorder_id from wm_item_consume_line l join wm_item_consume_line sibling on sibling.record_id=l.record_id join pro_execution_input i on i.consume_line_id=sibling.line_id join pro_execution_unit u on u.sn_id=i.sn_id where l.line_id=?"),
        PRODUCE("select workorder_id from wm_product_produce where record_id=?",
                "select t.workorder_id from wm_product_produce h join pro_task t on t.task_id=h.task_id where h.record_id=?",
                "select u.workorder_id from wm_product_produce_line l join pro_execution_inspection i on i.produce_line_id=l.line_id join pro_execution_unit u on u.sn_id=i.sn_id where l.record_id=?"),
        PRODUCE_LINE("select h.workorder_id from wm_product_produce_line l join wm_product_produce h on h.record_id=l.record_id where l.line_id=?",
                     "select t.workorder_id from wm_product_produce_line l join wm_product_produce h on h.record_id=l.record_id join pro_task t on t.task_id=h.task_id where l.line_id=?",
                     "select u.workorder_id from wm_product_produce_line l join wm_product_produce_line sibling on sibling.record_id=l.record_id join pro_execution_inspection i on i.produce_line_id=sibling.line_id join pro_execution_unit u on u.sn_id=i.sn_id where l.line_id=?"),
        PACKAGE("select l.workorder_id from wm_package p join wm_package_line l on l.package_id=p.package_id where p.package_id=? or find_in_set(?,p.ancestors)>0",
                "select s.workorder_id from wm_package p join wm_package_line l on l.package_id=p.package_id join wm_material_stock s on s.material_stock_id=l.material_stock_id where p.package_id=? or find_in_set(?,p.ancestors)>0",
                "select u.workorder_id from wm_package p join wm_package_line l on l.package_id=p.package_id join pro_execution_pack x on x.package_line_id=l.line_id join pro_execution_unit u on u.sn_id=x.sn_id where p.package_id=? or find_in_set(?,p.ancestors)>0"),
        PACKAGE_LINE("select workorder_id from wm_package_line where line_id=?",
                     "select s.workorder_id from wm_package_line l join wm_material_stock s on s.material_stock_id=l.material_stock_id where l.line_id=?",
                     "select sibling.workorder_id from wm_package_line l join wm_package_line sibling on sibling.package_id=l.package_id where l.line_id=?",
                     "select s.workorder_id from wm_package_line l join wm_package_line sibling on sibling.package_id=l.package_id join wm_material_stock s on s.material_stock_id=sibling.material_stock_id where l.line_id=?",
                     "select u.workorder_id from wm_package_line l join wm_package_line sibling on sibling.package_id=l.package_id join pro_execution_pack p on p.package_line_id=sibling.line_id join pro_execution_unit u on u.sn_id=p.sn_id where l.line_id=?"),
        STOCK("select workorder_id from wm_material_stock where material_stock_id=?",
              "select w.workorder_id from wm_material_stock s join pro_workorder w on w.workorder_code=s.workorder_code where s.material_stock_id=?",
              "select u.workorder_id from pro_execution_input i join pro_execution_unit u on u.sn_id=i.sn_id where i.material_stock_id=?");

        private final String[] sql;
        Ref(String... sql) { this.sql = sql; }
    }

    static final class LegacyRef {
        final Ref type;
        final Object key;
        LegacyRef(Ref type, Object key) { this.type = type; this.key = key; }
    }

    /** Pre-execution deployments cannot contain bound execution orders. */
    public boolean hasExecutionSchema() {
        return Boolean.TRUE.equals(jdbc.execute((org.springframework.jdbc.core.ConnectionCallback<Boolean>) c -> {
            try (java.sql.ResultSet found = c.getMetaData().getTables(c.getCatalog(), null, "%", null)) {
                while (found.next()) if ("pro_execution_order".equalsIgnoreCase(found.getString("TABLE_NAME"))) return true;
            }
            return false;
        }));
    }

    /** Call in the same transaction as the guarded write. */
    void requireLegacyWritable(Collection<LegacyRef> references) {
        if (references.isEmpty()) return;
        // Once the first execution table exists, incomplete migration fails closed.
        if (!hasExecutionSchema()) return;

        Set<Long> before = resolve(references, false);
        // Same outer lock as RELEASE; re-read current relationships (not a
        // snapshot) after acquiring them and reject a concurrent re-parenting.
        for (Long id : before) {
            jdbc.queryForList("select workorder_id from pro_workorder where workorder_id=? for update", Long.class, id);
        }
        Set<Long> current = resolve(references, true);
        if (!before.containsAll(current)) {
            throw ExecutionException.stateConflict("业务归属已变化，请重新读取");
        }
        for (Long id : before) {
            if (!jdbc.queryForList("select workorder_id from pro_execution_order where workorder_id=? for update",
                    Long.class, id).isEmpty()) {
                throw new ExecutionException(409, "LEGACY_WRITE_PROTECTED", "受控生产记录必须经生产执行动作修改");
            }
        }
    }

    private Set<Long> resolve(Collection<LegacyRef> references, boolean locked) {
        Set<Long> ids = new TreeSet<>();
        for (LegacyRef ref : references) {
            if (ref.key == null) continue;
            if (ref.type == Ref.ORDER) {
                if (((Long) ref.key) > 0) ids.add((Long) ref.key);
                continue;
            }
            for (String sql : ref.type.sql) {
                Object[] params = sql.contains("find_in_set") ? new Object[]{ref.key, ref.key} : new Object[]{ref.key};
                for (Long id : jdbc.queryForList(sql + (locked ? " for update" : ""), Long.class, params)) {
                    if (id != null && id > 0) ids.add(id);
                }
            }
        }
        return ids;
    }
}
