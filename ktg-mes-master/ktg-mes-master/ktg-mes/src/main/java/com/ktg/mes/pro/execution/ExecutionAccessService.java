package com.ktg.mes.pro.execution;

import com.ktg.common.utils.SecurityUtils;
import com.ktg.framework.web.service.PermissionService;
import org.springframework.stereotype.Service;

import java.util.Collection;
import java.util.HashSet;
import java.util.Objects;

/** Shared execution permission and object-scope rules. No client actor or workstation grant. */
@Service
public class ExecutionAccessService {
    private static final String PREFIX = "mes:pro:execution:";
    private final ExecutionOwnershipService ownership;
    private final PermissionService permissions;

    public ExecutionAccessService(ExecutionOwnershipService ownership, PermissionService permissions) {
        this.ownership = ownership;
        this.permissions = permissions;
    }

    public void requireOperation(ExecutionAction action, Long workorderId, Long taskId, Long snId) {
        authorize(action, workorderId, taskId, snId, false);
    }

    /** Batch report/pack callers check every selected SN before any business write. */
    public void requireTaskUnits(ExecutionAction action, Long workorderId, Long taskId, Collection<Long> snIds) {
        if ((action != ExecutionAction.REPORT && action != ExecutionAction.PACK)
                || snIds == null || snIds.isEmpty() || new HashSet<>(snIds).size() != snIds.size()
                || snIds.contains(null)) {
            throw ExecutionException.validation("单件列表无效");
        }
        for (Long snId : snIds) {
            requireOperation(action, workorderId, taskId, snId);
        }
    }

    /** Receipt callers also must verify receipt.actor_id == SecurityUtils.getUserId(). */
    public void requireReceipt(ExecutionAction action, Long workorderId, Long taskId, Long snId) {
        try {
            requirePermission("query");
            authorize(action, workorderId, taskId, snId, true);
        } catch (ExecutionException | com.ktg.common.exception.ServiceException ex) {
            throw ExecutionException.notFound();
        }
    }

    private void authorize(ExecutionAction action, Long workorderId, Long taskId, Long snId, boolean receipt) {
        if (action == null || workorderId == null) throw ExecutionException.forbidden();
        if (action != ExecutionAction.TASK_COMPLETE) {
            if (!permissions.hasPermi(action.getPermission())) throw ExecutionException.forbidden();
        }
        ExecutionOwnershipService.OrderScope order = ownership.order(workorderId);
        if (order == null) throw ExecutionException.notFound();
        Long actor = SecurityUtils.getUserId();
        if (actor == null) throw ExecutionException.forbidden();

        ExecutionOwnershipService.TaskScope task = ownership.task(taskId);
        if (taskId != null && (task == null || task.workorderId != workorderId.longValue())) {
            throw ExecutionException.notFound();
        }
        ExecutionOwnershipService.UnitScope unit = ownership.unit(snId);
        if (snId != null && (unit == null || unit.workorderId != workorderId.longValue())) {
            throw ExecutionException.notFound();
        }

        switch (action) {
            case RELEASE:
            case ASSIGN:
            case COMPLETE:
            case DISPOSITION_REWORK:
            case DISPOSITION_SCRAP:
                if (!manages(order, actor)) throw ExecutionException.forbidden();
                if (action == ExecutionAction.ASSIGN && task == null) throw ExecutionException.notFound();
                if ((action == ExecutionAction.DISPOSITION_REWORK || action == ExecutionAction.DISPOSITION_SCRAP)
                        && (unit == null || (!receipt && taskId != null && unit.currentTaskId != taskId.longValue()))) {
                    throw ExecutionException.notFound();
                }
                return;
            default:
                if (order.releaseUserId == null || task == null || !actor.equals(task.assignedUserId)) {
                    throw ExecutionException.forbidden();
                }
                if ((action == ExecutionAction.INSPECT_ACCEPT || action == ExecutionAction.INSPECT_REJECT)
                        && unit == null) throw ExecutionException.notFound();
                if (!receipt && unit != null && unit.currentTaskId != taskId.longValue()) {
                    throw ExecutionException.notFound();
                }
                requireTaskAction(action, task);
        }
    }

    private void requireTaskAction(ExecutionAction action, ExecutionOwnershipService.TaskScope task) {
        boolean rework = "REWORK".equals(task.taskKind);
        if (!rework && !"NORMAL".equals(task.taskKind)) throw ExecutionException.forbidden();
        switch (action) {
            case REPORT:
                if (rework || !"ASSEMBLY".equals(task.stepKind)) throw ExecutionException.forbidden();
                return;
            case INSPECT_ACCEPT:
            case INSPECT_REJECT:
                if (rework || !"INSPECTION".equals(task.stepKind)) throw ExecutionException.forbidden();
                return;
            case PACK:
                if (rework || !"PACKING".equals(task.stepKind)) throw ExecutionException.forbidden();
                return;
            case REWORK_COMPLETE:
                if (!rework) throw ExecutionException.forbidden();
                return;
            case TASK_COMPLETE:
                if (rework) throw ExecutionException.forbidden();
                // Task completion is an execution action, not permission to close a workorder.
                requireStepPermission(task);
                return;
            case START:
                requireStepPermission(task);
                return;
            default:
                throw ExecutionException.forbidden();
        }
    }

    private void requireStepPermission(ExecutionOwnershipService.TaskScope task) {
        if ("REWORK".equals(task.taskKind)) requirePermission("rework");
        else if ("ASSEMBLY".equals(task.stepKind)) requirePermission("report");
        else if ("INSPECTION".equals(task.stepKind)) requirePermission("inspect");
        else if ("PACKING".equals(task.stepKind)) requirePermission("pack");
        else throw ExecutionException.forbidden();
    }

    public void requireOrderQuery(Long workorderId) {
        requirePermission("query");
        ExecutionOwnershipService.OrderScope order = ownership.order(workorderId);
        Long actor = SecurityUtils.getUserId();
        if (order == null || (!manages(order, actor) && !ownership.hasAssignedTask(workorderId, actor))) {
            throw ExecutionException.notFound();
        }
    }

    public void requireTaskQuery(Long workorderId, Long taskId) {
        requirePermission("query");
        ExecutionOwnershipService.OrderScope order = ownership.order(workorderId);
        ExecutionOwnershipService.TaskScope task = ownership.task(taskId);
        Long actor = SecurityUtils.getUserId();
        if (order == null || task == null || !Objects.equals(workorderId, task.workorderId)
                || (!manages(order, actor) && !Objects.equals(actor, task.assignedUserId))) {
            throw ExecutionException.notFound();
        }
    }

    public void requireUnitQuery(Long workorderId, Long snId) {
        requirePermission("query");
        ExecutionOwnershipService.UnitScope unit = ownership.unit(snId);
        if (unit == null || !Objects.equals(workorderId, unit.workorderId)) throw ExecutionException.notFound();
        requireTaskQuery(workorderId, unit.currentTaskId);
    }

    public void requireTrace(Long workorderId, Long snId) {
        requirePermission("trace");
        requireUnitQuery(workorderId, snId);
    }

    private boolean manages(ExecutionOwnershipService.OrderScope order, Long actor) {
        if (actor == null) return false;
        return permissions.hasPermi(PREFIX + "manageAll")
                || (order.releaseUserId != null ? actor.equals(order.releaseUserId)
                    : Objects.equals(SecurityUtils.getUsername(), order.creator));
    }

    private void requirePermission(String suffix) {
        if (!permissions.hasPermi(PREFIX + suffix)) throw ExecutionException.forbidden();
    }
}
