package com.ktg.mes.pro.execution;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.ktg.common.core.domain.AjaxResult;
import com.ktg.common.utils.SecurityUtils;
import com.ktg.mes.pro.execution.mapper.ExecutionOperationMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.transaction.support.TransactionTemplate;

import java.util.*;
import java.util.function.Supplier;

/** Runs one endpoint-specific mutation and records a receipt in the same transaction. */
@Service
public class ExecutionOperationService {
    private static final Logger log = LoggerFactory.getLogger(ExecutionOperationService.class);
    private static final Set<String> RESULT_IDS = new HashSet<>(Arrays.asList(
            "feedbackId", "inspectionId", "ipqcId", "dispositionId", "createdTaskId",
            "consumeId", "produceLineId", "assignedUserId", "packageLineId", "version", "roundNo"));
    private static final Set<String> RESULT_COUNTS = new HashSet<>(Arrays.asList(
            "quantity", "snCount", "allocated", "assembled", "qualified", "packed", "scrapped", "taskCount"));
    private final ExecutionOperationMapper mapper;
    private final ExecutionAccessService access;
    private final PlatformTransactionManager transactions;
    private final ObjectMapper json;

    public ExecutionOperationService(ExecutionOperationMapper mapper, ExecutionAccessService access,
                                     PlatformTransactionManager transactions, ObjectMapper json) {
        this.mapper = mapper;
        this.access = access;
        this.transactions = transactions;
        this.json = json;
    }

    /**
     * Entry at a transaction boundary; caller supplies a validated DTO (converted to JSON)
     * and a trusted business mutation that joins this transaction, never commits independently.
     * Further task/SN/stock row locks and state eligibility belong to the endpoint-specific mutation.
     */
    public AjaxResult execute(String operationId, ExecutionAction action, Long orderId,
                              Long taskId, Long snId, JsonNode validatedPayload, Supplier<ObjectNode> mutation) {
        try {
            requireBoundary();
            if (!validId(operationId) || action == null || !positive(orderId)
                    || (taskId != null && !positive(taskId)) || (snId != null && !positive(snId)) || mutation == null)
                throw ExecutionException.validation("操作 ID 或目标无效");
            Long actorId = currentActor();
            String digest = new ExecutionRequestDigest(json).digest(action, orderId, taskId, snId, validatedPayload);
            try {
                return transaction().execute(status -> {
                    if (mapper.lockWorkorder(orderId) == null) throw ExecutionException.notFound();
                    // Replay must not check an already consumed predecessor state.
                    ExecutionOperation previous = mapper.selectById(operationId);
                    if (previous != null) return replay(previous, action, orderId, actorId, digest);
                    access.requireOperation(action, orderId, taskId, snId);
                    try {
                        if (mapper.insertClaim(new ExecutionOperation(operationId, action.name(), orderId, actorId, digest)) != 1)
                            throw new IllegalStateException("Operation claim not inserted");
                    } catch (DuplicateKeyException racedClaim) {
                        // Leave the losing transaction before reading the winner.
                        throw new ClaimRace(racedClaim);
                    }
                    ObjectNode result = boundedResult(taskId, snId, mutation.get());
                    if (mapper.complete(operationId, result.toString()) != 1)
                        throw new IllegalStateException("Operation receipt not completed");
                    return receipt(mapper.selectById(operationId));
                });
            } catch (ClaimRace race) {
                return transaction().execute(status -> {
                    ExecutionOperation committed = mapper.selectById(operationId);
                    if (committed == null) throw ExecutionException.resultUnknown();
                    return replay(committed, action, orderId, actorId, digest);
                });
            }
        } catch (ExecutionException rejection) {
            return rejection.toAjaxResult();
        } catch (RuntimeException unexpected) {
            log.warn("Execution operation outcome unresolved: operationId={}", validId(operationId) ? operationId : "(invalid)", unexpected);
            return ExecutionException.resultUnknown().toAjaxResult();
        }
    }

    /** GET recovery across sessions; unrestricted operation IDs never expose another actor's facts. */
    public AjaxResult result(String operationId) {
        try {
            requireBoundary();
            if (!validId(operationId)) throw ExecutionException.notFound();
            Long actorId;
            try { actorId = currentActor(); }
            catch (ExecutionException denied) { throw ExecutionException.notFound(); }
            return transaction().execute(status -> {
                ExecutionOperation receipt = mapper.selectById(operationId);
                if (receipt == null || !actorId.equals(receipt.getActorId())) throw ExecutionException.notFound();
                authorizeReceipt(receipt);
                return receipt(receipt);
            });
        } catch (ExecutionException rejection) {
            return rejection.toAjaxResult();
        } catch (RuntimeException unexpected) {
            log.warn("Execution receipt read unresolved: operationId={}", validId(operationId) ? operationId : "(invalid)", unexpected);
            return ExecutionException.resultUnknown().toAjaxResult();
        }
    }

