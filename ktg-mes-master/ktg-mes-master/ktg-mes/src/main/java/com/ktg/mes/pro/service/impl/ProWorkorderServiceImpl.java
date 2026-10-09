package com.ktg.mes.pro.service.impl;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.stream.Collectors;

import com.ktg.common.constant.UserConstants;
import com.ktg.common.constant.HttpStatus;
import com.ktg.common.core.domain.AjaxResult;
import com.ktg.common.exception.ServiceException;
import com.ktg.common.utils.DateUtils;
import com.ktg.common.utils.StringUtils;
import com.ktg.mes.pro.controller.vo.ProRouteHomeVO;
import com.ktg.mes.pro.controller.vo.ProWorkorderHomeVO;
import com.ktg.mes.pro.domain.ProFeedback;
import com.ktg.mes.pro.domain.ProRouteProcess;
import com.ktg.mes.pro.domain.ProRouteProduct;
import com.ktg.mes.pro.service.IProFeedbackService;
import com.ktg.mes.pro.service.IProRouteProcessService;
import com.ktg.mes.pro.service.IProRouteProductService;
import com.ktg.mes.md.domain.MdProductBom;
import com.ktg.mes.md.mapper.MdProductBomMapper;
import com.ktg.mes.pro.domain.ProWorkorderBom;
import com.ktg.mes.pro.mapper.ProWorkorderBomMapper;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.BeanUtils;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import com.ktg.mes.md.domain.MdItem;
import com.ktg.mes.md.service.IMdItemService;
import com.ktg.mes.pro.mapper.ProWorkorderMapper;
import com.ktg.mes.pro.domain.ProWorkorder;
import com.ktg.mes.pro.service.IProWorkorderService;

/**
 * 生产工单Service业务层处理
 * 
 * @author yinjinlu
 * @date 2022-05-09
 */
@Service
public class ProWorkorderServiceImpl implements IProWorkorderService 
{
    private static final String DEFAULT_WORKORDER_TYPE = "SELF";

    @Autowired
    private ProWorkorderMapper proWorkorderMapper;

    @Autowired
    private IProRouteProductService proRouteProductService;

    @Autowired
    private IProRouteProcessService proRouteProcessService;

    @Autowired
    private IProFeedbackService proFeedbackService;

    @Autowired
    private IMdItemService mdItemService;

    @Autowired
    private MdProductBomMapper mdProductBomMapper;

    @Autowired
    private ProWorkorderBomMapper proWorkorderBomMapper;

    /**
     * 查询生产工单
     * 
     * @param workorderId 生产工单主键
     * @return 生产工单
     */
    @Override
    public ProWorkorder selectProWorkorderByWorkorderId(Long workorderId)
    {
        return proWorkorderMapper.selectProWorkorderByWorkorderId(workorderId);
    }

    /**
     * 查询生产工单列表
     * 
     * @param proWorkorder 生产工单
     * @return 生产工单
     */
    @Override
    public List<ProWorkorder> selectProWorkorderList(ProWorkorder proWorkorder)
    {
        return proWorkorderMapper.selectProWorkorderList(proWorkorder);
    }

    @Override
    public String checkWorkorderCodeUnique(ProWorkorder proWorkorder) {
        ProWorkorder workorder = proWorkorderMapper.checkWorkorderCodeUnique(proWorkorder);
        Long workorderId = proWorkorder.getWorkorderId() == null? -1L: proWorkorder.getWorkorderId();
        if(StringUtils.isNotNull(workorder) && workorder.getWorkorderId().longValue() != workorderId.longValue()){
            return UserConstants.NOT_UNIQUE;
        }
        return UserConstants.UNIQUE;
    }


    /**
     * 新增生产工单
     * 
     * @param proWorkorder 生产工单
     * @return 结果
     */
    @Override
    public int insertProWorkorder(ProWorkorder proWorkorder)
    {
        normalizeDraft(proWorkorder);
        proWorkorder.setStatus(UserConstants.ORDER_STATUS_PREPARE);
        if (StringUtils.isEmpty(proWorkorder.getWorkorderType()))
        {
            proWorkorder.setWorkorderType(DEFAULT_WORKORDER_TYPE);
        }
        proWorkorder.setCreateTime(DateUtils.getNowDate());
        return proWorkorderMapper.insertProWorkorder(proWorkorder);
    }

    @Override
    @Transactional(rollbackFor = Exception.class)
    public Long createDraftWorkorder(ProWorkorder proWorkorder)
    {
        ensureWorkorderCodeUnique(proWorkorder);
        try
        {
            int inserted = insertProWorkorder(proWorkorder);
            if (inserted != 1 || proWorkorder.getWorkorderId() == null)
            {
                throw new ServiceException("生产工单创建失败");
            }
            generateBomLines(proWorkorder);
            return proWorkorder.getWorkorderId();
        }
        catch (DuplicateKeyException e)
        {
            throw duplicateWorkorderCode();
        }
    }

