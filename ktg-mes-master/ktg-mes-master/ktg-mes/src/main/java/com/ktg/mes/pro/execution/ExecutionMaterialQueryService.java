package com.ktg.mes.pro.execution;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.ktg.common.core.page.TableDataInfo;
import com.ktg.mes.pro.execution.ExecutionOwnershipService.TaskScope;
import com.ktg.mes.wm.domain.WmMaterialStock;
import org.springframework.jdbc.core.BeanPropertyRowMapper;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import javax.sql.DataSource;
import java.util.*;

/** Available stock is scoped to this workorder and proven by completed production-issue inbound ledger rows. */
@Service
public class ExecutionMaterialQueryService {
    private static final String STOCK_DIMENSIONS =
            " from wm_material_stock s join md_item i on i.item_id=s.item_id and i.item_or_product='ITEM'" +
            " join wm_warehouse w on w.warehouse_id=s.warehouse_id and w.warehouse_code='XBK_VIRTUAL'" +
            " join wm_storage_location l on l.location_id=s.location_id and l.warehouse_id=w.warehouse_id and l.location_code='XBKKQ_VIRTUAL'" +
            " join wm_storage_area a on a.area_id=s.area_id and a.location_id=l.location_id and a.area_code='XBKKW_VIRTUAL'" +
            " where s.workorder_id=? and binary s.unit_of_measure=binary i.unit_of_measure" +
            " and s.batch_code is not null and length(trim(s.batch_code))>0";
    private static final String ISSUE_JOIN =
            " join wm_issue_header h on h.issue_id=t.source_doc_id" +
            " and h.workorder_id=t.workorder_id and h.status='FINISHED'" +
            " join wm_issue_line il on il.line_id=t.source_doc_line_id and il.issue_id=h.issue_id and il.item_id=t.item_id" +
            " join wm_transaction out_tx on out_tx.transaction_id=t.related_transaction_id and out_tx.source_doc_type='ISSUE'" +
            " and out_tx.transaction_type='ITEM_ISSUE_OUT' and out_tx.transaction_flag=-1" +
            " and out_tx.source_doc_id=t.source_doc_id and out_tx.source_doc_line_id=t.source_doc_line_id" +
            " and out_tx.workorder_id=t.workorder_id and out_tx.item_id=t.item_id" +
            " and out_tx.material_stock_id=il.material_stock_id and out_tx.transaction_quantity=-t.transaction_quantity" +
            " and binary out_tx.batch_code=binary t.batch_code and binary out_tx.unit_of_measure=binary t.unit_of_measure";
    private static final String ISSUE_WHERE =
            " where t.source_doc_type='ISSUE' and t.transaction_type='ITEM_ISSUE_IN' and t.transaction_flag=1" +
            " and t.transaction_quantity>0 and t.material_stock_id=s.material_stock_id and t.workorder_id=s.workorder_id" +
            " and t.item_id=s.item_id and binary t.batch_code=binary s.batch_code" +
            " and binary t.unit_of_measure=binary s.unit_of_measure and binary il.batch_code=binary s.batch_code" +
            " and binary il.unit_of_measure=binary s.unit_of_measure" +
            " and t.warehouse_id=s.warehouse_id and t.location_id=s.location_id and t.area_id=s.area_id" +
            " and coalesce(t.vendor_id,0)=coalesce(s.vendor_id,0)";
    private final JdbcTemplate db;
    private final ExecutionOwnershipService ownership;
    private final ExecutionAccessService access;
    private final ObjectMapper json;

    public ExecutionMaterialQueryService(DataSource source, ExecutionOwnershipService ownership,
                                         ExecutionAccessService access, ObjectMapper json) {
        this.db = new JdbcTemplate(source);
        this.ownership = ownership;
        this.access = access;
        this.json = json;
    }

