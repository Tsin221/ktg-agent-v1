package com.ktg.mes.pro.execution;
import com.ktg.common.core.domain.AjaxResult;
import org.springframework.web.bind.annotation.*;
@RestController
@RequestMapping("/mes/pro/execution/units/{snId}/dispositions")
public class ExecutionDispositionController  {
    private final ExecutionDispositionService service;
    public ExecutionDispositionController(ExecutionDispositionService service) {
        this.service=service;
    }
    @PostMapping public AjaxResult dispose(@PathVariable Long snId,@RequestBody ExecutionDispositionRequest request) {
        return service.dispose(snId,request);
    }
}
