package com.ktg.mes.dv.service.impl;

import java.util.List;
import java.util.Arrays;
import java.util.TreeSet;
import java.util.Map;
import java.util.HashMap;
import com.ktg.common.constant.UserConstants;
import com.ktg.common.exception.ServiceException;
import com.ktg.mes.dv.domain.DvRepair;
import com.ktg.mes.dv.mapper.DvRepairMapper;
import com.ktg.common.utils.DateUtils;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import com.ktg.mes.dv.mapper.DvRepairLineMapper;
import com.ktg.mes.dv.domain.DvRepairLine;
import com.ktg.mes.dv.service.IDvRepairLineService;

/**
 * 设备维修单行Service业务层处理
 * 
 * @author yinjinlu
 * @date 2022-08-08
 */
@Service
public class DvRepairLineServiceImpl implements IDvRepairLineService 
{
    @Autowired
    private DvRepairLineMapper dvRepairLineMapper;

    @Autowired
    private DvRepairMapper dvRepairMapper;

    /**
     * 查询设备维修单行
     * 
     * @param lineId 设备维修单行主键
     * @return 设备维修单行
     */
    @Override
    public DvRepairLine selectDvRepairLineByLineId(Long lineId)
    {
        return dvRepairLineMapper.selectDvRepairLineByLineId(lineId);
    }

    /**
     * 查询设备维修单行列表
     * 
     * @param dvRepairLine 设备维修单行
     * @return 设备维修单行
     */
    @Override
    public List<DvRepairLine> selectDvRepairLineList(DvRepairLine dvRepairLine)
    {
        return dvRepairLineMapper.selectDvRepairLineList(dvRepairLine);
    }

    /**
     * 新增设备维修单行
     * 
     * @param dvRepairLine 设备维修单行
     * @return 结果
     */
    @Override
    @Transactional(rollbackFor = Exception.class)
    public int insertDvRepairLine(DvRepairLine dvRepairLine)
    {
        requireDraft(dvRepairLine.getRepairId());
        dvRepairLine.setLineId(null);
        dvRepairLine.setCreateTime(DateUtils.getNowDate());
        return dvRepairLineMapper.insertDvRepairLine(dvRepairLine);
    }

    /**
     * 修改设备维修单行
     * 
     * @param dvRepairLine 设备维修单行
     * @return 结果
     */
    @Override
    @Transactional(rollbackFor = Exception.class)
    public int updateDvRepairLine(DvRepairLine dvRepairLine)
    {
        if (dvRepairLine.getLineId() == null) throw new ServiceException("维修内容ID不能为空");
        DvRepairLine existing = dvRepairLineMapper.selectDvRepairLineByLineId(dvRepairLine.getLineId());
        if (existing == null) throw new ServiceException("维修内容不存在，请刷新后重试");
        requireDraft(existing.getRepairId());
        DvRepairLine locked = dvRepairLineMapper.selectDvRepairLineByLineId(dvRepairLine.getLineId());
        if (locked == null || !existing.getRepairId().equals(locked.getRepairId())) {
            throw new ServiceException("维修内容归属已变化，请刷新后重试");
        }
        if (dvRepairLine.getRepairId() != null && !locked.getRepairId().equals(dvRepairLine.getRepairId())) {
            throw new ServiceException("维修内容不能转移到其他维修单");
        }
        dvRepairLine.setRepairId(null);
        dvRepairLine.setUpdateTime(DateUtils.getNowDate());
        int updated = dvRepairLineMapper.updateDvRepairLine(dvRepairLine);
        if (updated != 1) throw new ServiceException("维修内容修改数量异常，已回滚");
        return updated;
    }

    /**
     * 批量删除设备维修单行
     * 
     * @param lineIds 需要删除的设备维修单行主键
     * @return 结果
     */
    @Override
    @Transactional(rollbackFor = Exception.class)
    public int deleteDvRepairLineByLineIds(Long[] lineIds)
    {
        if (lineIds == null || lineIds.length == 0 || Arrays.asList(lineIds).contains(null)) {
            throw new ServiceException("请选择维修内容");
        }
        TreeSet<Long> ids = new TreeSet<>(Arrays.asList(lineIds));
        TreeSet<Long> parentIds = new TreeSet<>();
        Map<Long, Long> originalParents = new HashMap<>();
        for (Long id : ids) {
            DvRepairLine line = dvRepairLineMapper.selectDvRepairLineByLineId(id);
            if (line == null) throw new ServiceException("维修内容不存在，请刷新后重试");
            parentIds.add(line.getRepairId());
            originalParents.put(id, line.getRepairId());
        }
        for (Long parentId : parentIds) requireDraft(parentId);
        for (Long id : ids) {
            DvRepairLine line = dvRepairLineMapper.selectDvRepairLineByLineId(id);
            if (line == null || !originalParents.get(id).equals(line.getRepairId())) {
                throw new ServiceException("维修内容归属已变化，请刷新后重试");
            }
        }
        int removed = dvRepairLineMapper.deleteDvRepairLineByLineIds(ids.toArray(new Long[0]));
        if (removed != ids.size()) throw new ServiceException("维修内容删除数量异常，已回滚");
        return removed;
    }

    /**
     * 删除设备维修单行信息
     * 
     * @param lineId 设备维修单行主键
     * @return 结果
     */
    @Override
    @Transactional(rollbackFor = Exception.class)
    public int deleteDvRepairLineByLineId(Long lineId)
    {
        return deleteDvRepairLineByLineIds(new Long[] { lineId });
    }

    @Override
    @Transactional(rollbackFor = Exception.class)
    public int deleteByRepairId(Long repairId) {
        requireDraft(repairId);
        return dvRepairLineMapper.deleteByRepairId(repairId);
    }

    private void requireDraft(Long repairId) {
        if (repairId == null) throw new ServiceException("维修单ID不能为空");
        DvRepair repair = dvRepairMapper.selectDvRepairForUpdate(repairId);
        if (repair == null) throw new ServiceException("维修单不存在，请刷新后重试");
        if (!UserConstants.ORDER_STATUS_PREPARE.equals(repair.getStatus())) {
            throw new ServiceException("只能维护草稿维修单的维修内容");
        }
    }
}
