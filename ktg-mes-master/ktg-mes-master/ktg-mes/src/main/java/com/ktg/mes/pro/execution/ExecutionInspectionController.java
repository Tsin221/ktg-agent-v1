package com.ktg.mes.pro.execution;
import com.ktg.common.core.domain.AjaxResult;
import org.springframework.web.bind.annotation.*;
import org.springframework.http.converter.HttpMessageNotReadableException;
@RestController @RequestMapping("/mes/pro/execution") public class ExecutionInspectionController  {
    private final ExecutionInspectionService service;
    private final ExecutionInspectionQueryService queries;
    public ExecutionInspectionController(ExecutionInspectionService s,ExecutionInspectionQueryService q) {
        service=s;
        queries=q;
    }
    @PostMapping("/units/{snId}/inspections") public AjaxResult inspect(@PathVariable Long snId,@RequestBody ExecutionInspectionRequest r) {
        return service.inspect(snId,r);
    }
    @GetMapping("/units/{snId}/inspections") public AjaxResult history(@PathVariable Long snId) {
        return AjaxResult.success(queries.history(snId));
    }
    @GetMapping("/tasks/{taskId}/inspection-requirements") public AjaxResult requirements(@PathVariable Long taskId) {
        return AjaxResult.success(queries.requirements(taskId));
    }
    @ExceptionHandler(HttpMessageNotReadableException.class) public AjaxResult invalid() {
        return ExecutionException.validation("检验请求格式或字段无效").toAjaxResult();
    }
}
