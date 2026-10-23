package com.ktg.mes.wm.controller;

import java.util.List;
import javax.servlet.http.HttpServletResponse;

import com.ktg.common.utils.StringUtils;
import com.ktg.mes.wm.domain.WmStorageArea;
import com.ktg.mes.wm.domain.WmStorageLocation;
import com.ktg.mes.wm.domain.WmWarehouse;
import com.ktg.mes.wm.service.IWmStorageAreaService;
import com.ktg.mes.wm.service.IWmStorageLocationService;
import com.ktg.mes.wm.service.IWmWarehouseService;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import com.ktg.common.annotation.Log;
import com.ktg.common.core.controller.BaseController;
import com.ktg.common.core.domain.AjaxResult;
import com.ktg.common.enums.BusinessType;
import com.ktg.mes.wm.domain.WmItemRecptLine;
import com.ktg.mes.wm.service.IWmItemRecptLineService;
import com.ktg.common.utils.poi.ExcelUtil;
import com.ktg.common.core.page.TableDataInfo;

/**
 * 物料入库单行Controller
 * 
 * @author yinjinlu
 * @date 2022-05-22
 */
@RestController
@RequestMapping("/mes/wm/itemrecptline")
public class WmItemRecptLineController extends BaseController
{
    @Autowired
    private IWmItemRecptLineService wmItemRecptLineService;


    @Autowired
    private IWmWarehouseService wmWarehouseService;

    @Autowired
    private IWmStorageLocationService wmStorageLocationService;

    @Autowired
    private IWmStorageAreaService wmStorageAreaService;

    /**
     * 查询物料入库单行列表
     */
    @PreAuthorize("@ss.hasPermi('mes:wm:itemrecptline:list')")
    @GetMapping("/list")
    public TableDataInfo list(WmItemRecptLine wmItemRecptLine)
    {
        startPage();
        List<WmItemRecptLine> list = wmItemRecptLineService.selectWmItemRecptLineList(wmItemRecptLine);
        return getDataTable(list);
    }

    /**
     * 导出物料入库单行列表
     */
    @PreAuthorize("@ss.hasPermi('mes:wm:itemrecptline:export')")
    @Log(title = "物料入库单行", businessType = BusinessType.EXPORT)
    @PostMapping("/export")
    public void export(HttpServletResponse response, WmItemRecptLine wmItemRecptLine)
    {
        List<WmItemRecptLine> list = wmItemRecptLineService.selectWmItemRecptLineList(wmItemRecptLine);
        ExcelUtil<WmItemRecptLine> util = new ExcelUtil<WmItemRecptLine>(WmItemRecptLine.class);
        util.exportExcel(response, list, "物料入库单行数据");
    }

    /**
     * 获取物料入库单行详细信息
     */
    @PreAuthorize("@ss.hasPermi('mes:wm:itemrecptline:query')")
    @GetMapping(value = "/{lineId}")
    public AjaxResult getInfo(@PathVariable("lineId") Long lineId)
    {
        return AjaxResult.success(wmItemRecptLineService.selectWmItemRecptLineByLineId(lineId));
    }

    /**
     * 新增物料入库单行
     */
    @PreAuthorize("@ss.hasPermi('mes:wm:itemrecptline:add')")
    @Log(title = "物料入库单行", businessType = BusinessType.INSERT)
    @PostMapping
    public AjaxResult add(@RequestBody WmItemRecptLine wmItemRecptLine)
    {
        if (StringUtils.isEmpty(wmItemRecptLine.getBatchCode())) return AjaxResult.error(422, "请输入批次号");
        wmItemRecptLine.setBatchCode(wmItemRecptLine.getBatchCode().trim());
        if (wmItemRecptLine.getBatchCode().length() > 255) return AjaxResult.error(422, "批次号长度不能超过255个字符");
        if(StringUtils.isNotNull(wmItemRecptLine.getWarehouseId())){
            WmWarehouse warehouse = wmWarehouseService.selectWmWarehouseByWarehouseId(wmItemRecptLine.getWarehouseId());
            wmItemRecptLine.setWarehouseCode(warehouse.getWarehouseCode());
            wmItemRecptLine.setWarehouseName(warehouse.getWarehouseName());
        }
        if(StringUtils.isNotNull(wmItemRecptLine.getLocationId())){
            WmStorageLocation location = wmStorageLocationService.selectWmStorageLocationByLocationId(wmItemRecptLine.getLocationId());
            wmItemRecptLine.setLocationCode(location.getLocationCode());
            wmItemRecptLine.setLocationName(location.getLocationName());
        }
        if(StringUtils.isNotNull(wmItemRecptLine.getAreaId())){
            WmStorageArea area = wmStorageAreaService.selectWmStorageAreaByAreaId(wmItemRecptLine.getAreaId());
            wmItemRecptLine.setAreaCode(area.getAreaCode());
            wmItemRecptLine.setAreaName(area.getAreaName());
        }
        return toAjax(wmItemRecptLineService.insertWmItemRecptLine(wmItemRecptLine));
    }

