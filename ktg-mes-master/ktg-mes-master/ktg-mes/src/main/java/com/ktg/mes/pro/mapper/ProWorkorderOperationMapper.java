package com.ktg.mes.pro.mapper;

import com.ktg.mes.pro.domain.ProWorkorderOperation;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

@Mapper
public interface ProWorkorderOperationMapper {
    ProWorkorderOperation selectById(@Param("operationId") String operationId);
    int insert(ProWorkorderOperation operation);
    int complete(ProWorkorderOperation operation);
}
