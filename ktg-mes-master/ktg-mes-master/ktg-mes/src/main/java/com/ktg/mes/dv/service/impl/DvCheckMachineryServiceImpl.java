package com.ktg.mes.dv.service.impl;

import java.util.List;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.TreeSet;

import com.ktg.common.constant.UserConstants;
import com.ktg.common.utils.DateUtils;
import com.ktg.common.utils.StringUtils;
import com.ktg.common.exception.ServiceException;
import com.ktg.mes.dv.domain.DvCheckPlan;
import com.ktg.mes.dv.mapper.DvCheckPlanMapper;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import com.ktg.mes.dv.mapper.DvCheckMachineryMapper;
import com.ktg.mes.dv.domain.DvCheckMachinery;
import com.ktg.mes.dv.service.IDvCheckMachineryService;

/**
 * 点检设备Service业务层处理
 * 
 * @author yinjinlu
 * @date 2022-06-17
 */
@Service
public class DvCheckMachineryServiceImpl implements IDvCheckMachineryService 
{
    @Autowired
    private DvCheckMachineryMapper dvCheckMachineryMapper;

    @Autowired
    private DvCheckPlanMapper dvCheckPlanMapper;

    /**
     * 查询点检设备
     * 
     * @param recordId 点检设备主键
     * @return 点检设备
     */
    @Override
    public DvCheckMachinery selectDvCheckMachineryByRecordId(Long recordId)
    {
        return dvCheckMachineryMapper.selectDvCheckMachineryByRecordId(recordId);
    }

    /**
     * 查询点检设备列表
     * 
     * @param dvCheckMachinery 点检设备
     * @return 点检设备
     */
    @Override
    public List<DvCheckMachinery> selectDvCheckMachineryList(DvCheckMachinery dvCheckMachinery)
    {
        return dvCheckMachineryMapper.selectDvCheckMachineryList(dvCheckMachinery);
    }

    @Override
    public String checkMachineryUnique(DvCheckMachinery dvCheckMachinery) {
        DvCheckMachinery machinery = dvCheckMachineryMapper.checkMachineryUnique(dvCheckMachinery);
        Long recordId = dvCheckMachinery.getRecordId()==null?-1L:dvCheckMachinery.getRecordId();
        if(StringUtils.isNotNull(machinery) && machinery.getRecordId().longValue() != recordId.longValue()){
            return UserConstants.NOT_UNIQUE;
        }
        return UserConstants.UNIQUE;
    }

    /**
     * 新增点检设备
     * 
     * @param dvCheckMachinery 点检设备
     * @return 结果
     */
    @Override
    @Transactional(rollbackFor = Exception.class)
    public int insertDvCheckMachinery(DvCheckMachinery dvCheckMachinery)
    {
        dvCheckMachinery.setRecordId(null);
        requirePreparePlan(dvCheckMachinery.getPlanId());
        if (dvCheckMachineryMapper.checkMachineryUnique(dvCheckMachinery) != null) {
            throw new ServiceException("该设备已关联到同类型计划");
        }
        dvCheckMachinery.setCreateTime(DateUtils.getNowDate());
        return dvCheckMachineryMapper.insertDvCheckMachinery(dvCheckMachinery);
    }

    /**
     * 修改点检设备
     * 
     * @param dvCheckMachinery 点检设备
     * @return 结果
     */
    @Override
    @Transactional(rollbackFor = Exception.class)
    public int updateDvCheckMachinery(DvCheckMachinery dvCheckMachinery)
    {
        DvCheckMachinery current = dvCheckMachineryMapper.selectDvCheckMachineryByRecordId(dvCheckMachinery.getRecordId());
        if (current == null) throw new ServiceException("设备关联记录不存在");
        if ((dvCheckMachinery.getPlanId() != null && !dvCheckMachinery.getPlanId().equals(current.getPlanId()))
                || (dvCheckMachinery.getMachineryId() != null && !dvCheckMachinery.getMachineryId().equals(current.getMachineryId()))) {
            throw new ServiceException("不允许更改设备关联的计划或源设备");
        }
        requirePreparePlan(current.getPlanId());
        dvCheckMachinery.setUpdateTime(DateUtils.getNowDate());
        int updated = dvCheckMachineryMapper.updateDvCheckMachinery(dvCheckMachinery);
        if (updated != 1) throw new ServiceException("设备关联记录已变化，请刷新后重试");
        return updated;
    }

