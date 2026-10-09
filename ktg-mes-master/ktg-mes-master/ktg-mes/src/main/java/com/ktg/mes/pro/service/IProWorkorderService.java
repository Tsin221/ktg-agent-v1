package com.ktg.mes.pro.service;

import java.util.List;
import com.ktg.common.core.domain.AjaxResult;
import com.ktg.mes.pro.domain.ProWorkorder;

/**
 * 生产工单Service接口
 * 
 * @author yinjinlu
 * @date 2022-05-09
 */
public interface IProWorkorderService 
{
    /**
     * 查询生产工单
     * 
     * @param workorderId 生产工单主键
     * @return 生产工单
     */
    public ProWorkorder selectProWorkorderByWorkorderId(Long workorderId);

    /**
     * 查询生产工单列表
     * 
     * @param proWorkorder 生产工单
     * @return 生产工单集合
     */
    public List<ProWorkorder> selectProWorkorderList(ProWorkorder proWorkorder);


    public String checkWorkorderCodeUnique(ProWorkorder proWorkorder);

    /**
     * 新增生产工单
     * 
     * @param proWorkorder 生产工单
     * @return 结果
     */
    public int insertProWorkorder(ProWorkorder proWorkorder);

    /** 原子创建草稿工单及其 BOM，返回数据库生成的工单 ID。 */
    public Long createDraftWorkorder(ProWorkorder proWorkorder);

    /**
     * 修改生产工单
     * 
     * @param proWorkorder 生产工单
     * @return 结果
     */
    public int updateProWorkorder(ProWorkorder proWorkorder);

    /** 原子校验并更新草稿；仅产品或数量变化时重建 BOM。 */
    public int updateDraftWorkorder(ProWorkorder proWorkorder);

    /**
     * 批量删除生产工单
     * 
     * @param workorderIds 需要删除的生产工单主键集合
     * @return 结果
     */
    public int deleteProWorkorderByWorkorderIds(Long[] workorderIds);

    /**
     * 删除生产工单信息
     * 
     * @param workorderId 生产工单主键
     * @return 结果
     */
    public int deleteProWorkorderByWorkorderId(Long workorderId);

    /** 查询首页生产工单及工序进度 */
    public AjaxResult getHomeList(ProWorkorder proWorkorder);

}