    private AjaxResult replay(ExecutionOperation previous, ExecutionAction action, Long orderId,
                              Long actorId, String digest) {
        if (!actorId.equals(previous.getActorId()) || !action.name().equals(previous.getActionType())
                || !orderId.equals(previous.getWorkorderId()) || !digest.equals(previous.getRequestDigest()))
            throw ExecutionException.operationConflict();
        authorizeReceipt(previous);
        return receipt(previous);
    }

    private void authorizeReceipt(ExecutionOperation operation) {
        JsonNode reference = readRef(operation);
        try {
            access.requireReceipt(ExecutionAction.valueOf(operation.getActionType()), operation.getWorkorderId(),
                    refId(reference, "taskId"), refId(reference, "snId"));
        } catch (ExecutionException denied) {
            throw ExecutionException.notFound();
        }
    }

    private AjaxResult receipt(ExecutionOperation operation) {
        JsonNode ref = readRef(operation);
        Map<String, Object> response = new LinkedHashMap<>();
        response.put("operationId", operation.getOperationId());
        response.put("actionType", operation.getActionType());
        response.put("workorderId", operation.getWorkorderId());
        response.put("committedAt", operation.getCommittedAt());
        response.put("resultRef", ref);
        return AjaxResult.success(response);
    }

    private JsonNode readRef(ExecutionOperation operation) {
        if (operation == null || operation.getCommittedAt() == null || operation.getResultRef() == null)
            throw ExecutionException.resultUnknown();
        try {
            JsonNode value = json.readTree(operation.getResultRef());
            if (value == null || !value.isObject() || value.path("schemaVersion").asInt() != 1)
                throw new IllegalArgumentException("Invalid result reference");
            return value;
        } catch (Exception invalid) {
            throw ExecutionException.resultUnknown();
        }
    }

    private ObjectNode boundedResult(Long taskId, Long snId, ObjectNode supplied) {
        if (supplied == null) throw new IllegalStateException("Missing execution result");
        ObjectNode ref = json.createObjectNode().put("schemaVersion", 1);
        if (taskId != null) ref.put("taskId", taskId);
        if (snId != null) ref.put("snId", snId);
        supplied.fields().forEachRemaining(entry -> {
            String key = entry.getKey();
            JsonNode value = entry.getValue();
            boolean nonnegative = value.isIntegralNumber() && value.canConvertToLong() && value.longValue() >= 0;
            if (RESULT_IDS.contains(key) && nonnegative && ("version".equals(key) || value.longValue() > 0)) {
                ref.put(key, value.longValue());
            } else if (RESULT_COUNTS.contains(key) && nonnegative) {
                ref.put(key, value.longValue());
            } else if ("taskIds".equals(key) && value.isArray() && value.size() <= 3) {
                TreeSet<Long> ids = new TreeSet<>();
                for (JsonNode item : value) {
                    if (!item.isIntegralNumber() || !item.canConvertToLong() || !positive(item.longValue())
                            || !ids.add(item.longValue())) throw new IllegalStateException("Invalid task references");
                }
                ids.forEach(ref.putArray("taskIds")::add);
            } else if ("state".equals(key) && value.isTextual() && isKnownState(value.asText())) {
                ref.put(key, value.asText());
            } else {
                throw new IllegalStateException("Unsupported result reference field");
            }
        });
        if (ref.toString().length() > 4096) throw new IllegalStateException("Result reference too large");
        return ref;
    }

    private boolean isKnownState(String value) {
        return Arrays.stream(ExecutionState.values()).anyMatch(state -> state.name().equals(value))
                || Arrays.stream(TaskExecutionState.values()).anyMatch(state -> state.name().equals(value))
                || Arrays.stream(WorkorderExecutionState.values()).anyMatch(state -> state.name().equals(value));
    }

    private Long refId(JsonNode ref, String key) {
        JsonNode value = ref.get(key);
        if (value == null) return null;
        if (!value.isIntegralNumber() || !value.canConvertToLong() || !positive(value.longValue()))
            throw ExecutionException.resultUnknown();
        return value.longValue();
    }

    private Long currentActor() {
        try {
            Long id = SecurityUtils.getUserId();
            if (positive(id)) return id;
        } catch (RuntimeException denied) {
            // Do not accept actor IDs from DTOs or silently default to the administrator.
        }
        throw ExecutionException.forbidden();
    }

    private boolean positive(Long id) { return id != null && id > 0; }

    private boolean validId(String id) {
        try { return id != null && UUID.fromString(id).toString().equals(id); }
        catch (IllegalArgumentException invalid) { return false; }
    }

    private TransactionTemplate transaction() { return new TransactionTemplate(transactions); }

    private void requireBoundary() {
        if (TransactionSynchronizationManager.isActualTransactionActive())
            throw new IllegalStateException("ExecutionOperationService must own transaction boundary");
    }

    private static final class ClaimRace extends RuntimeException {
        private ClaimRace(DuplicateKeyException cause) { super(cause); }
    }
}
