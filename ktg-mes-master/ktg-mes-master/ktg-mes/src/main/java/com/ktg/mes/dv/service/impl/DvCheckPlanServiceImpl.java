package com.ktg.mes.dv.service.impl;

import java.util.List;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.TreeSet;

import com.ktg.common.constant.UserConstants;
import com.ktg.common.utils.DateUtils;
import com.ktg.common.exception.ServiceException;
import com.ktg.mes.dv.mapper.DvCheckMachineryMapper;
import com.ktg.mes.dv.mapper.DvCheckSubjectMapper;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import com.ktg.mes.dv.mapper.DvCheckPlanMapper;
import com.ktg.mes.dv.domain.DvCheckPlan;
import com.ktg.mes.dv.service.IDvCheckPlanService;

/**
 * 设备点检计划头Service业务层处理
 * 
 * @author yinjinlu
 * @date 2022-06-16
 */
@Service
public class DvCheckPlanServiceImpl implements IDvCheckPlanService 
{
    @Autowired
    private DvCheckPlanMapper dvCheckPlanMapper;

    @Autowired
    private DvCheckMachineryMapper dvCheckMachineryMapper;

    @Autowired
    private DvCheckSubjectMapper dvCheckSubjectMapper;

    /**
     * 查询设备点检计划头
     * 
     * @param planId 设备点检计划头主键
     * @return 设备点检计划头
     */
    @Override
    public DvCheckPlan selectDvCheckPlanByPlanId(Long planId)
    {
        return dvCheckPlanMapper.selectDvCheckPlanByPlanId(planId);
    }

    /**
     * 查询设备点检计划头列表
     * 
     * @param dvCheckPlan 设备点检计划头
     * @return 设备点检计划头
     */
    @Override
    public List<DvCheckPlan> selectDvCheckPlanList(DvCheckPlan dvCheckPlan)
    {
        return dvCheckPlanMapper.selectDvCheckPlanList(dvCheckPlan);
    }

    @Override
    public String checkPlanCodeUnique(DvCheckPlan dvCheckPlan) {
        int conflicts = dvCheckPlanMapper.countPlanCodeConflicts(dvCheckPlan);
        return conflicts > 0 ? UserConstants.NOT_UNIQUE : UserConstants.UNIQUE;
    }

    /**
     * 新增设备点检计划头
     * 
     * @param dvCheckPlan 设备点检计划头
     * @return 结果
     */
    @Override
    public int insertDvCheckPlan(DvCheckPlan dvCheckPlan)
    {
        if (dvCheckPlan.getStatus() != null && !UserConstants.ORDER_STATUS_PREPARE.equals(dvCheckPlan.getStatus())) {
            throw new ServiceException("新建计划只能使用草稿状态");
        }
        dvCheckPlan.setPlanId(null);
        dvCheckPlan.setStatus(UserConstants.ORDER_STATUS_PREPARE);
        dvCheckPlan.setCreateTime(DateUtils.getNowDate());
        return dvCheckPlanMapper.insertDvCheckPlan(dvCheckPlan);
    }

    /**
     * 修改设备点检计划头
     * 
     * @param dvCheckPlan 设备点检计划头
     * @return 结果
     */
    @Override
    @Transactional(rollbackFor = Exception.class)
    public int updateDvCheckPlan(DvCheckPlan dvCheckPlan)
    {
        DvCheckPlan current = requireLockedPlan(dvCheckPlan.getPlanId());
        String requestedStatus = dvCheckPlan.getStatus();
        if (UserConstants.ORDER_STATUS_FINISHED.equals(current.getStatus())) {
            if (!UserConstants.ORDER_STATUS_PREPARE.equals(requestedStatus)) {
                throw new ServiceException("已启用计划只能停用，不能编辑或重复启用");
            }
            assertNoPlanFieldChanges(current, dvCheckPlan);
            dvCheckPlan.setPlanCode(null);
            dvCheckPlan.setPlanName(null);
            dvCheckPlan.setPlanType(null);
            dvCheckPlan.setStartDate(null);
            dvCheckPlan.setEndDate(null);
            dvCheckPlan.setCycleType(null);
            dvCheckPlan.setCycleCount(null);
            dvCheckPlan.setRemark(null);
            dvCheckPlan.setAttr1(null);
            dvCheckPlan.setAttr2(null);
            dvCheckPlan.setAttr3(null);
            dvCheckPlan.setAttr4(null);
            dvCheckPlan.setCreateBy(null);
            dvCheckPlan.setCreateTime(null);
            dvCheckPlan.setUpdateBy(null);
        } else if (UserConstants.ORDER_STATUS_PREPARE.equals(current.getStatus())) {
            if (requestedStatus != null && !UserConstants.ORDER_STATUS_PREPARE.equals(requestedStatus)
                    && !UserConstants.ORDER_STATUS_FINISHED.equals(requestedStatus)) {
                throw new ServiceException("计划状态迁移无效");
            }
            if (requestedStatus != null && UserConstants.ORDER_STATUS_FINISHED.equals(requestedStatus)
                    && (dvCheckPlanMapper.countPlanMachinery(current.getPlanId()) < 1
                    || dvCheckPlanMapper.countPlanSubjects(current.getPlanId()) < 1)) {
                throw new ServiceException("启用计划前必须至少关联一台设备和一个项目");
            }
            if (dvCheckPlan.getPlanType() != null && !dvCheckPlan.getPlanType().equals(current.getPlanType())) {
                if (dvCheckPlanMapper.countPlanMachinery(current.getPlanId()) > 0
                        || dvCheckPlanMapper.countPlanSubjects(current.getPlanId()) > 0) {
                    throw new ServiceException("已有设备或项目关联的计划不允许切换类型");
                }
            }
        } else {
            throw new ServiceException("计划当前状态不受支持，无法修改");
        }
        dvCheckPlan.setUpdateTime(DateUtils.getNowDate());
        int updated = dvCheckPlanMapper.updateDvCheckPlan(dvCheckPlan);
        if (updated != 1) throw new ServiceException("计划状态已变化，请刷新后重试");
        return updated;
    }