    /**
     * 修改生产工单
     * 
     * @param proWorkorder 生产工单
     * @return 结果
     */
    @Override
    public int updateProWorkorder(ProWorkorder proWorkorder)
    {
        proWorkorder.setUpdateTime(DateUtils.getNowDate());
        return proWorkorderMapper.updateProWorkorder(proWorkorder);
    }

    @Override
    @Transactional(rollbackFor = Exception.class)
    public int updateDraftWorkorder(ProWorkorder proWorkorder)
    {
        ProWorkorder existing = proWorkorderMapper.selectProWorkorderByWorkorderId(proWorkorder.getWorkorderId());
        if (StringUtils.isNull(existing))
        {
            throw new ServiceException("生产工单不存在！", HttpStatus.NOT_FOUND);
        }
        if (!UserConstants.ORDER_STATUS_PREPARE.equals(existing.getStatus()))
        {
            throw new ServiceException("只能编辑草稿状态生产工单！", HttpStatus.CONFLICT);
        }
        ensureWorkorderCodeUnique(proWorkorder);

        boolean rebuildBom = !Objects.equals(existing.getProductId(), proWorkorder.getProductId())
                || existing.getQuantity().compareTo(proWorkorder.getQuantity()) != 0;
        normalizeDraft(proWorkorder);
        proWorkorder.setUpdateTime(DateUtils.getNowDate());
        try
        {
            int updated = proWorkorderMapper.updateProWorkorder(proWorkorder);
            if (updated != 1)
            {
                throw new ServiceException("生产工单更新失败");
            }
            if (rebuildBom)
            {
                proWorkorderBomMapper.deleteProWorkorderBomByWorkorderId(proWorkorder.getWorkorderId());
                generateBomLines(proWorkorder);
            }
            return updated;
        }
        catch (DuplicateKeyException e)
        {
            throw duplicateWorkorderCode();
        }
    }

    private void ensureWorkorderCodeUnique(ProWorkorder proWorkorder)
    {
        if (UserConstants.NOT_UNIQUE.equals(checkWorkorderCodeUnique(proWorkorder)))
        {
            throw duplicateWorkorderCode();
        }
    }

    private ServiceException duplicateWorkorderCode()
    {
        return new ServiceException("生产工单编号已存在！", HttpStatus.CONFLICT);
    }

    private void generateBomLines(ProWorkorder workorder)
    {
        MdProductBom query = new MdProductBom();
        query.setItemId(workorder.getProductId());
        List<MdProductBom> boms = mdProductBomMapper.selectMdProductBomList(query);
        if (boms == null)
        {
            return;
        }
        for (MdProductBom bom : boms)
        {
            ProWorkorderBom row = new ProWorkorderBom();
            row.setWorkorderId(workorder.getWorkorderId());
            row.setItemId(bom.getBomItemId());
            row.setItemCode(bom.getBomItemCode());
            row.setItemName(bom.getBomItemName());
            row.setItemSpc(bom.getBomItemSpec());
            row.setItemOrProduct(bom.getItemOrProduct());
            row.setUnitOfMeasure(bom.getUnitOfMeasure());
            row.setQuantity(workorder.getQuantity().multiply(bom.getQuantity()));
            if (proWorkorderBomMapper.insertProWorkorderBom(row) != 1)
            {
                throw new ServiceException("生产工单 BOM 生成失败");
            }
        }
    }

    private void normalizeDraft(ProWorkorder workorder)
    {
        MdItem product = mdItemService.selectMdItemById(workorder.getProductId());
        if (StringUtils.isNull(product))
        {
            throw new ServiceException("productId: 产品不存在");
        }
        workorder.setProductCode(product.getItemCode());
        workorder.setProductName(product.getItemName());
        workorder.setProductSpc(product.getSpecification());
        workorder.setUnitOfMeasure(product.getUnitOfMeasure());

        Long parentId = workorder.getParentId();
        if (parentId == null || parentId == 0L)
        {
            workorder.setParentId(0L);
            workorder.setAncestors("0");
            return;
        }

        ProWorkorder parent = proWorkorderMapper.selectProWorkorderByWorkorderId(parentId);
        if (StringUtils.isNull(parent))
        {
            throw new ServiceException("parentId: 父工单不存在");
        }
        if (parentId.equals(workorder.getWorkorderId()) || containsAncestor(parent.getAncestors(), workorder.getWorkorderId()))
        {
            throw new ServiceException("parentId: 父工单不能形成循环引用");
        }
        workorder.setAncestors(parent.getAncestors() + "," + parent.getWorkorderId());
    }

    private boolean containsAncestor(String ancestors, Long workorderId)
    {
        if (StringUtils.isEmpty(ancestors) || workorderId == null)
        {
            return false;
        }
        for (String ancestor : ancestors.split(","))
        {
            if (String.valueOf(workorderId).equals(ancestor))
            {
                return true;
            }
        }
        return false;
    }

    /**
     * 批量删除生产工单
     * 
     * @param workorderIds 需要删除的生产工单主键
     * @return 结果
     */
    @Override
    public int deleteProWorkorderByWorkorderIds(Long[] workorderIds)
    {
        return proWorkorderMapper.deleteProWorkorderByWorkorderIds(workorderIds);
    }

