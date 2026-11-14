package com.ktg.mes.wm.controller;

import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import javax.servlet.http.HttpServletResponse;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import com.ktg.common.annotation.Log;
import com.ktg.common.core.controller.BaseController;
import com.ktg.common.core.domain.AjaxResult;
import com.ktg.common.enums.BusinessType;
import com.ktg.mes.wm.domain.WmMaterialStock;
import com.ktg.mes.wm.domain.WmStockSummaryGroup;
import com.ktg.mes.wm.service.IWmMaterialStockService;
import com.ktg.common.utils.poi.ExcelUtil;
import com.ktg.common.core.page.TableDataInfo;

/**
 * 库存记录Controller
 * 
 * @author yinjinlu
 * @date 2022-05-30
 */
@RestController
@RequestMapping("/mes/wm/wmstock")
public class WmMaterialStockController extends BaseController
{
    @Autowired
    private IWmMaterialStockService wmMaterialStockService;

    /**
     * 查询库存记录列表
     */
    @PreAuthorize("@ss.hasPermi('mes:wm:wmstock:list')")
    @GetMapping("/list")
    public TableDataInfo list(WmMaterialStock wmMaterialStock)
    {
        startPage();
        List<WmMaterialStock> list = wmMaterialStockService.selectWmMaterialStockList(wmMaterialStock);
        return getDataTable(list);
    }

    @PreAuthorize("@ss.hasPermi('mes:wm:wmstock:list')")
    @GetMapping("/summary")
    public AjaxResult summary(@RequestParam(value = "itemCode", required = false) String itemCode,
                              @RequestParam(value = "warehouseId", required = false) String warehouse)
    {
        String code = itemCode == null ? "" : itemCode.trim();
        if (code.isEmpty() || code.length() > 64)
        {
            return AjaxResult.error(400, "请提供完整物料编码（1 至 64 个字符）");
        }
        Long warehouseId = null;
        if (warehouse != null)
        {
            try
            {
                warehouseId = Long.valueOf(warehouse);
                if (warehouseId <= 0)
                {
                    return AjaxResult.error(400, "仓库 ID 必须为正整数");
                }
            }
            catch (NumberFormatException ex)
            {
                return AjaxResult.error(400, "仓库 ID 必须为正整数");
            }
        }

        Instant queriedAt = Instant.now();
        List<WmStockSummaryGroup> groups = wmMaterialStockService.selectStockSummary(code, warehouseId);
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("itemCode", code);
        result.put("warehouseId", warehouseId);
        result.put("queriedAt", queriedAt.toString());
        result.put("hasRecords", !groups.isEmpty());
        result.put("groups", groups);
        return AjaxResult.success(result);
    }

    /**
     * 导出库存记录列表
     */
    @PreAuthorize("@ss.hasPermi('mes:wm:wmstock:export')")
    @Log(title = "库存记录", businessType = BusinessType.EXPORT)
    @PostMapping("/export")
    public void export(HttpServletResponse response, WmMaterialStock wmMaterialStock)
    {
        List<WmMaterialStock> list = wmMaterialStockService.selectWmMaterialStockList(wmMaterialStock);
        ExcelUtil<WmMaterialStock> util = new ExcelUtil<WmMaterialStock>(WmMaterialStock.class);
        util.exportExcel(response, list, "库存记录数据");
    }

    /**
     * 获取库存记录详细信息
     */
    @PreAuthorize("@ss.hasPermi('mes:wm:wmstock:query')")
    @GetMapping(value = "/{materialStockId}")
    public AjaxResult getInfo(@PathVariable("materialStockId") Long materialStockId)
    {
        return AjaxResult.success(wmMaterialStockService.selectWmMaterialStockByMaterialStockId(materialStockId));
    }

    /**
     * 新增库存记录
     */
    @PreAuthorize("@ss.hasPermi('mes:wm:wmstock:add')")
    @Log(title = "库存记录", businessType = BusinessType.INSERT)
    @PostMapping
    public AjaxResult add(@RequestBody WmMaterialStock wmMaterialStock)
    {
        return toAjax(wmMaterialStockService.insertWmMaterialStock(wmMaterialStock));
    }

    /**
     * 修改库存记录
     */
    @PreAuthorize("@ss.hasPermi('mes:wm:wmstock:edit')")
    @Log(title = "库存记录", businessType = BusinessType.UPDATE)
    @PutMapping
    public AjaxResult edit(@RequestBody WmMaterialStock wmMaterialStock)
    {
        return toAjax(wmMaterialStockService.updateWmMaterialStock(wmMaterialStock));
    }

    /**
     * 删除库存记录
     */
    @PreAuthorize("@ss.hasPermi('mes:wm:wmstock:remove')")
    @Log(title = "库存记录", businessType = BusinessType.DELETE)
	@DeleteMapping("/{materialStockIds}")
    public AjaxResult remove(@PathVariable Long[] materialStockIds)
    {
        return toAjax(wmMaterialStockService.deleteWmMaterialStockByMaterialStockIds(materialStockIds));
    }
}
