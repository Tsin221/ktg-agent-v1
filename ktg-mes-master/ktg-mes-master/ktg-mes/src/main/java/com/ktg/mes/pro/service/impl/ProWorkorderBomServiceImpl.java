package com.ktg.mes.pro.service.impl;

import java.util.List;
import com.ktg.common.constant.HttpStatus;
import com.ktg.common.constant.UserConstants;
import com.ktg.common.exception.ServiceException;
import com.ktg.common.utils.DateUtils;
import com.ktg.mes.pro.domain.ProWorkorder;
import com.ktg.mes.pro.mapper.ProWorkorderMapper;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import com.ktg.mes.pro.mapper.ProWorkorderBomMapper;
import com.ktg.mes.pro.domain.ProWorkorderBom;
import com.ktg.mes.pro.service.IProWorkorderBomService;

/**
 * 生产工单BOM组成Service业务层处理
 * 
 * @author yinjinlu
 * @date 2022-05-09
 */
@Service
public class ProWorkorderBomServiceImpl implements IProWorkorderBomService 
{
    @Autowired
    private ProWorkorderBomMapper proWorkorderBomMapper;

    @Autowired
    private ProWorkorderMapper proWorkorderMapper;

    /**
     * 查询生产工单BOM组成
     * 
     * @param lineId 生产工单BOM组成主键
     * @return 生产工单BOM组成
     */
    @Override
    public ProWorkorderBom selectProWorkorderBomByLineId(Long lineId)
    {
        return proWorkorderBomMapper.selectProWorkorderBomByLineId(lineId);
    }

    /**
     * 查询生产工单BOM组成列表
     * 
     * @param proWorkorderBom 生产工单BOM组成
     * @return 生产工单BOM组成
     */
    @Override
    public List<ProWorkorderBom> selectProWorkorderBomList(ProWorkorderBom proWorkorderBom)
    {
        return proWorkorderBomMapper.selectProWorkorderBomList(proWorkorderBom);
    }

    /**
     * 新增生产工单BOM组成
     * 
     * @param proWorkorderBom 生产工单BOM组成
     * @return 结果
     */
    @Override
    @Transactional(rollbackFor = Exception.class)
    public int insertProWorkorderBom(ProWorkorderBom proWorkorderBom)
    {
        if (proWorkorderBom == null || proWorkorderBom.getWorkorderId() == null)
        {
            throw new ServiceException("工单 ID 不能为空！", HttpStatus.BAD_REQUEST);
        }
        Long workorderId = proWorkorderBom.getWorkorderId();
        // 与批量删除锁定同一工单行，使 BOM 新增和工单删除按事务串行化。
        ProWorkorder workorder = proWorkorderMapper.selectProWorkorderByWorkorderIdForUpdate(workorderId);
        if (workorder == null)
        {
            throw new ServiceException("生产工单不存在：" + workorderId, HttpStatus.NOT_FOUND);
        }
        if (!UserConstants.ORDER_STATUS_PREPARE.equals(workorder.getStatus()))
        {
            throw new ServiceException("只能修改草稿状态生产工单 BOM：" + workorderId, HttpStatus.CONFLICT);
        }
        proWorkorderBom.setCreateTime(DateUtils.getNowDate());
        return proWorkorderBomMapper.insertProWorkorderBom(proWorkorderBom);
    }

    /**
     * 修改生产工单BOM组成
     * 
     * @param proWorkorderBom 生产工单BOM组成
     * @return 结果
     */
    @Override
    public int updateProWorkorderBom(ProWorkorderBom proWorkorderBom)
    {
        proWorkorderBom.setUpdateTime(DateUtils.getNowDate());
        return proWorkorderBomMapper.updateProWorkorderBom(proWorkorderBom);
    }

    /**
     * 批量删除生产工单BOM组成
     * 
     * @param lineIds 需要删除的生产工单BOM组成主键
     * @return 结果
     */
    @Override
    public int deleteProWorkorderBomByLineIds(Long[] lineIds)
    {
        return proWorkorderBomMapper.deleteProWorkorderBomByLineIds(lineIds);
    }

    /**
     * 删除生产工单BOM组成信息
     * 
     * @param lineId 生产工单BOM组成主键
     * @return 结果
     */
    @Override
    public int deleteProWorkorderBomByLineId(Long lineId)
    {
        return proWorkorderBomMapper.deleteProWorkorderBomByLineId(lineId);
    }

    @Override
    public int deleteProWorkorderBomByWorkorderId(Long workorderId) {
        return proWorkorderBomMapper.deleteProWorkorderBomByWorkorderId(workorderId);
    }
}
