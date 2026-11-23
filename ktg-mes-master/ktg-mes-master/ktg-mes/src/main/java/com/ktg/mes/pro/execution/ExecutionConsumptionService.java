package com.ktg.mes.pro.execution;

import com.ktg.common.constant.UserConstants;
import com.ktg.common.utils.SecurityUtils;
import com.ktg.mes.wm.domain.WmMaterialStock;
import com.ktg.mes.wm.domain.WmTransaction;
import com.ktg.mes.wm.service.IWmTransactionService;
import com.ktg.mes.wm.service.InsufficientStockException;
import org.springframework.beans.BeanUtils;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.support.GeneratedKeyHolder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionSynchronizationManager;

import javax.sql.DataSource;
import java.math.BigDecimal;
import java.sql.Statement;
import java.util.*;

/** Writes actual consumption facts in the report transaction, never invokes the old BOM auto-consumption path. */
@Service
public class ExecutionConsumptionService {
    // The original consumption line is DOUBLE(12,2), narrower than the stock DECIMAL(18,2).
    private static final BigDecimal MAX_CONSUME_LINE = new BigDecimal("9999999999.99");
    private final JdbcTemplate db;
    private final ExecutionMaterialQueryService materials;
    private final IWmTransactionService transactions;

    public ExecutionConsumptionService(DataSource source, ExecutionMaterialQueryService materials,
                                       IWmTransactionService transactions) {
        this.db = new JdbcTemplate(source);
        this.materials = materials;
        this.transactions = transactions;
    }

    /** Report caller holds workorder/task/SN locks; this acquires stock locks in ascending primary-key order. */
    public Long consume(Long workorderId, Long taskId, Long feedbackId, ExecutionReportRequest request) {
        if (!TransactionSynchronizationManager.isActualTransactionActive())
            throw new IllegalStateException("Consumption must join the report transaction");
        ExecutionMaterialRequirements requirements = materials.requirements(workorderId);
        Map<Long, SortedMap<Long, BigDecimal>> groups = groupByStock(request);
        Map<Long, WmMaterialStock> stocks = new LinkedHashMap<>();
        Map<Long, BigDecimal> totals = new LinkedHashMap<>();
        Map<Long, Map<Long, BigDecimal>> byUnit = new HashMap<>();
        request.snIds.forEach(sn -> byUnit.put(sn, new HashMap<>()));

        for (Map.Entry<Long, SortedMap<Long, BigDecimal>> group : groups.entrySet()) {
            Long stockId = group.getKey();
            WmMaterialStock stock = materials.lockConsumable(workorderId, stockId, requirements);
            BigDecimal total = group.getValue().values().stream().reduce(BigDecimal.ZERO, BigDecimal::add);
            if (total.compareTo(MAX_CONSUME_LINE) > 0) throw ExecutionException.validation("合并用量超出耗料单行精度");
            if (stock.getQuantityOnhand().compareTo(total) < 0) throw insufficient();
            stocks.put(stockId, stock);
            totals.put(stockId, total);
            group.getValue().forEach((sn, quantity) -> byUnit.get(sn).merge(stock.getItemId(), quantity, BigDecimal::add));
        }
        byUnit.values().forEach(requirements::validateUnit);

        String username = SecurityUtils.getUsername();
        Long actor = SecurityUtils.getUserId();
        // Header derives every association from the current task/feedback, not the request payload.
        Long consumeId = insert("insert into wm_item_consume(workorder_id,workorder_code,workorder_name,task_id,task_code,task_name," +
                "workstation_id,workstation_code,workstation_name,process_id,process_code,process_name,consume_date,status,create_by,create_time) " +
                "select t.workorder_id,t.workorder_code,t.workorder_name,t.task_id,t.task_code,t.task_name,t.workstation_id,t.workstation_code," +
                "t.workstation_name,t.process_id,t.process_code,t.process_name,current_timestamp(3),'FINISHED',?,current_timestamp(3) " +
                "from pro_task t join pro_feedback f on f.task_id=t.task_id and f.workorder_id=t.workorder_id " +
                "where t.workorder_id=? and t.task_id=? and f.record_id=?", username, workorderId, taskId, feedbackId);

        for (Map.Entry<Long, WmMaterialStock> entry : stocks.entrySet()) {
            WmMaterialStock stock = entry.getValue();
            Long stockId = entry.getKey();
            BigDecimal total = totals.get(stockId);
            Long lineId = insert("insert into wm_item_consume_line(record_id,material_stock_id,item_id,item_code,item_name,specification," +
                            "unit_of_measure,quantity_consume,batch_code,warehouse_id,warehouse_code,warehouse_name,location_id,location_code," +
                            "location_name,area_id,area_code,area_name,create_by,create_time) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,current_timestamp(3))",
                    consumeId, stockId, stock.getItemId(), stock.getItemCode(), stock.getItemName(), stock.getSpecification(), stock.getUnitOfMeasure(),
                    total, stock.getBatchCode(), stock.getWarehouseId(), stock.getWarehouseCode(), stock.getWarehouseName(), stock.getLocationId(),
                    stock.getLocationCode(), stock.getLocationName(), stock.getAreaId(), stock.getAreaCode(), stock.getAreaName(), username);
            BigDecimal persisted = db.queryForObject("select quantity_consume from wm_item_consume_line where line_id=?", BigDecimal.class, lineId);
            if (persisted == null || persisted.compareTo(total) != 0)
                throw ExecutionException.validation("耗料单行不能精确记录该用量");

            WmTransaction ledger = new WmTransaction();
            BeanUtils.copyProperties(stock, ledger);
            ledger.setSourceDocType("ITEM_CONSUME");
            ledger.setSourceDocId(consumeId);
            ledger.setSourceDocCode("EX-CONSUME-" + consumeId);
            ledger.setSourceDocLineId(lineId);
            ledger.setTransactionType(UserConstants.TRANSACTION_TYPE_ITEM_CONSUME);
            ledger.setTransactionFlag(-1);
            ledger.setStorageCheckFlag(true);
            ledger.setTransactionQuantity(total);
            ledger.setCreateBy(username);
            ledger.setCreateTime(new Date());
            ledger.setTransactionDate(new Date());
            try {
                transactions.processTransaction(ledger);
            } catch (InsufficientStockException shortage) {
                throw insufficient();
            }

            groups.get(stockId).forEach((sn, quantity) -> {
                if (db.update("insert into pro_execution_input(sn_id,task_id,feedback_id,item_id,material_stock_id,source_batch_code," +
                                "consume_line_id,quantity,unit_of_measure,used_by,used_at) values(?,?,?,?,?,?,?,?,?,?,current_timestamp(3))",
                        sn, taskId, feedbackId, stock.getItemId(), stockId, stock.getBatchCode(), lineId, quantity, stock.getUnitOfMeasure(), actor) != 1)
                    throw new IllegalStateException("Actual material input was not persisted");
            });
        }
        return consumeId;
    }

