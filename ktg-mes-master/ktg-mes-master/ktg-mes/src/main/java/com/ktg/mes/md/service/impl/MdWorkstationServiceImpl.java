package com.ktg.mes.md.service.impl;

import java.util.List;
import java.util.Arrays;
import java.util.Collections;
import java.util.Objects;
import java.util.TreeSet;

import com.ktg.common.constant.UserConstants;
import com.ktg.common.exception.ServiceException;
import com.ktg.common.utils.DateUtils;
import com.ktg.common.utils.StringUtils;
import com.ktg.mes.pro.execution.ExecutionOwnershipService;
import org.apache.catalina.User;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import com.ktg.mes.md.mapper.MdWorkstationMapper;
import com.ktg.mes.md.domain.MdWorkstation;
import com.ktg.mes.md.service.IMdWorkstationService;

/**
 * 工作站Service业务层处理
 * 
 * @author yinjinlu
 * @date 2022-05-10
 */
@Service
public class MdWorkstationServiceImpl implements IMdWorkstationService 
{
    @Autowired
    private MdWorkstationMapper mdWorkstationMapper;

    @Autowired
    private ExecutionOwnershipService executionOwnershipService;

    /**
     * 查询工作站
     * 
     * @param workstationId 工作站主键
     * @return 工作站
     */
    @Override
    public MdWorkstation selectMdWorkstationByWorkstationId(Long workstationId)
    {
        return mdWorkstationMapper.selectMdWorkstationByWorkstationId(workstationId);
    }

    /**
     * 查询工作站列表
     * 
     * @param mdWorkstation 工作站
     * @return 工作站
     */
    @Override
    public List<MdWorkstation> selectMdWorkstationList(MdWorkstation mdWorkstation)
    {
        return mdWorkstationMapper.selectMdWorkstationList(mdWorkstation);
    }

    @Override
    public String checkWorkStationCodeUnique(MdWorkstation mdWorkstation) {
        MdWorkstation workstation = mdWorkstationMapper.checkWorkStationCodeUnique(mdWorkstation);
        Long workstationId = mdWorkstation.getWorkstationId()==null? -1L:mdWorkstation.getWorkstationId();
        if(StringUtils.isNotNull(workstation) && workstation.getWorkstationId().longValue() != workstationId.longValue()){
            return UserConstants.NOT_UNIQUE;
        }
        return UserConstants.UNIQUE;
    }

    @Override
    public String checkWorkStationNameUnique(MdWorkstation mdWorkstation) {
        MdWorkstation workstation = mdWorkstationMapper.checkWorkStationNameUnique(mdWorkstation);
        Long workstationId = mdWorkstation.getWorkstationId()==null? -1L:mdWorkstation.getWorkstationId();
        if(StringUtils.isNotNull(workstation) && workstation.getWorkstationId().longValue() != workstationId.longValue()){
            return UserConstants.NOT_UNIQUE;
        }
        return UserConstants.UNIQUE;
    }

    /**
     * 新增工作站
     * 
     * @param mdWorkstation 工作站
     * @return 结果
     */
    @Override
    public int insertMdWorkstation(MdWorkstation mdWorkstation)
    {
        mdWorkstation.setCreateTime(DateUtils.getNowDate());
        return mdWorkstationMapper.insertMdWorkstation(mdWorkstation);
    }

    /**
     * 修改工作站
     * 
     * @param mdWorkstation 工作站
     * @return 结果
     */
    @Override
    @Transactional
    public int updateMdWorkstation(MdWorkstation mdWorkstation)
    {
        MdWorkstation current=mdWorkstationMapper.lockMdWorkstationById(mdWorkstation.getWorkstationId());
        if(current!=null) guardExecutionChange(current,mdWorkstation);
        mdWorkstation.setUpdateTime(DateUtils.getNowDate());
        return mdWorkstationMapper.updateMdWorkstation(mdWorkstation);
    }

    /**
     * 批量删除工作站
     * 
     * @param workstationIds 需要删除的工作站主键
     * @return 结果
     */
    @Override
    @Transactional
    public int deleteMdWorkstationByWorkstationIds(Long[] workstationIds)
    {
        for(Long id:new TreeSet<>(Arrays.asList(workstationIds))) guardExecutionDeletion(id);
        return mdWorkstationMapper.deleteMdWorkstationByWorkstationIds(workstationIds);
    }

    /**
     * 删除工作站信息
     * 
     * @param workstationId 工作站主键
     * @return 结果
     */
    @Override
    @Transactional
    public int deleteMdWorkstationByWorkstationId(Long workstationId)
    {
        guardExecutionDeletion(workstationId);
        return mdWorkstationMapper.deleteMdWorkstationByWorkstationId(workstationId);
    }

    private void guardExecutionChange(MdWorkstation current,MdWorkstation changes) {
        // Match the dynamic UPDATE semantics: null fields and empty enable flag mean no change.
        String enable=changes.getEnableFlag();
        boolean disables=enable!=null&&!enable.isEmpty()&&!"Y".equals(enable)&&!Objects.equals(enable,current.getEnableFlag());
        boolean processChange=changed(changes.getProcessId(),current.getProcessId());
        boolean storageChange=changed(changes.getWarehouseId(),current.getWarehouseId())
                || changed(changes.getLocationId(),current.getLocationId()) || changed(changes.getAreaId(),current.getAreaId());
        if(!disables&&!processChange&&!storageChange) return;
        List<Long> processes=activeExecutionProcesses(current.getWorkstationId());
        if(!processes.isEmpty() && (disables || storageChange ||
                (processChange && processes.stream().anyMatch(required->!Objects.equals(required,changes.getProcessId()))))) {
            throw executionStationInUse();
        }
    }

    private boolean changed(Long proposed,Long current) {
        return proposed!=null&&!Objects.equals(proposed,current);
    }

    private void guardExecutionDeletion(Long workstationId) {
        // Release locks this station before creating task references. Keep the same lock until commit.
        mdWorkstationMapper.lockMdWorkstationById(workstationId);
        if(!activeExecutionProcesses(workstationId).isEmpty()) throw executionStationInUse();
    }

    private List<Long> activeExecutionProcesses(Long workstationId) {
        // Match legacy write guards: only the wholly pre-execution schema is exempt.
        // A partially migrated or inaccessible execution schema must fail closed.
        return executionOwnershipService.hasExecutionSchema()
                ? mdWorkstationMapper.lockActiveExecutionProcessIds(workstationId) : Collections.emptyList();
    }

    private ServiceException executionStationInUse() {
        return new ServiceException("工作站仍被未完结的生产执行工单引用，暂不能停用、删除、变更工序或线边库；请先完成相关工单。");
    }
}
