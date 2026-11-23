package com.ktg.mes.pro.execution.mapper;

import org.apache.ibatis.annotations.Param;
import java.util.List;
import java.util.Map;

/** Read projections over durable execution facts; never joins on guessed batch/product relationships. */
public interface ExecutionTraceMapper {
    List<Map<String,Object>> identity(@Param("snCode") String snCode);
    List<Map<String,Object>> tasks(@Param("snId") Long snId,@Param("workorderId") Long workorderId);
    List<Map<String,Object>> inputs(@Param("snId") Long snId,@Param("workorderId") Long workorderId);
    List<Map<String,Object>> transactions(@Param("sourceDocId") Long sourceDocId,@Param("sourceDocLineId") Long sourceDocLineId,
                                         @Param("workorderId") Long workorderId,@Param("sourceDocType") String sourceDocType);
    List<Map<String,Object>> inspections(@Param("snId") Long snId,@Param("workorderId") Long workorderId);
    List<Map<String,Object>> inspectionLines(@Param("ipqcId") Long ipqcId);
    List<Map<String,Object>> inspectionDefects(@Param("ipqcId") Long ipqcId);
    List<Map<String,Object>> dispositions(@Param("snId") Long snId,@Param("workorderId") Long workorderId);
    Map<String,Object> packing(@Param("snId") Long snId,@Param("workorderId") Long workorderId);
    Map<String,Object> assemblyFeedback(@Param("snId") Long snId,@Param("workorderId") Long workorderId);
    long countMaterials(@Param("itemId") Long itemId,@Param("batchCode") String batchCode,
                        @Param("actorId") Long actorId,@Param("manageAll") boolean manageAll);
    List<Map<String,Object>> materialRows(@Param("itemId") Long itemId,@Param("batchCode") String batchCode,
                                         @Param("actorId") Long actorId,@Param("manageAll") boolean manageAll,
                                         @Param("pageSize") int pageSize,@Param("offset") long offset);
}
