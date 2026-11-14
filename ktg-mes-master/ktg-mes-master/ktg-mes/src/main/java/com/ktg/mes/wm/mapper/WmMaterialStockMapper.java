package com.ktg.mes.wm.mapper;

import com.ktg.mes.wm.domain.WmMaterialStock;
import com.ktg.mes.wm.domain.WmStockSummaryGroup;

import java.util.List;
import org.apache.ibatis.annotations.Param;

/**
 * 库存记录Mapper接口
 * 
 * @author yinjinlu
 * @date 2022-05-24
 */
public interface WmMaterialStockMapper 
{
    /**
     * 查询库存记录
     * 
     * @param materialStockId 库存记录主键
     * @return 库存记录
     */
    public WmMaterialStock selectWmMaterialStockByMaterialStockId(Long materialStockId);


    List<WmStockSummaryGroup> selectStockSummary(@Param("itemCode") String itemCode,
                                                  @Param("warehouseId") Long warehouseId);

    public WmMaterialStock loadMaterialStock(WmMaterialStock stock);

    /** Atomically add to a dimension, creating it exactly once via the unique key. */
    int insertOrIncrementStock(WmMaterialStock stock);

    /** Returns one only when the requested debit was applied. */
    int decrementStock(@org.apache.ibatis.annotations.Param("id") Long id,
                       @org.apache.ibatis.annotations.Param("quantity") java.math.BigDecimal quantity,
                       @org.apache.ibatis.annotations.Param("checkBalance") boolean checkBalance);

    /**
     * 查询库存记录列表
     * 
     * @param wmMaterialStock 库存记录
     * @return 库存记录集合
     */
    public List<WmMaterialStock> selectWmMaterialStockList(WmMaterialStock wmMaterialStock);

    /**
     * 新增库存记录
     * 
     * @param wmMaterialStock 库存记录
     * @return 结果
     */
    public int insertWmMaterialStock(WmMaterialStock wmMaterialStock);

    /**
     * 修改库存记录
     * 
     * @param wmMaterialStock 库存记录
     * @return 结果
     */
    public int updateWmMaterialStock(WmMaterialStock wmMaterialStock);

    /**
     * 删除库存记录
     * 
     * @param materialStockId 库存记录主键
     * @return 结果
     */
    public int deleteWmMaterialStockByMaterialStockId(Long materialStockId);

    /**
     * 批量删除库存记录
     * 
     * @param materialStockIds 需要删除的数据主键集合
     * @return 结果
     */
    public int deleteWmMaterialStockByMaterialStockIds(Long[] materialStockIds);
}
