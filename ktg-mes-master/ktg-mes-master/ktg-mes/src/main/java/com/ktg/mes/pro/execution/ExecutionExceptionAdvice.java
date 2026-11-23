package com.ktg.mes.pro.execution;

import com.ktg.common.core.domain.AjaxResult;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

/** Preserve stable business errors for guarded legacy controllers and future execution controllers. */
@RestControllerAdvice(basePackages = "com.ktg.mes")
@Order(Ordered.HIGHEST_PRECEDENCE)
public class ExecutionExceptionAdvice {
    @ExceptionHandler(ExecutionException.class)
    public AjaxResult handleExecutionRejection(ExecutionException error) {
        return error.toAjaxResult();
    }
}