    /**
     * 批量删除设备点检计划头
     * 
     * @param planIds 需要删除的设备点检计划头主键
     * @return 结果
     */
    @Override
    @Transactional(rollbackFor = Exception.class)
    public int deleteDvCheckPlanByPlanIds(Long[] planIds)
    {
        if (planIds == null || planIds.length == 0 || Arrays.asList(planIds).contains(null)) {
            throw new ServiceException("请选择需要删除的计划");
        }
        List<Long> orderedIds = new ArrayList<Long>(new TreeSet<Long>(Arrays.asList(planIds)));
        List<DvCheckPlan> locked = dvCheckPlanMapper.selectDvCheckPlansForUpdate(orderedIds);
        if (locked.size() != orderedIds.size()) {
            throw new ServiceException("部分计划不存在，请刷新后重试");
        }
        for (DvCheckPlan plan : locked) {
            if (!UserConstants.ORDER_STATUS_PREPARE.equals(plan.getStatus())) {
                throw new ServiceException("只能删除草稿状态计划");
            }
        }
        for (Long planId : orderedIds) {
            dvCheckMachineryMapper.deleteByPlanId(planId);
            dvCheckSubjectMapper.deleteByPlanId(planId);
        }
        return dvCheckPlanMapper.deleteDvCheckPlanByPlanIds(planIds);
    }

    /**
     * 删除设备点检计划头信息
     * 
     * @param planId 设备点检计划头主键
     * @return 结果
     */
    @Override
    @Transactional(rollbackFor = Exception.class)
    public int deleteDvCheckPlanByPlanId(Long planId)
    {
        return deleteDvCheckPlanByPlanIds(new Long[] { planId });
    }

    private DvCheckPlan requireLockedPlan(Long planId) {
        if (planId == null) throw new ServiceException("计划编号不能为空");
        DvCheckPlan current = dvCheckPlanMapper.selectDvCheckPlanForUpdate(planId);
        if (current == null) throw new ServiceException("计划不存在，请刷新后重试");
        return current;
    }

    private void assertNoPlanFieldChanges(DvCheckPlan current, DvCheckPlan request) {
        if (changedString(request.getPlanCode(), current.getPlanCode())
                || changedString(request.getPlanName(), current.getPlanName())
                || changedString(request.getPlanType(), current.getPlanType())
                || changed(request.getStartDate(), current.getStartDate())
                || changed(request.getEndDate(), current.getEndDate())
                || changedString(request.getCycleType(), current.getCycleType())
                || changed(request.getCycleCount(), current.getCycleCount())
                || changedString(request.getRemark(), current.getRemark())
                || changedString(request.getAttr1(), current.getAttr1())
                || changedString(request.getAttr2(), current.getAttr2())
                || changed(request.getAttr3(), current.getAttr3())
                || changed(request.getAttr4(), current.getAttr4())) {
            throw new ServiceException("已启用计划不允许修改业务字段");
        }
    }

    private boolean changed(Object requested, Object current) {
        return requested != null && !requested.equals(current);
    }

    private boolean changedString(String requested, String current) {
        if (requested == null) return false;
        String normalizedRequest = requested.trim();
        String normalizedCurrent = current == null ? "" : current.trim();
        return !normalizedRequest.equals(normalizedCurrent);
    }
}