    /**
     * 批量删除点检设备
     * 
     * @param recordIds 需要删除的点检设备主键
     * @return 结果
     */
    @Override
    @Transactional(rollbackFor = Exception.class)
    public int deleteDvCheckMachineryByRecordIds(Long[] recordIds)
    {
        List<Long> ids = validateIds(recordIds);
        List<DvCheckMachinery> records = dvCheckMachineryMapper.selectDvCheckMachineryByRecordIds(recordIds);
        if (records.size() != ids.size()) throw new ServiceException("部分设备关联记录不存在");
        List<Long> planIds = new ArrayList<Long>();
        for (DvCheckMachinery record : records) planIds.add(record.getPlanId());
        requirePreparePlans(planIds);
        List<DvCheckMachinery> currentRecords = dvCheckMachineryMapper.selectDvCheckMachineryByRecordIds(recordIds);
        if (!samePlanOwnership(records, currentRecords)) throw new ServiceException("设备关联记录已变化，请刷新后重试");
        int deleted = dvCheckMachineryMapper.deleteDvCheckMachineryByRecordIds(ids.toArray(new Long[ids.size()]));
        if (deleted != ids.size()) throw new ServiceException("设备关联记录已变化，请刷新后重试");
        return deleted;
    }

    /**
     * 删除点检设备信息
     * 
     * @param recordId 点检设备主键
     * @return 结果
     */
    @Override
    @Transactional(rollbackFor = Exception.class)
    public int deleteDvCheckMachineryByRecordId(Long recordId)
    {
        return deleteDvCheckMachineryByRecordIds(new Long[] { recordId });
    }

    private void requirePreparePlan(Long planId) {
        if (planId == null) throw new ServiceException("计划编号不能为空");
        DvCheckPlan plan = dvCheckPlanMapper.selectDvCheckPlanForUpdate(planId);
        if (plan == null) throw new ServiceException("目标计划不存在");
        if (!UserConstants.ORDER_STATUS_PREPARE.equals(plan.getStatus())) throw new ServiceException("仅草稿计划允许修改设备关联");
    }

    private void requirePreparePlans(List<Long> planIds) {
        List<Long> sorted = new ArrayList<Long>(new TreeSet<Long>(planIds));
        List<DvCheckPlan> plans = dvCheckPlanMapper.selectDvCheckPlansForUpdate(sorted);
        if (plans.size() != sorted.size()) throw new ServiceException("设备关联的计划不存在");
        for (DvCheckPlan plan : plans) {
            if (!UserConstants.ORDER_STATUS_PREPARE.equals(plan.getStatus())) throw new ServiceException("仅草稿计划允许移除设备关联");
        }
    }

    private List<Long> validateIds(Long[] recordIds) {
        if (recordIds == null || recordIds.length == 0 || Arrays.asList(recordIds).contains(null)) throw new ServiceException("请选择设备关联记录");
        return new ArrayList<Long>(new TreeSet<Long>(Arrays.asList(recordIds)));
    }

    private boolean samePlanOwnership(List<DvCheckMachinery> before, List<DvCheckMachinery> after) {
        if (before.size() != after.size()) return false;
        for (int i = 0; i < before.size(); i++) {
            if (!before.get(i).getRecordId().equals(after.get(i).getRecordId())
                    || !before.get(i).getPlanId().equals(after.get(i).getPlanId())) return false;
        }
        return true;
    }
}
