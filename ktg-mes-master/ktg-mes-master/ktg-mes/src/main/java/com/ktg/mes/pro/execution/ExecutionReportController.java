package com.ktg.mes.pro.execution;

import com.fasterxml.jackson.annotation.JsonAnySetter;
import com.ktg.common.core.domain.AjaxResult;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/mes/pro/execution/tasks/{taskId}")
public class ExecutionReportController {
    private final ExecutionReportService service;
    public ExecutionReportController(ExecutionReportService service) { this.service = service; }

    @PostMapping("/reports")
    public AjaxResult report(@PathVariable Long taskId, @RequestBody ExecutionReportRequest request) {
        return service.report(taskId, request);
    }

    @PostMapping("/completions")
    public AjaxResult complete(@PathVariable Long taskId, @RequestBody CompletionRequest request) {
        return service.complete(taskId, request.operationId);
    }

    @ExceptionHandler(HttpMessageNotReadableException.class)
    public AjaxResult invalidBody() { return ExecutionException.validation("报工请求字段或格式无效").toAjaxResult(); }

    public static class CompletionRequest {
        public String operationId;
        @JsonAnySetter public void unknown(String name, Object value) { throw ExecutionException.validation("未知完结字段: " + name); }
    }
}