    /**
     * 删除生产工单信息
     * 
     * @param workorderId 生产工单主键
     * @return 结果
     */
    @Override
    public int deleteProWorkorderByWorkorderId(Long workorderId)
    {
        return proWorkorderMapper.deleteProWorkorderByWorkorderId(workorderId);
    }

    @Override
    public AjaxResult getHomeList(ProWorkorder proWorkorder)
    {
        List<ProWorkorder> workorders = proWorkorderMapper.selectProWorkorderList(proWorkorder);
        if (workorders == null || workorders.isEmpty())
        {
            return AjaxResult.success(workorders);
        }

        List<Long> workorderIds = workorders.stream()
                .map(ProWorkorder::getWorkorderId)
                .filter(Objects::nonNull)
                .collect(Collectors.toList());
        List<ProFeedback> feedbacks = workorderIds.isEmpty()
                ? new ArrayList<>()
                : proFeedbackService.selectByWorkorderIds(workorderIds);
        if (feedbacks == null)
        {
            feedbacks = new ArrayList<>();
        }
        feedbacks.forEach(feedback -> {
            if (feedback.getQuantityFeedback() == null)
            {
                feedback.setQuantityFeedback(BigDecimal.ZERO);
            }
        });

        List<Long> productIds = workorders.stream()
                .map(ProWorkorder::getProductId)
                .filter(Objects::nonNull)
                .distinct()
                .collect(Collectors.toList());
        List<ProRouteProduct> routeProducts = productIds.isEmpty()
                ? new ArrayList<>()
                : proRouteProductService.selectByProductIds(productIds);
        if (routeProducts == null)
        {
            routeProducts = new ArrayList<>();
        }

        List<Long> routeIds = routeProducts.stream()
                .map(ProRouteProduct::getRouteId)
                .filter(Objects::nonNull)
                .distinct()
                .collect(Collectors.toList());
        List<ProRouteProcess> routeProcesses = routeIds.isEmpty()
                ? new ArrayList<>()
                : proRouteProcessService.selectByRouteIds(routeIds);
        if (routeProcesses == null)
        {
            routeProcesses = new ArrayList<>();
        }

        Map<Long, List<ProRouteProcess>> processesByRouteId = routeProcesses.stream()
                .filter(process -> process.getRouteId() != null)
                .collect(Collectors.groupingBy(ProRouteProcess::getRouteId));
        Map<Long, List<ProRouteProcess>> processesByProductId = new HashMap<>();
        for (ProRouteProduct routeProduct : routeProducts)
        {
            if (routeProduct.getItemId() != null && routeProduct.getRouteId() != null)
            {
                processesByProductId.computeIfAbsent(routeProduct.getItemId(), key -> new ArrayList<>())
                        .addAll(processesByRouteId.getOrDefault(routeProduct.getRouteId(), new ArrayList<>()));
            }
        }

        final List<ProFeedback> feedbackList = feedbacks;
        List<ProWorkorderHomeVO> homeWorkorders = new ArrayList<>();
        for (ProWorkorder workorder : workorders)
        {
            ProWorkorderHomeVO homeWorkorder = new ProWorkorderHomeVO();
            BeanUtils.copyProperties(workorder, homeWorkorder);

            List<ProRouteHomeVO> progressList = new ArrayList<>();
            List<ProRouteProcess> processes = processesByProductId.get(workorder.getProductId());
            if (processes != null)
            {
                for (ProRouteProcess process : processes)
                {
                    ProRouteHomeVO progress = new ProRouteHomeVO();
                    BeanUtils.copyProperties(process, progress);
                    BigDecimal total = workorder.getQuantity() == null ? BigDecimal.ZERO : workorder.getQuantity();
                    BigDecimal completed = feedbackList.stream()
                            .filter(feedback -> Objects.equals(feedback.getWorkorderId(), workorder.getWorkorderId())
                                    && Objects.equals(feedback.getProcessId(), process.getProcessId()))
                            .map(ProFeedback::getQuantityFeedback)
                            .reduce(BigDecimal.ZERO, BigDecimal::add);
                    progress.setTotal(total);
                    progress.setCompleteNumber(completed);
                    progress.setIncompleteNumber(total.subtract(completed));
                    progressList.add(progress);
                }
            }
            homeWorkorder.setRouteHomg(progressList);
            homeWorkorders.add(homeWorkorder);
        }

        Map<Long, List<ProWorkorderHomeVO>> childrenByParentId = homeWorkorders.stream()
                .filter(item -> item.getParentId() != null)
                .collect(Collectors.groupingBy(ProWorkorderHomeVO::getParentId));
        homeWorkorders.forEach(item -> item.setChildren(
                childrenByParentId.getOrDefault(item.getWorkorderId(), new ArrayList<>())));
        List<ProWorkorderHomeVO> roots = homeWorkorders.stream()
                .filter(item -> item.getParentId() == null || Long.valueOf(0L).equals(item.getParentId()))
                .collect(Collectors.toList());
        return AjaxResult.success(roots);
    }
}
