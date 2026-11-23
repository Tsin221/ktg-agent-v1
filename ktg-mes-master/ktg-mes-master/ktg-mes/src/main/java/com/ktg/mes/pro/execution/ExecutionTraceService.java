package com.ktg.mes.pro.execution;

import com.ktg.common.core.page.TableDataInfo;
import com.ktg.common.utils.SecurityUtils;
import com.ktg.framework.web.service.PermissionService;
import com.ktg.mes.pro.execution.mapper.ExecutionTraceMapper;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Isolation;
import org.springframework.transaction.annotation.Transactional;

import java.sql.Timestamp;
import java.time.LocalDateTime;
import java.util.*;

/** Read-only history, scoped exactly as B01's requireTrace. Never supplies synthetic facts. */
@Service
public class ExecutionTraceService {
    private static final String PREFIX="mes:pro:execution:";
    private final ExecutionTraceMapper mapper;
    private final ExecutionAccessService access;
    private final PermissionService permissions;

    public ExecutionTraceService(ExecutionTraceMapper mapper,ExecutionAccessService access,PermissionService permissions) {
        this.mapper=mapper;this.access=access;this.permissions=permissions;
    }

    // Use one consistent snapshot for the identity, permission checks and all related facts.
    @Transactional(readOnly=true,isolation=Isolation.REPEATABLE_READ)
    public Map<String,Object> unit(String snCode) {
        snCode=ExecutionTraceQuery.sn(snCode);
        requirePermission();
        List<Map<String,Object>> identity=camelRows(mapper.identity(snCode));
        if(identity.size()!=1) throw ExecutionException.notFound();
        Map<String,Object> result=identity.get(0);
        Long snId=id(result.get("snId")),workorderId=id(result.get("workorderId"));
        if(workorderId==null) {
            // Legacy wm_sn has no durable workorder link. Its existing read permission is
            // required to distinguish a known identity from an unknown or inaccessible one.
            if(!permissions.hasPermi("mes:wm:sn:query")) throw ExecutionException.notFound();
            result.put("traceStatus","LEGACY_NO_RECORD");
            result.put("tasks",Collections.emptyList());result.put("inputs",Collections.emptyList());
            result.put("inspections",Collections.emptyList());result.put("dispositions",Collections.emptyList());
            result.put("assemblyFeedback",null);result.put("packing",null);
            return result;
        }
        access.requireTrace(workorderId,snId);
        result.put("traceStatus","TRACKED");
        result.put("tasks",camelRows(mapper.tasks(snId,workorderId)));
        List<Map<String,Object>> inputs=camelRows(mapper.inputs(snId,workorderId));
        for(Map<String,Object> input:inputs) {
            input.put("transactions",camelRows(mapper.transactions(id(input.get("consumeRecordId")),
                    id(input.get("consumeLineId")),workorderId,"ITEM_CONSUME")));
        }
        result.put("inputs",inputs);
        List<Map<String,Object>> inspections=camelRows(mapper.inspections(snId,workorderId));
        for(Map<String,Object> inspection:inspections) {
            Long inspectionId=id(inspection.get("ipqcId"));
            inspection.put("lines",camelRows(mapper.inspectionLines(inspectionId)));
            inspection.put("defects",camelRows(mapper.inspectionDefects(inspectionId)));
            Long produceId=id(inspection.get("produceRecordId")),produceLine=id(inspection.get("produceLineId"));
            inspection.put("produceTransactions",produceId==null||produceLine==null?Collections.emptyList():
                    camelRows(mapper.transactions(produceId,produceLine,workorderId,"PRODUCT_PRODUCE")));
        }
        result.put("inspections",inspections);
        result.put("dispositions",camelRows(mapper.dispositions(snId,workorderId)));
        result.put("assemblyFeedback",camel(mapper.assemblyFeedback(snId,workorderId)));
        result.put("packing",camel(mapper.packing(snId,workorderId)));
        return result;
    }

    @Transactional(readOnly=true,isolation=Isolation.REPEATABLE_READ)
    public TableDataInfo material(Long itemId,String batchCode,int pageNum,int pageSize) {
        ExecutionTraceQuery.material(itemId,pageNum,pageSize);
        batchCode=ExecutionTraceQuery.batch(batchCode);
        Long actor=requirePermission();
        boolean manageAll=permissions.hasPermi(PREFIX+"manageAll");
        long total=mapper.countMaterials(itemId,batchCode,actor,manageAll);
        List<Map<String,Object>> rows=camelRows(mapper.materialRows(itemId,batchCode,actor,manageAll,pageSize,((long)pageNum-1)*pageSize));
        TableDataInfo response=new TableDataInfo();
        response.setRows(rows);response.setTotal(total);response.setCode(200);response.setMsg("查询成功");
        return response;
    }

    private Long requirePermission() {
        if(!permissions.hasPermi(PREFIX+"trace") || !permissions.hasPermi(PREFIX+"query")) throw ExecutionException.forbidden();
        Long actor=SecurityUtils.getUserId();
        if(actor==null) throw ExecutionException.forbidden();
        return actor;
    }

    private static Long id(Object value) {return value==null?null:((Number)value).longValue();}

    private static Map<String,Object> camel(Map<String,Object> row) {
        if(row==null)return null;
        Map<String,Object> normalized=new LinkedHashMap<>();
        row.forEach((key,value)->normalized.put(key.toLowerCase(Locale.ROOT),
                value instanceof LocalDateTime?Timestamp.valueOf((LocalDateTime)value):value));
        return ExecutionTaskQueryService.camel(normalized);
    }

    private static List<Map<String,Object>> camelRows(List<Map<String,Object>> rows) {
        List<Map<String,Object>> result=new ArrayList<>(rows.size());
        for(Map<String,Object> row:rows)result.add(camel(row));
        return result;
    }
}
