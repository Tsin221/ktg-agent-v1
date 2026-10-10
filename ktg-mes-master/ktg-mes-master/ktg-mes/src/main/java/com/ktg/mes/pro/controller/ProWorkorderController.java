package com.ktg.mes.pro.controller;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.List;
import javax.servlet.http.HttpServletResponse;
import javax.validation.Valid;

import cn.hutool.core.collection.CollUtil;
import com.ktg.common.core.page.TableDataInfo;
import com.ktg.mes.md.domain.MdProductBom;
import com.ktg.mes.md.service.IMdProductBomService;
import com.ktg.mes.pro.domain.ProWorkorderBom;
import com.ktg.mes.pro.controller.dto.ProWorkorderDraftRequest;
import com.ktg.mes.pro.service.IProWorkorderBomService;
import io.minio.messages.Item;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.util.CollectionUtils;
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
import com.ktg.mes.pro.domain.ProWorkorder;
import com.ktg.mes.pro.service.IProWorkorderService;
import com.ktg.common.utils.poi.ExcelUtil;

/**
 * 生产工单Controller
 * 
 * @author yinjinlu
 * @date 2022-05-09
 */
@RestController
@RequestMapping("/mes/pro/workorder")
public class ProWorkorderController extends BaseController
{
    @Autowired
    private IProWorkorderService proWorkorderService;

    @Autowired
    private IProWorkorderBomService proWorkorderBomService;

    @Autowired
    private IMdProductBomService mdProductBomService;

    /**
     * 查询生产工单列表
     */
    @PreAuthorize("@ss.hasPermi('mes:pro:workorder:list')")
    @GetMapping("/list")
    public TableDataInfo list(ProWorkorder proWorkorder)
    {
        startPage();
        List<ProWorkorder> list = proWorkorderService.selectProWorkorderList(proWorkorder);
        return getDataTable(list);
    }

    /** 查询首页生产工单及工序进度 */
    @GetMapping("/getHomeList")
    public AjaxResult getHomeList(ProWorkorder proWorkorder)
    {
        return proWorkorderService.getHomeList(proWorkorder);
    }

    /**
     * 导出生产工单列表
     */
    @PreAuthorize("@ss.hasPermi('mes:pro:workorder:export')")
    @Log(title = "生产工单", businessType = BusinessType.EXPORT)
    @PostMapping("/export")
    public void export(HttpServletResponse response, ProWorkorder proWorkorder)
    {
        List<ProWorkorder> list = proWorkorderService.selectProWorkorderList(proWorkorder);
        ExcelUtil<ProWorkorder> util = new ExcelUtil<ProWorkorder>(ProWorkorder.class);
        util.exportExcel(response, list, "生产工单数据");
    }

    /**
     * 获取生产工单详细信息
     */
    @PreAuthorize("@ss.hasPermi('mes:pro:workorder:query')")
    @GetMapping(value = "/{workorderId}")
    public AjaxResult getInfo(@PathVariable("workorderId") Long workorderId)
    {
        return AjaxResult.success(proWorkorderService.selectProWorkorderByWorkorderId(workorderId));
    }

    /**
     * 新增生产工单
     */
    @PreAuthorize("@ss.hasPermi('mes:pro:workorder:add')")
    @Log(title = "生产工单", businessType = BusinessType.INSERT)
    @PostMapping
    public AjaxResult add(@Valid @RequestBody ProWorkorderDraftRequest request)
    {
        ProWorkorder proWorkorder = request.toWorkorder();
        proWorkorder.setCreateBy(getUsername());
        return AjaxResult.success(proWorkorderService.createDraftWorkorder(proWorkorder));
    }

    /**
     * 修改生产工单
     */
    @PreAuthorize("@ss.hasPermi('mes:pro:workorder:edit')")
    @Log(title = "生产工单", businessType = BusinessType.UPDATE)
    @PutMapping
    public AjaxResult edit(@Valid @RequestBody ProWorkorderDraftRequest request)
    {
        ProWorkorder proWorkorder = request.toWorkorder();
        proWorkorder.setUpdateBy(getUsername());
        return toAjax(proWorkorderService.updateDraftWorkorder(proWorkorder));
    }

    /**
     * 删除生产工单
     */
    @PreAuthorize("@ss.hasPermi('mes:pro:workorder:remove')")
    @Log(title = "生产工单", businessType = BusinessType.DELETE)
	@DeleteMapping("/{workorderIds}")
    public AjaxResult remove(@PathVariable("workorderIds") Long[] workorderIds)
    {
        return toAjax(proWorkorderService.deleteProWorkorderByWorkorderIds(workorderIds));
    }

    /**
     * 获取当前工单的物料需求清单
     */
    @PreAuthorize("@ss.hasPermi('mes:pro:workorder:list')")
    @GetMapping("/listItems")
    public TableDataInfo listItemss(ProWorkorder proWorkorder)
    {
        List<MdProductBom> result = new ArrayList<MdProductBom>();
        ProWorkorderBom param = new ProWorkorderBom();
        param.setWorkorderId(proWorkorder.getWorkorderId());
        List<ProWorkorderBom> boms = proWorkorderBomService.selectProWorkorderBomList(param);
        if(!CollectionUtils.isEmpty(boms)){
            for ( ProWorkorderBom bom: boms
                 ) {
                MdProductBom theBom = new MdProductBom();
                theBom.setBomItemId(bom.getItemId());
                result.addAll(getBoms(theBom,bom.getQuantity(),0));
            }
        }
        return getDataTable(result);
    }

    private List<MdProductBom> getBoms(MdProductBom item,BigDecimal quantity,int count){
        MdProductBom param = new MdProductBom();
        List<MdProductBom> results = new ArrayList<MdProductBom>();
        if(count >20){
            return results;
        }
        param.setItemId(item.getBomItemId());
        List<MdProductBom> boms = mdProductBomService.selectMdProductBomList(param);
        if(CollUtil.isNotEmpty(boms)){
            //最多20层依赖
            count ++;
            for (MdProductBom bomItem: boms
                 ) {
                bomItem.setQuantity(quantity.multiply(bomItem.getQuantity()));
                results.addAll(getBoms(bomItem,bomItem.getQuantity(),count));
            }
        }else{
            results.add(item);
        }
        return results;
    }

}
