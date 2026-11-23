package com.ktg.mes.pro.execution;

import com.ktg.common.core.page.TableDataInfo;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/mes/pro/execution/tasks")
public class ExecutionMaterialController {
    private final ExecutionMaterialQueryService service;
    public ExecutionMaterialController(ExecutionMaterialQueryService service) { this.service = service; }

    @GetMapping("/{taskId}/available-materials")
    public TableDataInfo available(@PathVariable Long taskId,
                                   @RequestParam(required = false) Long itemId,
                                   @RequestParam(required = false) String batchCode,
                                   @RequestParam(defaultValue = "1") int pageNum,
                                   @RequestParam(defaultValue = "20") int pageSize) {
        return service.available(taskId, itemId, batchCode, pageNum, pageSize);
    }
}
