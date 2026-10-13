package com.ktg.mes.wm.service.impl;

import java.util.List;

import com.ktg.common.constant.UserConstants;
import com.ktg.common.utils.DateUtils;
import com.ktg.common.utils.StringUtils;
import com.ktg.mes.wm.domain.tx.ItemRecptTxBean;
import com.ktg.mes.wm.service.InventoryDocumentGuard;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import com.ktg.mes.wm.mapper.WmItemRecptMapper;
import com.ktg.mes.wm.domain.WmItemRecpt;
import com.ktg.mes.wm.service.IWmItemRecptService;

/**
 * 物料入库单Service业务层处理
 * 
 * @author yinjinlu
 * @date 2022-05-22
 */
@Service
public class WmItemRecptServiceImpl implements IWmItemRecptService 
{
    @Autowired
    private WmItemRecptMapper wmItemRecptMapper;
    @Autowired private InventoryDocumentGuard documentGuard;

    /**
     * 查询物料入库单
     * 
     * @param recptId 物料入库单主键
     * @return 物料入库单
     */
    @Override
    public WmItemRecpt selectWmItemRecptByRecptId(Long recptId)
    {
        return wmItemRecptMapper.selectWmItemRecptByRecptId(recptId);
    }

    /**
     * 查询物料入库单列表
     * 
     * @param wmItemRecpt 物料入库单
     * @return 物料入库单
     */
    @Override
    public List<WmItemRecpt> selectWmItemRecptList(WmItemRecpt wmItemRecpt)
    {
        return wmItemRecptMapper.selectWmItemRecptList(wmItemRecpt);
    }

    @Override
    public String checkRecptCodeUnique(WmItemRecpt wmItemRecpt) {
        WmItemRecpt itemRecpt = wmItemRecptMapper.checkRecptCodeUnique(wmItemRecpt);
        Long recptId = wmItemRecpt.getRecptId()==null?-1L:wmItemRecpt.getRecptId();
        if(StringUtils.isNotNull(itemRecpt) && itemRecpt.getRecptId().longValue() != recptId.longValue()){
            return UserConstants.NOT_UNIQUE;
        }
        return UserConstants.UNIQUE;
    }

    /**
     * 新增物料入库单
     * 
     * @param wmItemRecpt 物料入库单
     * @return 结果
     */
    @Override
    public int insertWmItemRecpt(WmItemRecpt wmItemRecpt)
    {
        wmItemRecpt.setCreateTime(DateUtils.getNowDate());
        return wmItemRecptMapper.insertWmItemRecpt(wmItemRecpt);
    }

    /**
     * 修改物料入库单
     * 
     * @param wmItemRecpt 物料入库单
     * @return 结果
     */
    @Override
    @Transactional
    public int updateWmItemRecpt(WmItemRecpt wmItemRecpt)
    {
        documentGuard.lockHeaderUpdate("IR", wmItemRecpt.getRecptId(), wmItemRecpt.getVersion(), wmItemRecpt.getStatus());
        wmItemRecpt.setUpdateTime(DateUtils.getNowDate());
        int changed = wmItemRecptMapper.updateWmItemRecpt(wmItemRecpt);
        if (changed != 1) throw InventoryDocumentGuard.conflict("单据更新失败");
        documentGuard.bumpVersion("IR", wmItemRecpt.getRecptId());
        return changed;
    }

    /**
     * 批量删除物料入库单
     * 
     * @param recptIds 需要删除的物料入库单主键
     * @return 结果
     */
    @Override
    @Transactional
    public int deleteWmItemRecptByRecptIds(Long[] recptIds)
    {
        if (recptIds == null || recptIds.length == 0) throw InventoryDocumentGuard.conflict("单据 ID 无效");
        for (Long id : new java.util.TreeSet<>(java.util.Arrays.asList(recptIds))) documentGuard.requireDraft("IR", id);
        return wmItemRecptMapper.deleteWmItemRecptByRecptIds(recptIds);
    }

    /**
     * 删除物料入库单信息
     * 
     * @param recptId 物料入库单主键
     * @return 结果
     */
    @Override
    @Transactional
    public int deleteWmItemRecptByRecptId(Long recptId)
    {
        documentGuard.requireDraft("IR", recptId);
        return wmItemRecptMapper.deleteWmItemRecptByRecptId(recptId);
    }

    @Override
    public List<ItemRecptTxBean> getTxBeans(Long receptId) {
        return wmItemRecptMapper.getTxBeans(receptId);
    }
}
