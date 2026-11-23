package com.ktg.mes.pro.execution;

import com.ktg.common.constant.UserConstants;
import com.ktg.mes.pro.controller.ProFeedbackController;
import com.ktg.mes.pro.controller.ProTaskMobController;
import com.ktg.mes.pro.domain.*;
import com.ktg.mes.pro.service.impl.*;
import com.ktg.mes.qc.controller.QcDefectRecordController;
import com.ktg.mes.qc.controller.QcIpqcController;
import com.ktg.mes.qc.domain.*;
import com.ktg.mes.qc.service.impl.*;
import com.ktg.mes.wm.domain.*;
import com.ktg.mes.wm.controller.WmProductProduceController;
import com.ktg.mes.wm.service.impl.*;
import org.aspectj.lang.ProceedingJoinPoint;
import org.aspectj.lang.annotation.Around;
import org.aspectj.lang.annotation.Aspect;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

import java.util.ArrayList;
import java.util.List;

import static com.ktg.mes.pro.execution.ExecutionOwnershipService.Ref;
import static com.ktg.mes.pro.execution.ExecutionOwnershipService.LegacyRef;

/**
 * Explicit legacy write boundary, including the mobile paths using these
 * services. New execution commands use dedicated transactional mappers, not
 * these legacy generic CRUD methods. No role has a legacy write override.
 */
@Aspect
@Component
@Order(Ordered.LOWEST_PRECEDENCE - 100)
public class LegacyExecutionGuard {
    private final ExecutionOwnershipService ownership;
    private final TransactionTemplate transactions;

    public LegacyExecutionGuard(ExecutionOwnershipService ownership, PlatformTransactionManager manager) {
        this.ownership = ownership;
        this.transactions = new TransactionTemplate(manager);
    }

    @Around(
        "execution(* com.ktg.mes.pro.service.impl.ProWorkorderServiceImpl+.*(..)) || " +
        "execution(* com.ktg.mes.pro.service.impl.ProWorkorderBomServiceImpl+.*(..)) || " +
        "execution(* com.ktg.mes.pro.service.impl.ProTaskServiceImpl+.*(..)) || " +
        "execution(* com.ktg.mes.pro.service.impl.ProFeedbackServiceImpl+.*(..)) || " +
        "execution(* com.ktg.mes.qc.service.impl.QcIpqcServiceImpl+.*(..)) || " +
        "execution(* com.ktg.mes.qc.service.impl.QcIpqcLineServiceImpl+.*(..)) || " +
        "execution(* com.ktg.mes.qc.service.impl.QcDefectRecordServiceImpl+.*(..)) || " +
        "execution(* com.ktg.mes.wm.service.impl.WmSnServiceImpl+.*(..)) || " +
        "execution(* com.ktg.mes.wm.service.impl.WmItemConsumeServiceImpl+.*(..)) || " +
        "execution(* com.ktg.mes.wm.service.impl.WmItemConsumeLineServiceImpl+.*(..)) || " +
        "execution(* com.ktg.mes.wm.service.impl.WmProductProduceServiceImpl+.*(..)) || " +
        "execution(* com.ktg.mes.wm.service.impl.WmProductProduceLineServiceImpl+.*(..)) || " +
        "execution(* com.ktg.mes.wm.service.impl.WmPackageServiceImpl+.*(..)) || " +
        "execution(* com.ktg.mes.wm.service.impl.WmPackageLineServiceImpl+.*(..)) || " +
        "execution(* com.ktg.mes.wm.service.impl.WmMaterialStockServiceImpl+.*(..)) || " +
        "execution(* com.ktg.mes.pro.controller.ProFeedbackController.execute(..)) || " +
        "execution(* com.ktg.mes.pro.controller.ProTaskMobController.feedBack(..)) || " +
        "execution(* com.ktg.mes.qc.controller.QcIpqcController.add(..)) || " +
        "execution(* com.ktg.mes.qc.controller.QcIpqcController.edit(..)) || " +
        "execution(* com.ktg.mes.qc.controller.QcIpqcController.remove(..)) || " +
        "execution(* com.ktg.mes.qc.controller.QcDefectRecordController.updateList(..)) || " +
        "execution(* com.ktg.mes.wm.controller.WmProductProduceController.remove(..))"
    )
    public Object guard(ProceedingJoinPoint call) throws Throwable {
        String method = call.getSignature().getName();
        if (!method.startsWith("insert") && !method.startsWith("update") && !method.startsWith("delete")
                && !method.startsWith("generate") && !method.equals("createDraftWorkorder")
                && !method.equals("execute") && !method.equals("feedBack") && !method.equals("remove")
                && !method.equals("add") && !method.equals("edit")) {
            return call.proceed();
        }
        List<LegacyRef> refs = new ArrayList<>();
        Object target = call.getTarget();
        Object[] args = call.getArgs();
        Ref idType = kind(target, method);
        if (target instanceof QcIpqcLineServiceImpl && "updateCrMajMinQuantity".equals(method)) {
            add(refs, Ref.IPQC, args[0]); add(refs, Ref.IPQC_LINE, args[1]);
        } else {
            for (Object argument : args) collect(refs, idType, argument);
        }
        if (refs.isEmpty()) return call.proceed();

        // Whole bulk and compound operation checked before any legacy write.
        // Required transaction joins an existing controller/service transaction.
        try {
            return transactions.execute(status -> {
                ownership.requireLegacyWritable(refs);
                try {
                    return call.proceed();
                } catch (RuntimeException | Error ex) {
                    throw ex;
                } catch (Throwable ex) {
                    throw new CheckedInvocation(ex);
                }
            });
        } catch (CheckedInvocation ex) {
            throw ex.getCause();
        }
    }

