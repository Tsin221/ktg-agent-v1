package com.ktg.mes.pro.execution;

/** Request boundary shared by HTTP and direct service callers. */
final class ExecutionTraceQuery {
    private ExecutionTraceQuery() { }

    static String sn(String value) { return required(value,64,"SN"); }
    static String batch(String value) { return required(value,255,"原料批次"); }

    static void material(Long itemId,int pageNum,int pageSize) {
        ExecutionTaskQueryService.positive(itemId);
        ExecutionTaskQueryService.checkPage(pageNum,pageSize);
    }

    private static String required(String value,int limit,String label) {
        if(value==null || value.trim().isEmpty() || value.length()>limit)
            throw ExecutionException.validation(label+"不能为空或超过长度限制");
        // Do not normalize persisted identifiers: nonblank legacy SNs may have
        // leading spaces, and raw-material batch codes are exact stock identities.
        return value;
    }
}