    @Transactional(readOnly = true)
    public TableDataInfo available(Long taskId, Long itemId, String batchCode, int pageNum, int pageSize) {
        if (taskId == null || taskId <= 0 || (itemId != null && itemId <= 0) || pageNum < 1
                || pageSize < 1 || pageSize > 200 || (batchCode != null && batchCode.length() > 255))
            throw ExecutionException.validation("分页或物料筛选条件无效");
        TaskScope task = ownership.task(taskId);
        // The shared query guard checks permission even for a missing task. Do not
        // differentiate an unauthorized caller's missing and existing task IDs.
        Long orderId = task == null ? null : Long.valueOf(task.workorderId);
        access.requireTaskQuery(orderId, taskId);
        if (task == null) throw ExecutionException.notFound();
        if (!"NORMAL".equals(task.taskKind) || !"ASSEMBLY".equals(task.stepKind))
            throw ExecutionException.validation("非装配任务不可查询耗料库存");
        ExecutionMaterialRequirements requirements = requirements(task.workorderId);
        List<Object> args = new ArrayList<>();
        args.add(task.workorderId);
        String where = STOCK_DIMENSIONS + " and s.quantity_onhand>0 and exists(select 1 from wm_transaction t" + ISSUE_JOIN + ISSUE_WHERE + ")";
        // Use frozen required item/unit pairs only; both ends revalidate this before consuming.
        StringJoiner acceptable = new StringJoiner(" or ", " and (", ")");
        for (Long requiredId : requirements.itemIds()) {
            acceptable.add("s.item_id=? and binary s.unit_of_measure=binary ?");
            args.add(requiredId);
            args.add(requirements.unit(requiredId));
        }
        where += acceptable.toString();
        if (itemId != null) { where += " and s.item_id=?"; args.add(itemId); }
        if (batchCode != null && !batchCode.isEmpty()) { where += " and binary s.batch_code=binary ?"; args.add(batchCode); }
        Long total = db.queryForObject("select count(*)" + where, Long.class, args.toArray());
        args.add(pageSize); args.add(((long) pageNum - 1) * pageSize);
        List<Map<String, Object>> rows = db.queryForList("select s.material_stock_id as materialStockId,s.item_id as itemId," +
                "s.item_code as itemCode,s.item_name as itemName,s.specification,s.batch_code as sourceBatchCode," +
                "s.quantity_onhand as quantityOnhand,s.unit_of_measure as unitOfMeasure,s.warehouse_id as warehouseId," +
                "s.location_id as locationId,s.area_id as areaId" + where + " order by s.material_stock_id limit ? offset ?", args.toArray());
        TableDataInfo response = new TableDataInfo();
        response.setCode(200); response.setMsg("查询成功"); response.setTotal(total); response.setRows(rows);
        return response;
    }

    ExecutionMaterialRequirements requirements(Long workorderId) {
        List<Map<String,Object>> rows = db.queryForList("select planned_units,material_requirements_snapshot from pro_execution_order where workorder_id=?", workorderId);
        if (rows.size() != 1) throw ExecutionException.notFound();
        Map<String,Object> row = rows.get(0);
        return ExecutionMaterialRequirements.parse(json, ((Number)row.get("planned_units")).intValue(),
                (String) row.get("material_requirements_snapshot"));
    }

    // Lock one stock primary key at a time (caller sorts all IDs). The balance read is current.
    WmMaterialStock lockConsumable(Long workorderId, Long stockId, ExecutionMaterialRequirements requirements) {
        List<WmMaterialStock> rows = db.query("select s.* from wm_material_stock s where s.material_stock_id=? for update",
                new BeanPropertyRowMapper<>(WmMaterialStock.class), stockId);
        if (rows.size() != 1 || !workorderId.equals(rows.get(0).getWorkorderId())) throw invalidStock();
        WmMaterialStock stock = rows.get(0);
        if (!requirements.allows(stock.getItemId(), stock.getUnitOfMeasure())) throw invalidStock();
        List<Long> allowed = db.queryForList("select s.material_stock_id" + STOCK_DIMENSIONS + " and s.material_stock_id=? for update",
                Long.class, workorderId, stockId);
        if (allowed.size() != 1) throw invalidStock();
        // Completed issue documents and ledger facts are immutable; use a committed snapshot and
        // never wait for the issue-header lock while holding a destination stock lock. If the
        // issuance just committed after our snapshot, this conservatively rejects; refresh/retry.
        List<Long> proof = db.queryForList("select t.transaction_id from wm_material_stock s join wm_transaction t on t.material_stock_id=s.material_stock_id" +
                ISSUE_JOIN + ISSUE_WHERE +
                " and s.material_stock_id=? limit 1", Long.class, stockId);
        if (proof.isEmpty()) throw invalidStock();
        return stock;
    }

    private ExecutionException invalidStock() {
        return ExecutionException.validation("所选库存不属于本工单已领入的合法原料线边库存");
    }
}