    private Ref kind(Object target, String method) {
        if (target instanceof ProWorkorderServiceImpl) return Ref.ORDER;
        if (target instanceof ProWorkorderBomServiceImpl)
            return "deleteProWorkorderBomByWorkorderId".equals(method) ? Ref.ORDER : Ref.BOM;
        if (target instanceof ProTaskServiceImpl || target instanceof ProTaskMobController) return Ref.TASK;
        if (target instanceof ProFeedbackServiceImpl || target instanceof ProFeedbackController) return Ref.FEEDBACK;
        if (target instanceof QcIpqcServiceImpl || target instanceof QcIpqcController) return Ref.IPQC;
        if (target instanceof QcIpqcLineServiceImpl)
            return "deleteByIpqcId".equals(method) ? Ref.IPQC : Ref.IPQC_LINE;
        if (target instanceof QcDefectRecordServiceImpl || target instanceof QcDefectRecordController) return Ref.DEFECT;
        if (target instanceof WmSnServiceImpl) return Ref.SN;
        if (target instanceof WmItemConsumeServiceImpl) return Ref.CONSUME;
        if (target instanceof WmItemConsumeLineServiceImpl) return Ref.CONSUME_LINE;
        if (target instanceof WmProductProduceServiceImpl || target instanceof WmProductProduceController) return Ref.PRODUCE;
        if (target instanceof WmProductProduceLineServiceImpl)
            return "deleteByRecordId".equals(method) ? Ref.PRODUCE : Ref.PRODUCE_LINE;
        if (target instanceof WmPackageServiceImpl) return Ref.PACKAGE;
        if (target instanceof WmPackageLineServiceImpl) return Ref.PACKAGE_LINE;
        if (target instanceof WmMaterialStockServiceImpl) return Ref.STOCK;
        throw new IllegalStateException("Unclassified legacy write boundary");
    }

