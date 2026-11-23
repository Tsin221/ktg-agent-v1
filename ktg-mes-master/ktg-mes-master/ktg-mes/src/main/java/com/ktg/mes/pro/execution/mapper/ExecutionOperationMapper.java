package com.ktg.mes.pro.execution.mapper;

import com.ktg.mes.pro.execution.ExecutionOperation;
import org.apache.ibatis.annotations.Param;

public interface ExecutionOperationMapper {
    Long lockWorkorder(@Param("workorderId") Long workorderId);
    ExecutionOperation selectById(@Param("operationId") String operationId);
    int insertClaim(ExecutionOperation operation);
    int complete(@Param("operationId") String operationId, @Param("resultRef") String resultRef);
}
