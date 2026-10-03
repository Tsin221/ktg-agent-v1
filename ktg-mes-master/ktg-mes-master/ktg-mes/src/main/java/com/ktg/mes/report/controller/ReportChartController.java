package com.ktg.mes.report.controller;

import com.ktg.common.core.domain.AjaxResult;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Collections;

/**
 * 首页图表兼容接口。
 *
 * 当前数据库未安装图表配置表，因此首页返回空图表列表，
 * 保证未启用该可选模块时页面仍可正常加载。
 */
@RestController
@RequestMapping("/mes/report/chart")
public class ReportChartController
{
    @GetMapping("/getMyCharts")
    public AjaxResult getMyCharts()
    {
        return AjaxResult.success(Collections.emptyList());
    }
}
