package com.ktg.system.mapper;

import com.ktg.common.core.domain.entity.SysAutoCodeRule;

import java.util.List;

public interface SysAutoCodeRuleMapper {
    @org.apache.ibatis.annotations.Select("select rule_id from sys_auto_code_rule where rule_code=#{ruleCode} for update")
    Long lockRule(@org.apache.ibatis.annotations.Param("ruleCode") String ruleCode);

    public List<SysAutoCodeRule> selectSysAutoCodeResultList(SysAutoCodeRule sysAutoCodeRule);

    public SysAutoCodeRule findById(Long ruleId);

    public int add(SysAutoCodeRule sysAutoCodeRule);

    public int updateSysAutoCodeRule(SysAutoCodeRule sysAutoCodeRule);

    public int deleteById(Long ruleId);

    public SysAutoCodeRule checkRuleCodeUnique(String ruleCode);

    public SysAutoCodeRule checkRuleNameUnique(String ruleName);
}
