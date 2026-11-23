package com.ktg.mes.pro.execution;

import com.ktg.common.core.domain.AjaxResult;
import com.ktg.common.core.page.TableDataInfo;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/mes/pro/execution/tasks")
public class ExecutionTaskController {
    private final ExecutionTaskQueryService queries;
    private final ExecutionAssignmentService assignments;
    private final ExecutionTaskStartService starts;

    public ExecutionTaskController(ExecutionTaskQueryService queries,ExecutionAssignmentService assignments,ExecutionTaskStartService starts) {
        this.queries=queries;this.assignments=assignments;this.starts=starts;
    }

    @ExceptionHandler(HttpMessageNotReadableException.class)
    public AjaxResult invalidBody() { return ExecutionException.validation("任务请求字段或格式无效").toAjaxResult(); }

    @GetMapping
    public TableDataInfo list(@RequestParam(defaultValue="1") int pageNum,@RequestParam(defaultValue="20") int pageSize,
                              @RequestParam(required=false) Long workorderId,@RequestParam(required=false) String taskKind,
                              @RequestParam(required=false) String status) {
        return queries.list(pageNum,pageSize,workorderId,taskKind,status);
    }

    @GetMapping("/{taskId}")
    public AjaxResult detail(@PathVariable Long taskId) {return AjaxResult.success(queries.detail(taskId));}

    @GetMapping("/{taskId}/units")
    public TableDataInfo units(@PathVariable Long taskId,@RequestParam(defaultValue="1") int pageNum,
                               @RequestParam(defaultValue="20") int pageSize,@RequestParam(required=false) String state,
                               @RequestParam(required=false) String snCode) {
        return queries.units(taskId,pageNum,pageSize,state,snCode);
    }

    @GetMapping("/{taskId}/assignment-options")
    public AjaxResult assignmentOptions(@PathVariable Long taskId) {return AjaxResult.success(assignments.options(taskId));}

    @PostMapping("/{taskId}/assignments")
    public AjaxResult assign(@PathVariable Long taskId,@RequestBody ExecutionTaskAssignmentRequest request) {return assignments.assign(taskId,request);}

    @PostMapping("/{taskId}/starts")
    public AjaxResult start(@PathVariable Long taskId,@RequestBody ExecutionTaskStartRequest request) {return starts.start(taskId,request);}
}
