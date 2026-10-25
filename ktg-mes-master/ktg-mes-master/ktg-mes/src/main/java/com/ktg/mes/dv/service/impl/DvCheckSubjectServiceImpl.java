package com.ktg.mes.dv.service.impl;

import java.util.List;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.TreeSet;

import com.ktg.common.constant.UserConstants;
import com.ktg.common.exception.ServiceException;
import com.ktg.mes.dv.domain.DvCheckPlan;
import com.ktg.mes.dv.mapper.DvCheckPlanMapper;
import com.ktg.common.utils.DateUtils;
import com.ktg.common.utils.StringUtils;
import org.apache.catalina.User;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import com.ktg.mes.dv.mapper.DvCheckSubjectMapper;
import com.ktg.mes.dv.domain.DvCheckSubject;
import com.ktg.mes.dv.service.IDvCheckSubjectService;

/**
 * 点检项目Service业务层处理
 * 
 * @author yinjinlu
 * @date 2022-06-18
 */
@Service
public class DvCheckSubjectServiceImpl implements IDvCheckSubjectService 
{
    @Autowired
    private DvCheckSubjectMapper dvCheckSubjectMapper;

    @Autowired
    private DvCheckPlanMapper dvCheckPlanMapper;

    /**
     * 查询点检项目
     * 
     * @param recordId 点检项目主键
     * @return 点检项目
     */
    @Override
    public DvCheckSubject selectDvCheckSubjectByRecordId(Long recordId)
    {
        return dvCheckSubjectMapper.selectDvCheckSubjectByRecordId(recordId);
    }

    /**
     * 查询点检项目列表
     * 
     * @param dvCheckSubject 点检项目
     * @return 点检项目
     */
    @Override
    public List<DvCheckSubject> selectDvCheckSubjectList(DvCheckSubject dvCheckSubject)
    {
        return dvCheckSubjectMapper.selectDvCheckSubjectList(dvCheckSubject);
    }

    @Override
    public String checkSubjectUnique(DvCheckSubject dvCheckSubject) {
        DvCheckSubject subject = dvCheckSubjectMapper.checkSubjectUnique(dvCheckSubject);
        Long recordId = dvCheckSubject.getRecordId()==null?-1L:dvCheckSubject.getRecordId();
        if(StringUtils.isNotNull(subject) && subject.getRecordId().longValue() != recordId.longValue()){
            return UserConstants.NOT_UNIQUE;
        }

        return UserConstants.UNIQUE;
    }

    /**
     * 新增点检项目
     * 
     * @param dvCheckSubject 点检项目
     * @return 结果
     */
    @Override
    @Transactional(rollbackFor = Exception.class)
    public int insertDvCheckSubject(DvCheckSubject dvCheckSubject)
    {
        dvCheckSubject.setRecordId(null);
        requirePreparePlan(dvCheckSubject.getPlanId());
        if (dvCheckSubjectMapper.checkSubjectUnique(dvCheckSubject) != null) throw new ServiceException("点检项目已经添加过！");
        dvCheckSubject.setCreateTime(DateUtils.getNowDate());
        int inserted = dvCheckSubjectMapper.insertDvCheckSubject(dvCheckSubject);
        if (inserted == 0) {
            throw new ServiceException("项目不存在、已停用或与方案类型不匹配，请刷新后重试");
        }
        return inserted;
    }

    /**
     * 修改点检项目
     * 
     * @param dvCheckSubject 点检项目
     * @return 结果
     */
    @Override
    @Transactional(rollbackFor = Exception.class)
    public int updateDvCheckSubject(DvCheckSubject dvCheckSubject)
    {
        DvCheckSubject current = dvCheckSubjectMapper.selectDvCheckSubjectByRecordId(dvCheckSubject.getRecordId());
        if (current == null) throw new ServiceException("项目关联记录不存在");
        if ((dvCheckSubject.getPlanId() != null && !dvCheckSubject.getPlanId().equals(current.getPlanId()))
                || (dvCheckSubject.getSubjectId() != null && !dvCheckSubject.getSubjectId().equals(current.getSubjectId()))) {
            throw new ServiceException("不允许更改项目关联的计划或源项目");
        }
        requirePreparePlan(current.getPlanId());
        dvCheckSubject.setUpdateTime(DateUtils.getNowDate());
        int updated = dvCheckSubjectMapper.updateDvCheckSubject(dvCheckSubject);
        if (updated != 1) throw new ServiceException("项目关联记录已变化，请刷新后重试");
        return updated;
    }

