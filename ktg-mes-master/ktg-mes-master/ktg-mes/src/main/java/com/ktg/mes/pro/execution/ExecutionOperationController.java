package com.ktg.mes.pro.execution;

import com.ktg.common.core.domain.AjaxResult;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/** Only receipt recovery is active in B01; business write endpoints arrive in B02–B05. */
@RestController
@RequestMapping("/mes/pro/execution/operations")
public class ExecutionOperationController {
    private final ExecutionOperationService operations;

    public ExecutionOperationController(ExecutionOperationService operations) {
        this.operations = operations;
    }

    // Service checks the current login, query + original action permission and object scope,
    // deliberately returning the same 404 for nonexistent and undisclosable receipts.
    @GetMapping("/{operationId}")
    public ResponseEntity<AjaxResult> result(@PathVariable("operationId") String operationId) {
        AjaxResult response = operations.result(operationId);
        return ResponseEntity.status((Integer) response.get("code")).body(response);
    }
}