    private Map<Long, SortedMap<Long, BigDecimal>> groupByStock(ExecutionReportRequest request) {
        if (request == null || request.snIds == null || request.snIds.isEmpty() || request.inputs == null || request.inputs.isEmpty())
            throw ExecutionException.validation("须选择单件并记录实际用料");
        Set<Long> allowed = new HashSet<>(request.snIds);
        if (allowed.size() != request.snIds.size() || allowed.contains(null)) throw ExecutionException.validation("单件列表无效");
        Map<Long, SortedMap<Long, BigDecimal>> groups = new TreeMap<>();
        for (ExecutionInputGroup group : request.inputs) {
            if (group == null || group.materialStockId == null || group.materialStockId <= 0 || group.quantityPerUnit == null
                    || group.quantityPerUnit.signum() <= 0 || group.quantityPerUnit.stripTrailingZeros().scale() > 2
                    || group.quantityPerUnit.precision() - group.quantityPerUnit.scale() > 16
                    || group.snIds == null || group.snIds.isEmpty()) throw ExecutionException.validation("实际用料分组无效");
            SortedMap<Long, BigDecimal> units = groups.computeIfAbsent(group.materialStockId, id -> new TreeMap<>());
            for (Long sn : group.snIds) {
                if (sn == null || !allowed.contains(sn) || units.putIfAbsent(sn, group.quantityPerUnit.setScale(2)) != null)
                    throw ExecutionException.validation("用料分组包含未选中或重复的单件");
            }
        }
        return groups;
    }

    private static ExecutionException insufficient() {
        return new ExecutionException(409, "INSUFFICIENT_STOCK", "所选线边库存余额不足");
    }

    private Long insert(String sql, Object... args) {
        GeneratedKeyHolder key = new GeneratedKeyHolder();
        int changed = db.update(c -> {
            java.sql.PreparedStatement ps = c.prepareStatement(sql, Statement.RETURN_GENERATED_KEYS);
            for (int i = 0; i < args.length; i++) ps.setObject(i + 1, args[i]);
            return ps;
        }, key);
        if (changed != 1 || key.getKey() == null) throw new IllegalStateException("Consumption record was not persisted");
        return key.getKey().longValue();
    }
}