    private void collect(List<LegacyRef> refs, Ref idType, Object value) {
        if (value instanceof Long) add(refs, idType, value);
        else if (value instanceof Long[]) {
            for (Long id : (Long[]) value) add(refs, idType, id);
        } else if (value instanceof Iterable) {
            for (Object element : (Iterable<?>) value) collect(refs, idType, element);
        } else if (value instanceof ProWorkorder) {
            ProWorkorder d = (ProWorkorder) value;
            add(refs, Ref.ORDER, d.getWorkorderId());
        } else if (value instanceof ProWorkorderBom) {
            ProWorkorderBom d = (ProWorkorderBom) value;
            add(refs, Ref.BOM, d.getLineId()); add(refs, Ref.ORDER, d.getWorkorderId());
        } else if (value instanceof ProTask) {
            ProTask d = (ProTask) value;
            add(refs, Ref.TASK, d.getTaskId()); add(refs, Ref.ORDER, d.getWorkorderId());
            add(refs, Ref.ORDER_CODE, d.getWorkorderCode());
        } else if (value instanceof ProFeedback) {
            ProFeedback d = (ProFeedback) value;
            add(refs, Ref.FEEDBACK, d.getRecordId()); add(refs, Ref.TASK, d.getTaskId());
            add(refs, Ref.ORDER, d.getWorkorderId()); add(refs, Ref.ORDER_CODE, d.getWorkorderCode());
        } else if (value instanceof QcIpqc) {
            QcIpqc d = (QcIpqc) value;
            add(refs, Ref.IPQC, d.getIpqcId()); add(refs, Ref.TASK, d.getTaskId());
            add(refs, Ref.ORDER, d.getWorkorderId()); add(refs, Ref.ORDER_CODE, d.getWorkorderCode());
        } else if (value instanceof QcIpqcLine) {
            QcIpqcLine d = (QcIpqcLine) value;
            add(refs, Ref.IPQC_LINE, d.getLineId()); add(refs, Ref.IPQC, d.getIpqcId());
        } else if (value instanceof QcDefectRecord) {
            QcDefectRecord d = (QcDefectRecord) value;
            add(refs, Ref.DEFECT, d.getRecordId());
            if (UserConstants.QC_TYPE_IPQC.equals(d.getQcType()) || d.getQcType() == null) {
                add(refs, Ref.IPQC, d.getQcId()); add(refs, Ref.IPQC_LINE, d.getLineId());
            }
        } else if (value instanceof WmSn) {
            add(refs, Ref.SN, ((WmSn) value).getSnId());
        } else if (value instanceof WmItemConsume) {
            WmItemConsume d = (WmItemConsume) value;
            add(refs, Ref.CONSUME, d.getRecordId()); add(refs, Ref.TASK, d.getTaskId());
            add(refs, Ref.ORDER, d.getWorkorderId()); add(refs, Ref.ORDER_CODE, d.getWorkorderCode());
        } else if (value instanceof WmItemConsumeLine) {
            WmItemConsumeLine d = (WmItemConsumeLine) value;
            add(refs, Ref.CONSUME_LINE, d.getLineId()); add(refs, Ref.CONSUME, d.getRecordId());
            add(refs, Ref.STOCK, d.getMaterialStockId());
        } else if (value instanceof WmProductProduce) {
            WmProductProduce d = (WmProductProduce) value;
            add(refs, Ref.PRODUCE, d.getRecordId()); add(refs, Ref.TASK, d.getTaskId());
            add(refs, Ref.ORDER, d.getWorkorderId()); add(refs, Ref.ORDER_CODE, d.getWorkorderCode());
        } else if (value instanceof WmProductProduceLine) {
            WmProductProduceLine d = (WmProductProduceLine) value;
            add(refs, Ref.PRODUCE_LINE, d.getLineId()); add(refs, Ref.PRODUCE, d.getRecordId());
        } else if (value instanceof WmPackage) {
            WmPackage d = (WmPackage) value;
            add(refs, Ref.PACKAGE, d.getPackageId()); add(refs, Ref.PACKAGE, d.getParentId());
        } else if (value instanceof WmPackageLine) {
            WmPackageLine d = (WmPackageLine) value;
            add(refs, Ref.PACKAGE_LINE, d.getLineId()); add(refs, Ref.PACKAGE, d.getPackageId());
            add(refs, Ref.STOCK, d.getMaterialStockId()); add(refs, Ref.ORDER, d.getWorkorderId());
            add(refs, Ref.ORDER_CODE, d.getWorkorderCode());
        } else if (value instanceof WmMaterialStock) {
            WmMaterialStock d = (WmMaterialStock) value;
            add(refs, Ref.STOCK, d.getMaterialStockId()); add(refs, Ref.ORDER, d.getWorkorderId());
            add(refs, Ref.ORDER_CODE, d.getWorkorderCode());
        }
    }

    private static void add(List<LegacyRef> refs, Ref type, Object key) {
        if (key != null && (!(key instanceof String) || !((String) key).trim().isEmpty())
                && (!(key instanceof Long) || (Long) key > 0L)) {
            refs.add(new LegacyRef(type, key));
        }
    }

    private static final class CheckedInvocation extends RuntimeException {
        CheckedInvocation(Throwable cause) { super(cause); }
    }
}
