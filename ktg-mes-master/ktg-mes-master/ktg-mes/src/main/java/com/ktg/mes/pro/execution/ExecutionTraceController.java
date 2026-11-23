package com.ktg.mes.pro.execution;

import com.ktg.common.core.domain.AjaxResult;
import com.ktg.common.core.page.TableDataInfo;
import org.springframework.web.bind.MissingServletRequestParameterException;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.method.annotation.MethodArgumentTypeMismatchException;
import java.util.*;

@RestController
@RequestMapping("/mes/pro/execution")
public class ExecutionTraceController {
    private final ExecutionTraceService service;
    public ExecutionTraceController(ExecutionTraceService service) {this.service=service;}

    @GetMapping("/units/trace")
    public AjaxResult unit(@RequestParam("snCode") String snCode) {
        return AjaxResult.success(wire(service.unit(ExecutionTraceQuery.sn(snCode))));
    }

    @GetMapping("/materials/trace")
    public TableDataInfo material(@RequestParam("itemId") Long itemId,
                                  @RequestParam("batchCode") String batchCode,
                                  @RequestParam(value="pageNum",defaultValue="1") int pageNum,
                                  @RequestParam(value="pageSize",defaultValue="20") int pageSize) {
        ExecutionTraceQuery.material(itemId,pageNum,pageSize);
        TableDataInfo result=service.material(itemId,ExecutionTraceQuery.batch(batchCode),pageNum,pageSize);
        result.setRows((List<?>)wire(result.getRows()));
        return result;
    }

    @ExceptionHandler({MissingServletRequestParameterException.class,MethodArgumentTypeMismatchException.class})
    public AjaxResult invalid() {return ExecutionException.validation("追溯查询参数不完整或格式无效").toAjaxResult();}

    // BIGINT identifiers can exceed JavaScript's 53-bit integer range. Keep
    // these exact on the wire without altering numeric quantities or other APIs.
    private static final Set<String> ACTOR_FIELDS=new HashSet<>(Arrays.asList(
            "usedBy","packedBy","decidedBy","startedBy","endedBy"));

    private static Object wire(Object value) {
        if(value instanceof List) {
            List<Object> values=new ArrayList<>();
            for(Object entry:(List<?>)value) values.add(wire(entry));
            return values;
        }
        if(value instanceof Map) {
            Map<String,Object> result=new LinkedHashMap<>();
            ((Map<?,?>)value).forEach((key,field)->{
                String name=(String)key;
                if(field instanceof Number && (name.endsWith("Id") || ACTOR_FIELDS.contains(name)))
                    result.put(name,field.toString());
                else result.put(name,wire(field));
            });
            return result;
        }
        return value;
    }
}
