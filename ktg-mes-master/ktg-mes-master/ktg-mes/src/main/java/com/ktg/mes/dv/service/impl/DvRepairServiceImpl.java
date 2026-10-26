package com.ktg.mes.dv.service.impl;

import java.util.List;
import java.util.Arrays;
import java.util.TreeSet;

import com.ktg.common.constant.UserConstants;
import com.ktg.common.utils.DateUtils;
import com.ktg.common.utils.StringUtils;
import com.ktg.common.exception.ServiceException;
import com.ktg.mes.dv.mapper.DvRepairLineMapper;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import com.ktg.mes.dv.mapper.DvRepairMapper;
import com.ktg.mes.dv.domain.DvRepair;
import com.ktg.mes.dv.service.IDvRepairService;

/**
 * 设备维修单Service业务层处理
 * 
 * @author yinjinlu
 * @date 2022-08-06
 */
@Service
public class DvRepairServiceImpl implements IDvRepairService 
{
    @Autowired
    private DvRepairMapper dvRepairMapper;

    @Autowired
    private DvRepairLineMapper dvRepairLineMapper;

    /**
     * 查询设备维修单
     * 
     * @param repairId 设备维修单主键
     * @return 设备维修单
     */
    @Override
    public DvRepair selectDvRepairByRepairId(Long repairId)
    {
        return dvRepairMapper.selectDvRepairByRepairId(repairId);
    }

    /**
     * 查询设备维修单列表
     * 
     * @param dvRepair 设备维修单
     * @return 设备维修单
     */
    @Override
    public List<DvRepair> selectDvRepairList(DvRepair dvRepair)
    {
        return dvRepairMapper.selectDvRepairList(dvRepair);
    }

    @Override
    public String checkCodeUnique(DvRepair dvRepair) {
        DvRepair rp = dvRepairMapper.checkCodeUnique(dvRepair);
        Long repairId = dvRepair.getRepairId() ==null?-1L: dvRepair.getRepairId();
        if(StringUtils.isNotNull(rp) && repairId.longValue() != rp.getRepairId().longValue()){
            return UserConstants.NOT_UNIQUE;
        }
        return UserConstants.UNIQUE;
    }

    /**
     * 新增设备维修单
     * 
     * @param dvRepair 设备维修单
     * @return 结果
     */
    @Override
    public int insertDvRepair(DvRepair dvRepair)
    {
        if (dvRepair.getStatus() != null && !UserConstants.ORDER_STATUS_PREPARE.equals(dvRepair.getStatus())) {
            throw new ServiceException("新建维修单只能使用草稿状态");
        }
        dvRepair.setRepairId(null);
        dvRepair.setStatus(UserConstants.ORDER_STATUS_PREPARE);
        dvRepair.setCreateTime(DateUtils.getNowDate());
        return dvRepairMapper.insertDvRepair(dvRepair);
    }

    /**
     * 修改设备维修单
     * 
     * @param dvRepair 设备维修单
     * @return 结果
     */
    @Override
    @Transactional(rollbackFor = Exception.class)
    public int updateDvRepair(DvRepair dvRepair)
    {
        requireDraft(dvRepair.getRepairId());
        if (dvRepair.getStatus() != null && !UserConstants.ORDER_STATUS_PREPARE.equals(dvRepair.getStatus())) {
            throw new ServiceException("维修单状态变更尚未开放");
        }
        dvRepair.setStatus(UserConstants.ORDER_STATUS_PREPARE);
        dvRepair.setUpdateTime(DateUtils.getNowDate());
        int updated = dvRepairMapper.updateDvRepair(dvRepair);
        if (updated != 1) throw new ServiceException("维修单修改数量异常，已回滚");
        return updated;
    }

    /**
     * 批量删除设备维修单
     * 
     * @param repairIds 需要删除的设备维修单主键
     * @return 结果
     */
    @Override
    @Transactional(rollbackFor = Exception.class)
    public int deleteDvRepairByRepairIds(Long[] repairIds)
    {
        if (repairIds == null || repairIds.length == 0 || Arrays.asList(repairIds).contains(null)) {
            throw new ServiceException("请选择维修单");
        }
        TreeSet<Long> orderedIds = new TreeSet<>(Arrays.asList(repairIds));
        for (Long repairId : orderedIds) requireDraft(repairId);
        for (Long repairId : orderedIds) dvRepairLineMapper.deleteByRepairId(repairId);
        int removed = dvRepairMapper.deleteDvRepairByRepairIds(orderedIds.toArray(new Long[0]));
        if (removed != orderedIds.size()) throw new ServiceException("维修单删除数量异常，已回滚");
        return removed;
    }

    /**
     * 删除设备维修单信息
     * 
     * @param repairId 设备维修单主键
     * @return 结果
     */
    @Override
    @Transactional(rollbackFor = Exception.class)
    public int deleteDvRepairByRepairId(Long repairId)
    {
        return deleteDvRepairByRepairIds(new Long[] { repairId });
    }

    private DvRepair requireDraft(Long repairId) {
        if (repairId == null) throw new ServiceException("维修单ID不能为空");
        DvRepair repair = dvRepairMapper.selectDvRepairForUpdate(repairId);
        if (repair == null) throw new ServiceException("维修单不存在，请刷新后重试");
        if (!UserConstants.ORDER_STATUS_PREPARE.equals(repair.getStatus())) {
            throw new ServiceException("只能维护草稿维修单");
        }
        return repair;
    }
}
