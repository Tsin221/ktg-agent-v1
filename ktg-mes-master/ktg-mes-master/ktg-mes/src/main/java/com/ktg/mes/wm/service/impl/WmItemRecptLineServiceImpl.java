package com.ktg.mes.wm.service.impl;

import java.util.List;
import com.ktg.common.utils.DateUtils;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import com.ktg.mes.wm.mapper.WmItemRecptLineMapper;
import com.ktg.mes.wm.domain.WmItemRecptLine;
import com.ktg.mes.wm.service.InventoryDocumentGuard;
import org.springframework.transaction.annotation.Transactional;
import com.ktg.mes.wm.service.IWmItemRecptLineService;

/**
 * 物料入库单行Service业务层处理
 * 
 * @author yinjinlu
 * @date 2022-05-22
 */
@Service
public class WmItemRecptLineServiceImpl implements IWmItemRecptLineService 
{
    @Autowired
    private WmItemRecptLineMapper wmItemRecptLineMapper;
    @Autowired private InventoryDocumentGuard documentGuard;

    /**
     * 查询物料入库单行
     * 
     * @param lineId 物料入库单行主键
     * @return 物料入库单行
     */
    @Override
    public WmItemRecptLine selectWmItemRecptLineByLineId(Long lineId)
    {
        return wmItemRecptLineMapper.selectWmItemRecptLineByLineId(lineId);
    }

    /**
     * 查询物料入库单行列表
     * 
     * @param wmItemRecptLine 物料入库单行
     * @return 物料入库单行
     */
    @Override
    public List<WmItemRecptLine> selectWmItemRecptLineList(WmItemRecptLine wmItemRecptLine)
    {
        return wmItemRecptLineMapper.selectWmItemRecptLineList(wmItemRecptLine);
    }

    /**
     * 新增物料入库单行
     * 
     * @param wmItemRecptLine 物料入库单行
     * @return 结果
     */
    @Override
    @Transactional
    public int insertWmItemRecptLine(WmItemRecptLine wmItemRecptLine)
    {
        documentGuard.lockLineMutation("IR", wmItemRecptLine.getRecptId(), null);
        if (!"PREPARE".equals(documentGuard.lockedDocumentStatus("IR", wmItemRecptLine.getRecptId())))
            throw InventoryDocumentGuard.conflict("只能在草稿阶段新增入库明细");
        wmItemRecptLine.setCreateTime(DateUtils.getNowDate());
        int changed = wmItemRecptLineMapper.insertWmItemRecptLine(wmItemRecptLine);
        documentGuard.bumpVersion("IR", wmItemRecptLine.getRecptId());
        return changed;
    }

    /**
     * 修改物料入库单行
     * 
     * @param wmItemRecptLine 物料入库单行
     * @return 结果
     */
    @Override
    @Transactional
    public int updateWmItemRecptLine(WmItemRecptLine wmItemRecptLine)
    {
        Long parentId = documentGuard.lineParent("IR", wmItemRecptLine.getLineId());
        documentGuard.lockLineMutation("IR", parentId, wmItemRecptLine.getLineId());
        if (!"PREPARE".equals(documentGuard.lockedDocumentStatus("IR", parentId)))
            throw InventoryDocumentGuard.conflict("只能在草稿阶段编辑入库明细");
        if (wmItemRecptLine.getRecptId() != null && !parentId.equals(wmItemRecptLine.getRecptId()))
            throw InventoryDocumentGuard.conflict("不能更改单据行所属单据");
        wmItemRecptLine.setUpdateTime(DateUtils.getNowDate());
        int changed = wmItemRecptLineMapper.updateWmItemRecptLine(wmItemRecptLine);
        if (changed != 1) throw InventoryDocumentGuard.conflict("单据行更新失败");
        documentGuard.bumpVersion("IR", parentId);
        return changed;
    }

    @Override
    @Transactional
    public int updateReceiptLinePutaway(WmItemRecptLine wmItemRecptLine) {
        Long parentId = documentGuard.lineParent("IR", wmItemRecptLine.getLineId());
        documentGuard.lockLineMutation("IR", parentId, wmItemRecptLine.getLineId());
        if (!"APPROVING".equals(documentGuard.lockedDocumentStatus("IR", parentId)))
            throw InventoryDocumentGuard.conflict("只能在执行上架阶段修改目标仓储位置");
        WmItemRecptLine update = new WmItemRecptLine();
        update.setLineId(wmItemRecptLine.getLineId());
        update.setBatchCode(wmItemRecptLine.getBatchCode());
        update.setWarehouseId(wmItemRecptLine.getWarehouseId());
        update.setWarehouseCode(wmItemRecptLine.getWarehouseCode());
        update.setWarehouseName(wmItemRecptLine.getWarehouseName());
        update.setLocationId(wmItemRecptLine.getLocationId());
        update.setLocationCode(wmItemRecptLine.getLocationCode());
        update.setLocationName(wmItemRecptLine.getLocationName());
        update.setAreaId(wmItemRecptLine.getAreaId());
        update.setAreaCode(wmItemRecptLine.getAreaCode());
        update.setAreaName(wmItemRecptLine.getAreaName());
        update.setUpdateTime(DateUtils.getNowDate());
        int changed = wmItemRecptLineMapper.updateWmItemRecptLine(update);
        if (changed != 1) throw InventoryDocumentGuard.conflict("目标仓储位置更新失败");
        documentGuard.bumpVersion("IR", parentId);
        return changed;
    }

    /**
     * 批量删除物料入库单行
     * 
     * @param lineIds 需要删除的物料入库单行主键
     * @return 结果
     */
    @Override
    @Transactional
    public int deleteWmItemRecptLineByLineIds(Long[] lineIds)
    {
        java.util.Set<Long> parents = documentGuard.lockLines("IR", lineIds);
        for (Long parent : parents) {
            if (!"PREPARE".equals(documentGuard.lockedDocumentStatus("IR", parent)))
                throw InventoryDocumentGuard.conflict("只能在草稿阶段删除入库明细");
        }
        int changed = wmItemRecptLineMapper.deleteWmItemRecptLineByLineIds(lineIds);
        if (changed != lineIds.length) throw InventoryDocumentGuard.conflict("单据行删除冲突");
        for (Long parent : parents) documentGuard.bumpVersion("IR", parent);
        return changed;
    }

    /**
     * 删除物料入库单行信息
     * 
     * @param lineId 物料入库单行主键
     * @return 结果
     */
    @Override
    @Transactional
    public int deleteWmItemRecptLineByLineId(Long lineId)
    {
        Long parentId = documentGuard.lineParent("IR", lineId);
        documentGuard.lockLineMutation("IR", parentId, lineId);
        if (!"PREPARE".equals(documentGuard.lockedDocumentStatus("IR", parentId)))
            throw InventoryDocumentGuard.conflict("只能在草稿阶段删除入库明细");
        int changed = wmItemRecptLineMapper.deleteWmItemRecptLineByLineId(lineId);
        if (changed != 1) throw InventoryDocumentGuard.conflict("单据行删除冲突");
        documentGuard.bumpVersion("IR", parentId);
        return changed;
    }

    @Override
    @Transactional
    public int deleteByRecptId(Long recptId) {
        documentGuard.requireDraft("IR", recptId);
        return wmItemRecptLineMapper.deleteByRecptId(recptId);
    }
}
