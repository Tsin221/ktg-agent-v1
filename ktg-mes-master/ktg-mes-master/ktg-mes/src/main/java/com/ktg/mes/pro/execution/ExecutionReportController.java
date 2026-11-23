package com.ktg.mes.pro.execution;

import com.fasterxml.jackson.annotation.JsonAnySetter;
import com.ktg.common.core.domain.AjaxResult;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/mes/pro/execution/tasks/{taskId}")
public class ExecutionReportController {
    private final ExecutionReportService service;
    private final ExecutionReworkService rework;
    private final ExecutionInspectionService inspections;
    private final ExecutionTaskQueryService tasks;
    public ExecutionReportController(ExecutionReportService service, ExecutionReworkService rework, ExecutionInspectionService inspections, ExecutionTaskQueryService tasks) { this.service = service; this.rework=rework; this.inspections=inspections; this.tasks=tasks; }

    @PostMapping("/reports")
    public AjaxResult report(@PathVariable Long taskId, @RequestBody ExecutionReportRequest request) {
        return service.report(taskId, request);
    }

    @PostMapping("/completions")
    public AjaxResult complete(@PathVariable Long taskId, @RequestBody CompletionRequest request) {
        if(request==null) throw ExecutionException.validation("完结请求不能为空");
        java.util.Map<String,Object> task=tasks.taskRow(taskId,false);
        if("REWORK".equals(task.get("task_kind")))return rework.complete(taskId,request.operationId);
        if("INSPECTION".equals(task.get("step_kind")))return inspections.complete(taskId,request.operationId);
        return service.complete(taskId, request.operationId);
    }

    @ExceptionHandler(HttpMessageNotReadableException.class)
    public AjaxResult invalidBody() { return ExecutionException.validation("报工请求字段或格式无效").toAjaxResult(); }

    public static class CompletionRequest {
        public String operationId;
        @JsonAnySetter public void unknown(String name, Object value) { throw ExecutionException.validation("未知完结字段: " + name); }
    }
}
