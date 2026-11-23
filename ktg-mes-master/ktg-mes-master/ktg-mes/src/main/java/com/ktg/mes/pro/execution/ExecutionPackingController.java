package com.ktg.mes.pro.execution;
import com.ktg.common.core.domain.AjaxResult;
import org.springframework.web.bind.annotation.*;
import org.springframework.http.converter.HttpMessageNotReadableException;
@RestController
@RequestMapping("/mes/pro/execution")
public class ExecutionPackingController {
    private final ExecutionPackingService service;
    public ExecutionPackingController(ExecutionPackingService service) {this.service=service;}
    @PostMapping("/tasks/{taskId}/packages") public AjaxResult pack(@PathVariable Long taskId,@RequestBody ExecutionPackingRequest request) {return service.pack(taskId,request);}
    @ExceptionHandler(HttpMessageNotReadableException.class) public AjaxResult invalid() {return ExecutionException.validation("包装请求格式或字段无效").toAjaxResult();}
}
