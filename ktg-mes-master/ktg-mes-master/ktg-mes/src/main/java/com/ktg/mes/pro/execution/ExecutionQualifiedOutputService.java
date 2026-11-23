package com.ktg.mes.pro.execution;
import com.ktg.common.utils.SecurityUtils;
import com.ktg.mes.wm.domain.tx.ProductProductTxBean;
import com.ktg.mes.wm.service.IStorageCoreService;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import javax.sql.DataSource;
import java.math.BigDecimal;
import java.util.*;
@Service public class ExecutionQualifiedOutputService  {
    private final JdbcTemplate db;
    private final IStorageCoreService storage;
    private final com.ktg.system.strategy.AutoCodeUtil codes;
    public ExecutionQualifiedOutputService(DataSource ds,IStorageCoreService storage,com.ktg.system.strategy.AutoCodeUtil codes) {
        db=new JdbcTemplate(ds);
        this.storage=storage;
        this.codes=codes;
    }
    public long produce(Map<String,Object> task,long snId) {
        if(!TransactionSynchronizationManager.isActualTransactionActive())throw new IllegalStateException("Output must join inspection transaction");
        if(db.queryForObject("select count(*) from pro_execution_inspection where sn_id=? and produce_line_id is not null",Integer.class,snId)!=0)throw ExecutionException.stateConflict("单件已有合格产出");
        Map<String,Object> order=db.queryForMap("select * from pro_workorder where workorder_id=?",task.get("workorder_id"));
        String batch=(String)order.get("batch_code");
        if(batch==null||batch.trim().isEmpty()) {
            // The operation already holds this workorder lock. Allocator writes and batch identity
            // join the same inspection transaction; a rejected output must not consume a batch.
            batch=codes.genSerialCode("BATCH_CODE",String.valueOf(order.get("product_code")));
            if(batch==null||batch.trim().isEmpty()||batch.length()>64)
                throw ExecutionException.stateConflict("成品批次编码规则无效");
            if(db.update("update pro_workorder set batch_code=? where workorder_id=? and (batch_code is null or trim(batch_code)='')",batch,order.get("workorder_id"))!=1)
                throw ExecutionException.stateConflict("工单成品批次已变化");
        }
        Map<String,Object> head=ExecutionFactWriter.copy(task,"workorder_id,workorder_code,workorder_name,task_id,task_code,task_name,workstation_id,workstation_code,workstation_name,process_id,process_code,process_name");
        head.putAll(ExecutionFactWriter.values("produce_date",new Date(),"status","FINISHED","create_by",SecurityUtils.getUsername(),"create_time",new Date()));
        long doc=ExecutionFactWriter.insert(db,"wm_product_produce","record_id",head);
        Map<String,Object> line=ExecutionFactWriter.copy(task,"item_id,item_code,item_name,specification,unit_of_measure");
        line.putAll(ExecutionFactWriter.values("record_id",doc,"quantity_produce",1,"batch_code",batch,"create_by",SecurityUtils.getUsername(),"create_time",new Date()));
        long id=ExecutionFactWriter.insert(db,"wm_product_produce_line","line_id",line);
        ProductProductTxBean tx=new ProductProductTxBean();
        tx.setItemId(((Number)task.get("item_id")).longValue());
        tx.setItemCode((String)task.get("item_code"));
        tx.setItemName((String)task.get("item_name"));
        tx.setSpecification((String)task.get("specification"));
        tx.setUnitOfMeasure((String)task.get("unit_of_measure"));
        tx.setBatchCode(batch);
        tx.setWorkorderId(((Number)task.get("workorder_id")).longValue());
        tx.setWorkorderCode((String)task.get("workorder_code"));
        tx.setWorkorderName((String)task.get("workorder_name"));
        tx.setSourceDocType("PRODUCT_PRODUCE");
        tx.setSourceDocId(doc);
        tx.setSourceDocCode("EX-PRODUCE-"+doc);
        tx.setSourceDocLineId(id);
        tx.setTransactionQuantity(BigDecimal.ONE);
        tx.setProduceDate(new Date());
        tx.setCreateBy(SecurityUtils.getUsername());
        storage.processProductProduce(Collections.singletonList(tx));
        return id;
    }
}
