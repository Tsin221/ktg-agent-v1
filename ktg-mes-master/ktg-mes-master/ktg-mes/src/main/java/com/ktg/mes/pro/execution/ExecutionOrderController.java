package com.ktg.mes.pro.execution;

import com.ktg.common.core.domain.AjaxResult;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/mes/pro/execution")
public class ExecutionOrderController {
    @ExceptionHandler(org.springframework.http.converter.HttpMessageNotReadableException.class)
    public AjaxResult invalidBody() {return ExecutionException.validation("下达请求字段或格式无效").toAjaxResult();}
    private final ExecutionReleaseService service; private final ExecutionOrderProgressService progress; private final ExecutionCompletionService completion;
    public ExecutionOrderController(ExecutionReleaseService service,ExecutionOrderProgressService progress,ExecutionCompletionService completion) {this.service=service;this.progress=progress;this.completion=completion;}
    @PostMapping("/workorders/{workorderId}/releases")
    public AjaxResult release(@PathVariable Long workorderId,@RequestBody ExecutionReleaseRequest request) {return service.release(workorderId,request);}
    @GetMapping("/workorders/{workorderId}/release-options")
    public AjaxResult options(@PathVariable Long workorderId) {return AjaxResult.success(service.options(workorderId));}
    @PostMapping("/workorders/{workorderId}/completions")
    public AjaxResult complete(@PathVariable Long workorderId,@RequestBody ExecutionCompletionRequest request) {return completion.complete(workorderId,request);}
    @GetMapping("/workorders/{workorderId}")
    public AjaxResult detail(@PathVariable Long workorderId) {java.util.Map<String,Object> result=service.detail(workorderId); result.put("progress",progress.progress(workorderId)); return AjaxResult.success(result);}
}
