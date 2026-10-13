package com.ktg.mes.wm.service.impl;

import java.util.List;
import com.ktg.common.utils.DateUtils;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import com.ktg.mes.wm.mapper.WmIssueLineMapper;
import com.ktg.mes.wm.domain.WmIssueLine;
import com.ktg.mes.wm.service.InventoryDocumentGuard;
import org.springframework.transaction.annotation.Transactional;
import com.ktg.mes.wm.service.IWmIssueLineService;

/**
 * 生产领料单行Service业务层处理
 * 
 * @author yinjinlu
 * @date 2022-07-14
 */
@Service
public class WmIssueLineServiceImpl implements IWmIssueLineService 
{
    @Autowired
    private WmIssueLineMapper wmIssueLineMapper;
    @Autowired private InventoryDocumentGuard documentGuard;

    /**
     * 查询生产领料单行
     * 
     * @param lineId 生产领料单行主键
     * @return 生产领料单行
     */
    @Override
    public WmIssueLine selectWmIssueLineByLineId(Long lineId)
    {
        return wmIssueLineMapper.selectWmIssueLineByLineId(lineId);
    }

    /**
     * 查询生产领料单行列表
     * 
     * @param wmIssueLine 生产领料单行
     * @return 生产领料单行
     */
    @Override
    public List<WmIssueLine> selectWmIssueLineList(WmIssueLine wmIssueLine)
    {
        return wmIssueLineMapper.selectWmIssueLineList(wmIssueLine);
    }

    /**
     * 新增生产领料单行
     * 
     * @param wmIssueLine 生产领料单行
     * @return 结果
     */
    @Override
    @Transactional
    public int insertWmIssueLine(WmIssueLine wmIssueLine)
    {
        documentGuard.lockLineMutation("ISSUE", wmIssueLine.getIssueId(), null);
        wmIssueLine.setCreateTime(DateUtils.getNowDate());
        int changed = wmIssueLineMapper.insertWmIssueLine(wmIssueLine);
        documentGuard.bumpVersion("ISSUE", wmIssueLine.getIssueId());
        return changed;
    }

    /**
     * 修改生产领料单行
     * 
     * @param wmIssueLine 生产领料单行
     * @return 结果
     */
    @Override
    @Transactional
    public int updateWmIssueLine(WmIssueLine wmIssueLine)
    {
        Long parentId = documentGuard.lineParent("ISSUE", wmIssueLine.getLineId());
        documentGuard.lockLineMutation("ISSUE", parentId, wmIssueLine.getLineId());
        if (wmIssueLine.getIssueId() != null && !parentId.equals(wmIssueLine.getIssueId()))
            throw InventoryDocumentGuard.conflict("不能更改单据行所属单据");
        wmIssueLine.setUpdateTime(DateUtils.getNowDate());
        int changed = wmIssueLineMapper.updateWmIssueLine(wmIssueLine);
        if (changed != 1) throw InventoryDocumentGuard.conflict("单据行更新失败");
        documentGuard.bumpVersion("ISSUE", parentId);
        return changed;
    }

    /**
     * 批量删除生产领料单行
     * 
     * @param lineIds 需要删除的生产领料单行主键
     * @return 结果
     */
    @Override
    @Transactional
    public int deleteWmIssueLineByLineIds(Long[] lineIds)
    {
        java.util.Set<Long> parents = documentGuard.lockLines("ISSUE", lineIds);
        int changed = wmIssueLineMapper.deleteWmIssueLineByLineIds(lineIds);
        if (changed != lineIds.length) throw InventoryDocumentGuard.conflict("单据行删除冲突");
        for (Long parent : parents) documentGuard.bumpVersion("ISSUE", parent);
        return changed;
    }

    /**
     * 删除生产领料单行信息
     * 
     * @param lineId 生产领料单行主键
     * @return 结果
     */
    @Override
    @Transactional
    public int deleteWmIssueLineByLineId(Long lineId)
    {
        Long parentId = documentGuard.lineParent("ISSUE", lineId);
        documentGuard.lockLineMutation("ISSUE", parentId, lineId);
        int changed = wmIssueLineMapper.deleteWmIssueLineByLineId(lineId);
        if (changed != 1) throw InventoryDocumentGuard.conflict("单据行删除冲突");
        documentGuard.bumpVersion("ISSUE", parentId);
        return changed;
    }

    @Override
    @Transactional
    public int deleteByIssueHeaderId(Long issueId) {
        documentGuard.requireDraft("ISSUE", issueId);
        return wmIssueLineMapper.deleteByIssueHeaderId(issueId);
    }
}