    /**
     * 批量删除点检项目
     * 
     * @param recordIds 需要删除的点检项目主键
     * @return 结果
     */
    @Override
    @Transactional(rollbackFor = Exception.class)
    public int deleteDvCheckSubjectByRecordIds(Long[] recordIds)
    {
        List<Long> ids = validateIds(recordIds);
        List<DvCheckSubject> records = dvCheckSubjectMapper.selectDvCheckSubjectByRecordIds(recordIds);
        if (records.size() != ids.size()) throw new ServiceException("部分项目关联记录不存在");
        List<Long> planIds = new ArrayList<Long>();
        for (DvCheckSubject record : records) planIds.add(record.getPlanId());
        requirePreparePlans(planIds);
        List<DvCheckSubject> currentRecords = dvCheckSubjectMapper.selectDvCheckSubjectByRecordIds(recordIds);
        if (!samePlanOwnership(records, currentRecords)) throw new ServiceException("项目关联记录已变化，请刷新后重试");
        int deleted = dvCheckSubjectMapper.deleteDvCheckSubjectByRecordIds(ids.toArray(new Long[ids.size()]));
        if (deleted != ids.size()) throw new ServiceException("项目关联记录已变化，请刷新后重试");
        return deleted;
    }

    /**
     * 删除点检项目信息
     * 
     * @param recordId 点检项目主键
     * @return 结果
     */
    @Override
    @Transactional(rollbackFor = Exception.class)
    public int deleteDvCheckSubjectByRecordId(Long recordId)
    {
        return deleteDvCheckSubjectByRecordIds(new Long[] { recordId });
    }

    private void requirePreparePlan(Long planId) {
        if (planId == null) throw new ServiceException("计划编号不能为空");
        DvCheckPlan plan = dvCheckPlanMapper.selectDvCheckPlanForUpdate(planId);
        if (plan == null) throw new ServiceException("目标计划不存在");
        if (!UserConstants.ORDER_STATUS_PREPARE.equals(plan.getStatus())) throw new ServiceException("仅草稿计划允许修改项目关联");
    }

    private void requirePreparePlans(List<Long> planIds) {
        List<Long> sorted = new ArrayList<Long>(new TreeSet<Long>(planIds));
        List<DvCheckPlan> plans = dvCheckPlanMapper.selectDvCheckPlansForUpdate(sorted);
        if (plans.size() != sorted.size()) throw new ServiceException("项目关联的计划不存在");
        for (DvCheckPlan plan : plans) {
            if (!UserConstants.ORDER_STATUS_PREPARE.equals(plan.getStatus())) throw new ServiceException("仅草稿计划允许移除项目关联");
        }
    }

    private List<Long> validateIds(Long[] recordIds) {
        if (recordIds == null || recordIds.length == 0 || Arrays.asList(recordIds).contains(null)) throw new ServiceException("请选择项目关联记录");
        return new ArrayList<Long>(new TreeSet<Long>(Arrays.asList(recordIds)));
    }

    private boolean samePlanOwnership(List<DvCheckSubject> before, List<DvCheckSubject> after) {
        if (before.size() != after.size()) return false;
        for (int i = 0; i < before.size(); i++) {
            if (!before.get(i).getRecordId().equals(after.get(i).getRecordId())
                    || !before.get(i).getPlanId().equals(after.get(i).getPlanId())) return false;
        }
        return true;
    }
}
