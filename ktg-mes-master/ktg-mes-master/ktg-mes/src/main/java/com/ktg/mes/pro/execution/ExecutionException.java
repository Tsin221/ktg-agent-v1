package com.ktg.mes.pro.execution;

import com.ktg.common.core.domain.AjaxResult;

/** Explicit business rejection; throwing it aborts the whole execution transaction. */
public class ExecutionException extends RuntimeException {
    private final int code;
    private final String errorCode;

    public ExecutionException(int code, String errorCode, String message) {
        super(message);
        this.code = code;
        this.errorCode = errorCode;
    }

    public int getCode() { return code; }
    public String getErrorCode() { return errorCode; }

    public AjaxResult toAjaxResult() {
        return AjaxResult.error(code, getMessage()).put("errorCode", errorCode);
    }

    public static ExecutionException validation(String message) {
        return new ExecutionException(400, "VALIDATION_ERROR", message);
    }

    public static ExecutionException forbidden() {
        return new ExecutionException(403, "FORBIDDEN", "没有执行此操作的权限");
    }

    public static ExecutionException notFound() {
        return new ExecutionException(404, "NOT_FOUND", "记录不存在");
    }

    public static ExecutionException stateConflict(String message) {
        return new ExecutionException(409, "STATE_CONFLICT", message);
    }

    public static ExecutionException operationConflict() {
        return new ExecutionException(409, "OPERATION_CONFLICT", "操作 ID 已绑定其他请求");
    }

    public static ExecutionException resultUnknown() {
        return new ExecutionException(202, "RESULT_UNKNOWN", "操作结果待核实，请按原操作 ID 查询");
    }
}