    /**
     * 修改物料入库单行
     */
    @PreAuthorize("@ss.hasPermi('mes:wm:itemrecptline:edit')")
    @Log(title = "物料入库单行", businessType = BusinessType.UPDATE)
    @PutMapping
    public AjaxResult edit(@RequestBody WmItemRecptLine wmItemRecptLine)
    {
        if (StringUtils.isEmpty(wmItemRecptLine.getBatchCode())) return AjaxResult.error(422, "请输入批次号");
        wmItemRecptLine.setBatchCode(wmItemRecptLine.getBatchCode().trim());
        if (wmItemRecptLine.getBatchCode().length() > 255) return AjaxResult.error(422, "批次号长度不能超过255个字符");
        if(StringUtils.isNotNull(wmItemRecptLine.getWarehouseId())){
            WmWarehouse warehouse = wmWarehouseService.selectWmWarehouseByWarehouseId(wmItemRecptLine.getWarehouseId());
            wmItemRecptLine.setWarehouseCode(warehouse.getWarehouseCode());
            wmItemRecptLine.setWarehouseName(warehouse.getWarehouseName());
        }
        if(StringUtils.isNotNull(wmItemRecptLine.getLocationId())){
            WmStorageLocation location = wmStorageLocationService.selectWmStorageLocationByLocationId(wmItemRecptLine.getLocationId());
            wmItemRecptLine.setLocationCode(location.getLocationCode());
            wmItemRecptLine.setLocationName(location.getLocationName());
        }
        if(StringUtils.isNotNull(wmItemRecptLine.getAreaId())){
            WmStorageArea area = wmStorageAreaService.selectWmStorageAreaByAreaId(wmItemRecptLine.getAreaId());
            wmItemRecptLine.setAreaCode(area.getAreaCode());
            wmItemRecptLine.setAreaName(area.getAreaName());
        }
        return toAjax(wmItemRecptLineService.updateWmItemRecptLine(wmItemRecptLine));
    }

    /** Update only a receipt line's batch and destination during the APPROVING putaway step. */
    @PreAuthorize("@ss.hasPermi('mes:wm:itemrecpt:edit')")
    @Log(title = "物料入库单上架", businessType = BusinessType.UPDATE)
    @PutMapping("/{lineId}/putaway")
    public AjaxResult putaway(@PathVariable Long lineId, @RequestBody WmItemRecptLine request) {
        if (StringUtils.isEmpty(request.getBatchCode())) {
            return AjaxResult.error(422, "请输入批次号");
        }
        request.setBatchCode(request.getBatchCode().trim());
        if (request.getBatchCode().length() > 255) {
            return AjaxResult.error(422, "批次号长度不能超过255个字符");
        }
        if (request.getWarehouseId() == null || request.getLocationId() == null || request.getAreaId() == null) {
            return AjaxResult.error(422, "请选择完整的仓库、库位和库区");
        }
        WmWarehouse warehouse = wmWarehouseService.selectWmWarehouseByWarehouseId(request.getWarehouseId());
        WmStorageLocation location = wmStorageLocationService.selectWmStorageLocationByLocationId(request.getLocationId());
        WmStorageArea area = wmStorageAreaService.selectWmStorageAreaByAreaId(request.getAreaId());
        if (warehouse == null || location == null || area == null
                || !request.getWarehouseId().equals(location.getWarehouseId())
                || !request.getLocationId().equals(area.getLocationId())) {
            return AjaxResult.error(422, "仓库、库位和库区层级不匹配");
        }
        WmItemRecptLine line = new WmItemRecptLine();
        line.setLineId(lineId);
        line.setBatchCode(request.getBatchCode().trim());
        line.setWarehouseId(warehouse.getWarehouseId());
        line.setWarehouseCode(warehouse.getWarehouseCode());
        line.setWarehouseName(warehouse.getWarehouseName());
        line.setLocationId(location.getLocationId());
        line.setLocationCode(location.getLocationCode());
        line.setLocationName(location.getLocationName());
        line.setAreaId(area.getAreaId());
        line.setAreaCode(area.getAreaCode());
        line.setAreaName(area.getAreaName());
        return toAjax(wmItemRecptLineService.updateReceiptLinePutaway(line));
    }

    /**
     * 删除物料入库单行
     */
    @PreAuthorize("@ss.hasPermi('mes:wm:itemrecptline:remove')")
    @Log(title = "物料入库单行", businessType = BusinessType.DELETE)
	@DeleteMapping("/{lineIds}")
    public AjaxResult remove(@PathVariable Long[] lineIds)
    {
        return toAjax(wmItemRecptLineService.deleteWmItemRecptLineByLineIds(lineIds));
    }
}
