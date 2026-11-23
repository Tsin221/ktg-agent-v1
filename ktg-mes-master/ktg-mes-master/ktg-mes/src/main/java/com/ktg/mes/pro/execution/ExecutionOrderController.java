package com.ktg.mes.pro.execution;

import com.ktg.common.core.domain.AjaxResult;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/mes/pro/execution")
public class ExecutionOrderController {
    @ExceptionHandler(org.springframework.http.converter.HttpMessageNotReadableException.class)
    public AjaxResult invalidBody() {return ExecutionException.validation("下达请求字段或格式无效").toAjaxResult();}
    private final ExecutionReleaseService service;
    public ExecutionOrderController(ExecutionReleaseService service) {this.service=service;}
    @PostMapping("/workorders/{workorderId}/releases")
    public AjaxResult release(@PathVariable Long workorderId,@RequestBody ExecutionReleaseRequest request) {return service.release(workorderId,request);}
    @GetMapping("/workorders/{workorderId}/release-options")
    public AjaxResult options(@PathVariable Long workorderId) {return AjaxResult.success(service.options(workorderId));}
    @GetMapping("/workorders/{workorderId}")
    public AjaxResult detail(@PathVariable Long workorderId) {return AjaxResult.success(service.detail(workorderId));}
}
