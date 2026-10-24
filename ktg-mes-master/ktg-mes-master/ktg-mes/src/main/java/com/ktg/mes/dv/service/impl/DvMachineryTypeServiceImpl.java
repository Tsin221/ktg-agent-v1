package com.ktg.mes.dv.service.impl;

import java.util.List;

import com.ktg.common.core.domain.entity.ItemType;
import com.ktg.common.exception.ServiceException;
import com.ktg.common.utils.DateUtils;
import com.ktg.common.utils.StringUtils;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import com.ktg.mes.dv.mapper.DvMachineryTypeMapper;
import com.ktg.mes.dv.domain.DvMachineryType;
import com.ktg.mes.dv.service.IDvMachineryTypeService;

/**
 * 设备类型Service业务层处理
 * 
 * @author yinjinlu
 * @date 2022-05-08
 */
@Service
public class DvMachineryTypeServiceImpl implements IDvMachineryTypeService 
{
    @Autowired
    private DvMachineryTypeMapper dvMachineryTypeMapper;

    /**
     * 查询设备类型
     * 
     * @param machineryTypeId 设备类型主键
     * @return 设备类型
     */
    @Override
    public DvMachineryType selectDvMachineryTypeByMachineryTypeId(Long machineryTypeId)
    {
        return dvMachineryTypeMapper.selectDvMachineryTypeByMachineryTypeId(machineryTypeId);
    }

    /**
     * 查询设备类型列表
     * 
     * @param dvMachineryType 设备类型
     * @return 设备类型
     */
    @Override
    public List<DvMachineryType> selectDvMachineryTypeList(DvMachineryType dvMachineryType)
    {
        return dvMachineryTypeMapper.selectDvMachineryTypeList(dvMachineryType);
    }

    /**
     * 校验同一父类型下名称不重复（编辑时排除自身）
     *
     * @param dvMachineryType 待保存的设备类型
     */
    private void checkNameUnique(DvMachineryType dvMachineryType)
    {
        Long parentTypeId = dvMachineryType.getParentTypeId() == null ? 0L : dvMachineryType.getParentTypeId();
        String typeName = dvMachineryType.getMachineryTypeName().trim();
        DvMachineryType query = new DvMachineryType();
        query.setParentTypeId(parentTypeId);
        query.setMachineryTypeName(typeName);
        List<DvMachineryType> siblings = dvMachineryTypeMapper.selectDvMachineryTypeList(query);
        for (DvMachineryType sibling : siblings) {
            if (sibling.getMachineryTypeId().equals(dvMachineryType.getMachineryTypeId())) {
                continue;
            }
            String siblingName = sibling.getMachineryTypeName() == null ? "" : sibling.getMachineryTypeName().trim();
            if (typeName.equals(siblingName)) {
                throw new ServiceException("同级下已存在名称为「" + typeName + "」的设备类型，请换一个名称");
            }
        }
    }

    /**
     * 新增设备类型
     * 
     * @param dvMachineryType 设备类型
     * @return 结果
     */
    @Override
    public int insertDvMachineryType(DvMachineryType dvMachineryType)
    {
        if (StringUtils.isEmpty(dvMachineryType.getMachineryTypeName())) {
            throw new ServiceException("设备类型名称不能为空");
        }
        if (dvMachineryType.getParentTypeId() == null) {
            dvMachineryType.setParentTypeId(0L);
        }
        if (dvMachineryType.getParentTypeId() == 0L) {
            // 根分类：ancestors 为 NOT NULL 且无默认值，必须显式写入根祖先
            dvMachineryType.setAncestors("0");
        } else {
            DvMachineryType parent = dvMachineryTypeMapper.selectDvMachineryTypeByMachineryTypeId(dvMachineryType.getParentTypeId());
            if (parent == null) {
                throw new ServiceException("父类型不存在，无法新增设备类型");
            }
            dvMachineryType.setAncestors(parent.getAncestors() + "," + parent.getMachineryTypeId());
        }
        checkNameUnique(dvMachineryType);
        dvMachineryType.setCreateTime(DateUtils.getNowDate());
        return dvMachineryTypeMapper.insertDvMachineryType(dvMachineryType);
    }

    /**
     * 修改设备类型
     * 
     * @param dvMachineryType 设备类型
     * @return 结果
     */
    @Override
    public int updateDvMachineryType(DvMachineryType dvMachineryType)
    {
        DvMachineryType current = dvMachineryTypeMapper.selectDvMachineryTypeByMachineryTypeId(dvMachineryType.getMachineryTypeId());
        if (current == null) {
            throw new ServiceException("设备类型不存在，无法修改");
        }
        // 编辑不允许修改父类型与祖先链：父类型一变，设备按分类递归筛选就会失真，甚至成环
        dvMachineryType.setParentTypeId(current.getParentTypeId());
        dvMachineryType.setAncestors(current.getAncestors());
        if (StringUtils.isEmpty(dvMachineryType.getMachineryTypeName())) {
            dvMachineryType.setMachineryTypeName(current.getMachineryTypeName());
        }
        checkNameUnique(dvMachineryType);
        dvMachineryType.setUpdateTime(DateUtils.getNowDate());
        return dvMachineryTypeMapper.updateDvMachineryType(dvMachineryType);
    }

    /**
     * 批量删除设备类型
     * 
     * @param machineryTypeIds 需要删除的设备类型主键
     * @return 结果
     */
    @Override
    public int deleteDvMachineryTypeByMachineryTypeIds(Long[] machineryTypeIds)
    {
        return dvMachineryTypeMapper.deleteDvMachineryTypeByMachineryTypeIds(machineryTypeIds);
    }

    /**
     * 删除设备类型信息
     * 
     * @param machineryTypeId 设备类型主键
     * @return 结果
     */
    @Override
    public int deleteDvMachineryTypeByMachineryTypeId(Long machineryTypeId)
    {
        return dvMachineryTypeMapper.deleteDvMachineryTypeByMachineryTypeId(machineryTypeId);
    }
}
