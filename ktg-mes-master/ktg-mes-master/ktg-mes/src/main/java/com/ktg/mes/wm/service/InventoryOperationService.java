package com.ktg.mes.wm.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.ktg.common.core.domain.AjaxResult;
import com.ktg.common.constant.UserConstants;
import com.ktg.common.utils.SecurityUtils;
import com.ktg.mes.wm.domain.InventoryOperationReceipt;
import com.ktg.mes.wm.domain.tx.IssueTxBean;
import com.ktg.mes.wm.domain.tx.ItemRecptTxBean;
import com.ktg.mes.wm.mapper.InventoryOperationMapper;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.dao.PessimisticLockingFailureException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.nio.charset.StandardCharsets;
import java.math.BigDecimal;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.OffsetDateTime;
import java.time.Duration;
import java.time.LocalDateTime;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.HashSet;
import java.util.Set;
import java.util.List;
import java.util.Map;
import java.util.Locale;
import java.util.UUID;

/**
 * A reservation is durable before execution. The document claim, inventory
 * changes, ledger links, document status and success receipt commit together.
 */
@Service
public class InventoryOperationService {
    private static final Logger log = LoggerFactory.getLogger(InventoryOperationService.class);
    private static final String RECEIPT = "ITEM_RECEIPT_EXECUTE";
    private static final String ISSUE = "PRODUCTION_ISSUE_EXECUTE";

    @Autowired private InventoryOperationMapper mapper;
    @Autowired private IWmItemRecptService itemRecptService;
    @Autowired private IWmIssueHeaderService issueHeaderService;
    @Autowired private IStorageCoreService storageCoreService;
    @Autowired private ObjectMapper objectMapper;
    @Autowired private PlatformTransactionManager transactionManager;
    @Autowired private InventoryDocumentGuard documentGuard;

    public AjaxResult executeReceipt(Long documentId, String operationId) {
        return execute(RECEIPT, documentId, operationId, null, null);
    }

    /** A read-only proposal. Execution always rechecks the document version under its row lock. */
    public AjaxResult previewReceipt(Long documentId) {
        if (documentId == null || documentId <= 0) {
            return error(422, "INVALID_DOCUMENT_ID", "单据 ID 无效", null);
        }
        Map<String, Object> document = mapper.readReceiptDocument(documentId);
        if (document == null) return error(404, "DOCUMENT_NOT_FOUND", "入库单不存在", null);
        if (!"APPROVED".equals(document.get("status"))) {
            return error(409, "DOCUMENT_STATE_CONFLICT", "入库单状态不允许执行", null);
        }
        if (blank((String) document.get("document_code"))) {
            return error(422, "INVALID_DOCUMENT_LINES", "单据编号不能为空", null);
        }
        if (mapper.executionOwner(RECEIPT, "IR", documentId) != null
                || !mapper.transactions("IR", documentId).isEmpty()) {
            return error(409, "DOCUMENT_ALREADY_EXECUTED", "入库单已经执行", null);
        }
        List<ItemRecptTxBean> lines = itemRecptService.getTxBeans(documentId);
        try {
            validateReceiptLines(documentId, lines);
        } catch (InventoryOperationException ex) {
            return error(422, ex.code, ex.getMessage(), null);
        }
        Map<String, Object> proposal = new LinkedHashMap<>();
        proposal.put("documentId", documentId);
        proposal.put("documentCode", document.get("document_code"));
        proposal.put("documentStatus", document.get("status"));
        proposal.put("documentVersion", document.get("version"));
        List<Map<String, Object>> additions = new java.util.ArrayList<>();
        for (ItemRecptTxBean line : lines) {
            Map<String, Object> addition = new LinkedHashMap<>();
            addition.put("sourceDocLineId", line.getSourceDocLineId());
            addition.put("itemId", line.getItemId());
            addition.put("itemCode", line.getItemCode());
            addition.put("itemName", line.getItemName());
            addition.put("quantity", line.getTransactionQuantity());
            addition.put("unitOfMeasure", line.getUnitOfMeasure());
            addition.put("batchCode", line.getBatchCode());
            addition.put("warehouseId", line.getWarehouseId());
            addition.put("warehouseName", line.getWarehouseName());
            addition.put("locationId", line.getLocationId());
            addition.put("locationName", line.getLocationName());
            addition.put("areaId", line.getAreaId());
            addition.put("areaName", line.getAreaName());
            additions.add(addition);
        }
        proposal.put("additions", additions);
        return AjaxResult.success(proposal);
    }

    /** Validate the data needed by inventory execution before a receipt is approved. */
    public AjaxResult validateReceiptForApproval(Long documentId) {
        if (documentId == null || documentId <= 0) {
            return error(422, "INVALID_DOCUMENT_ID", "单据 ID 无效", null);
        }
        try {
            validateReceiptLines(documentId, itemRecptService.getTxBeans(documentId));
            return AjaxResult.success();
        } catch (InventoryOperationException ex) {
            return error(422, ex.code, ex.getMessage(), null);
        }
    }

    /** Reopen only a not-yet-executed approved receipt whose lines fail execution validation. */
    public AjaxResult reopenReceiptForPutaway(Long documentId, Long expectedVersion) {
        if (documentId == null || documentId <= 0 || expectedVersion == null || expectedVersion < 0) {
            return error(422, "INVALID_DOCUMENT_VERSION", "单据 ID 或版本无效", null);
        }
        return transaction().execute(status -> {
            documentGuard.lockEditable("IR", documentId, expectedVersion);
            Map<String, Object> document = mapper.lockDocument("IR", documentId);
            if (document == null) return error(404, "DOCUMENT_NOT_FOUND", "入库单不存在", null);
            if (!"APPROVED".equals(document.get("status"))) {
                return error(409, "DOCUMENT_STATE_CONFLICT", "只有未执行的已审批入库单可以退回补录", null);
            }
            if (mapper.executionOwner(RECEIPT, "IR", documentId) != null
                    || !mapper.transactions("IR", documentId).isEmpty()) {
                return error(409, "DOCUMENT_ALREADY_EXECUTED", "存在库存执行记录，不能退回补录", null);
            }
            List<ItemRecptTxBean> lines = itemRecptService.getTxBeans(documentId);
            if (lines == null || lines.isEmpty()) {
                return error(409, "INVALID_DOCUMENT_LINES", "入库单没有明细，不能退回补录", null);
            }
            try {
                validateReceiptLines(documentId, lines);
                return error(409, "DOCUMENT_LINES_VALID", "入库明细完整，无需退回补录", null);
            } catch (InventoryOperationException invalid) {
                if (mapper.reopenReceiptForPutaway(documentId, expectedVersion) != 1) {
                    return error(409, "DOCUMENT_VERSION_CONFLICT", "单据版本已变化，请重新加载", null);
                }
                Map<String, Object> data = new LinkedHashMap<>();
                data.put("documentId", documentId);
                data.put("documentStatus", "APPROVING");
                data.put("documentVersion", expectedVersion + 1);
                data.put("message", invalid.getMessage());
                return AjaxResult.success(data);
            }
        });
    }

    public AjaxResult previewIssue(Long documentId) {
        if (documentId == null || documentId <= 0) {
            return error(422, "INVALID_DOCUMENT_ID", "单据 ID 无效", null);
        }
        Map<String, Object> document = mapper.readIssueDocument(documentId);
        if (document == null) return error(404, "DOCUMENT_NOT_FOUND", "领料单不存在", null);
        if (!"APPROVED".equals(document.get("status"))) {
            return error(409, "DOCUMENT_STATE_CONFLICT", "领料单状态不允许执行", null);
        }
        if (blank((String) document.get("document_code"))) {
            return error(422, "INVALID_DOCUMENT_LINES", "单据编号不能为空", null);
        }
        if (mapper.executionOwner(ISSUE, "ISSUE", documentId) != null
                || !mapper.transactions("ISSUE", documentId).isEmpty()) {
            return error(409, "DOCUMENT_ALREADY_EXECUTED", "领料单已经执行", null);
        }
        List<IssueTxBean> lines = issueHeaderService.getTxBeans(documentId);
        Map<String, Object> target = mapper.issueTarget();
        try {
            validateIssueLines(documentId, lines);
            requireIssueTarget(target);
            requirePhysicalIssueSource(lines, target);
        } catch (InventoryOperationException ex) {
            return error("INSUFFICIENT_STOCK".equals(ex.code) ? 409 : 422,
                    ex.code, ex.getMessage(), null);
        }
        Map<String, Object> proposal = new LinkedHashMap<>();
        proposal.put("documentId", documentId);
        proposal.put("documentCode", document.get("document_code"));
        proposal.put("documentStatus", document.get("status"));
        proposal.put("documentVersion", document.get("version"));
        List<Map<String, Object>> movements = new java.util.ArrayList<>();
        for (IssueTxBean line : lines) {
            Map<String, Object> movement = new LinkedHashMap<>();
            movement.put("sourceDocLineId", line.getSourceDocLineId());
            movement.put("materialStockId", line.getMaterialStockId());
            movement.put("itemId", line.getItemId());
            movement.put("itemCode", line.getItemCode());
            movement.put("itemName", line.getItemName());
            movement.put("quantity", line.getTransactionQuantity());
            movement.put("unitOfMeasure", line.getUnitOfMeasure());
            movement.put("batchCode", line.getBatchCode());
            movement.put("availableQuantity", line.getAvailableQuantity());
            movement.put("sourceWarehouseId", line.getWarehouseId());
            movement.put("sourceWarehouseName", line.getWarehouseName());
            movement.put("sourceLocationId", line.getLocationId());
            movement.put("sourceLocationName", line.getLocationName());
            movement.put("sourceAreaId", line.getAreaId());
            movement.put("sourceAreaName", line.getAreaName());
            movement.put("target", target);
            movements.add(movement);
        }
        proposal.put("movements", movements);
        return AjaxResult.success(proposal);
    }

    public AjaxResult receiptResult(Long documentId, String operationId) {
        return documentResult(RECEIPT, "IR", documentId, operationId);
    }

    public AjaxResult issueResult(Long documentId, String operationId) {
        return documentResult(ISSUE, "ISSUE", documentId, operationId);
    }

    /** Read a committed receipt by ID. A caller cannot use a guessed ID to inspect another actor's work. */
    public AjaxResult operationResult(String operationId) {
        InventoryOperationReceipt receipt = findVisible(operationId);
        if (receipt == null) return error(404, "OPERATION_NOT_FOUND", "库存操作不存在", null);
        AjaxResult checked = documentResult(receipt.getActionType(), receipt.getDocumentType(),
                receipt.getDocumentId(), operationId);
        Object value = checked.get("data");
        if (!(value instanceof Map)) return checked;
        @SuppressWarnings("unchecked")
        Map<String, Object> data = new LinkedHashMap<>((Map<String, Object>) value);
        if ("executing".equals(data.get("operationStatus"))) {
            data.put("operationStatus", "reconciling");
        }
        data.put("safeToRetry", false);
        data.put("verifiedAt", OffsetDateTime.now().toString());
        Map<String, Object> document = RECEIPT.equals(receipt.getActionType())
                ? mapper.readReceiptDocument(receipt.getDocumentId())
                : mapper.readIssueDocument(receipt.getDocumentId());
        data.put("documentStatus", document == null ? null : document.get("status"));
        LocalDateTime end = receipt.getCompletedAt() == null ? LocalDateTime.now() : receipt.getCompletedAt();
        long elapsedMs = receipt.getCreatedAt() == null ? -1L
                : Duration.between(receipt.getCreatedAt(), end).toMillis();
        log.info("Inventory result operationId={} documentType={} documentId={} transactionIds={} "
                        + "initiatorId={} elapsedMs={} classification={}",
                operationId, receipt.getDocumentType(), receipt.getDocumentId(),
                transactionIds(data), receipt.getExecutedById(), elapsedMs, data.get("operationStatus"));
        return AjaxResult.success(data);
    }

    private List<Object> transactionIds(Map<String, Object> data) {
        List<Object> ids = new java.util.ArrayList<>();
        Object value = data.get("transactions");
        if (value instanceof List) {
            for (Object item : (List<?>) value) {
                if (item instanceof Map) ids.add(((Map<?, ?>) item).get("transactionId"));
            }
        }
        return ids;
    }

    /** The one-time claim is authoritative; absence of a claim is not proof that retry is safe. */
    public AjaxResult documentExecutionFact(String actionType, Long documentId) {
        if ((!RECEIPT.equals(actionType) && !ISSUE.equals(actionType))
                || documentId == null || documentId <= 0) {
            return error(422, "INVALID_DOCUMENT_ID", "单据或动作无效", null);
        }
        String type = RECEIPT.equals(actionType) ? "IR" : "ISSUE";
        Map<String, Object> document = RECEIPT.equals(actionType)
                ? mapper.readReceiptDocument(documentId) : mapper.readIssueDocument(documentId);
        if (document == null) return error(404, "DOCUMENT_NOT_FOUND", "单据不存在", null);
        InventoryOperationReceipt owner = mapper.findByDocument(actionType, type, documentId);
        if (owner != null) return operationResult(owner.getOperationId());
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("actionType", actionType);
        data.put("documentType", type);
        data.put("documentId", documentId);
        data.put("documentStatus", document.get("status"));
        data.put("operationStatus", "unknown");
        data.put("safeToRetry", false);
        data.put("verifiedAt", OffsetDateTime.now().toString());
        return AjaxResult.success(data);
    }

    private InventoryOperationReceipt findVisible(String operationId) {
        if (operationId == null || operationId.length() > 36) return null;
        InventoryOperationReceipt receipt = mapper.find(operationId);
        return receipt != null && SecurityUtils.getUserId().equals(receipt.getExecutedById())
                ? receipt : null;
    }

    private AjaxResult documentResult(String actionType, String documentType, Long documentId, String operationId) {
        if (documentId == null || operationId == null) {
            return error(422, "INVALID_OPERATION_ID", "操作 ID 无效", null);
        }
        InventoryOperationReceipt receipt = findVisible(operationId);
        if (receipt == null || !actionType.equals(receipt.getActionType())
                || !documentId.equals(receipt.getDocumentId())) {
            return error(404, "OPERATION_NOT_FOUND", "库存操作不存在", null);
        }
        if ("succeeded".equals(receipt.getOperationStatus())) {
            try {
                Map<String, Object> document = RECEIPT.equals(actionType)
                        ? mapper.readReceiptDocument(documentId) : mapper.readIssueDocument(documentId);
                List<Map<String, Object>> transactions = mapper.transactions(documentType, documentId);
                List<?> recorded = receipt.getResultJson() == null ? null
                        : (List<?>) parse(receipt.getResultJson()).get("transactions");
                Set<Long> actualIds = new HashSet<>();
                for (Map<String, Object> transaction : transactions) {
                    actualIds.add(((Number) transaction.get("transactionId")).longValue());
                }
                Set<Long> recordedIds = new HashSet<>();
                if (recorded != null) {
                    for (Object item : recorded) {
                        recordedIds.add(((Number) ((Map<?, ?>) item).get("transactionId")).longValue());
                    }
                }
                if (document == null || !"FINISHED".equals(document.get("status"))
                        || recorded == null || transactions.isEmpty()
                        || transactions.size() != mapper.linkedTransactionCount(operationId)
                        || transactions.size() != recorded.size() || !actualIds.equals(recordedIds)
                        || !actualIds.equals(new HashSet<>(mapper.linkedTransactionIds(operationId)))) {
                    return error(202, "RESULT_UNKNOWN", "库存结果待核实", baseResult(receipt, "unknown"));
                }
            } catch (RuntimeException ex) {
                log.error("Inventory receipt {} could not be verified", operationId, ex);
                return error(202, "RESULT_UNKNOWN", "库存结果待核实", baseResult(receipt, "unknown"));
            }
        }
        return replayOrConflict(receipt, receipt, true);
    }

    public AjaxResult executeReceipt(Long documentId, String operationId, Long expectedVersion) {
        return execute(RECEIPT, documentId, operationId, null, expectedVersion);
    }

    public AjaxResult executeIssue(Long documentId, String operationId) {
        return execute(ISSUE, documentId, operationId, null, null);
    }

    public AjaxResult executeIssue(Long documentId, String operationId, Long expectedVersion) {
        return execute(ISSUE, documentId, operationId, null, expectedVersion);
    }

    public AjaxResult execute(String actionType, Long documentId, String operationId, String expectedStatus) {
        return execute(actionType, documentId, operationId, expectedStatus, null);
    }

    public AjaxResult execute(String actionType, Long documentId, String operationId,
                              String expectedStatus, Long expectedVersion) {
        final String normalizedExpectedStatus = expectedStatus == null || expectedStatus.trim().isEmpty()
                ? null : expectedStatus.trim().toUpperCase(Locale.ROOT);
        if (!RECEIPT.equals(actionType) && !ISSUE.equals(actionType)) {
            return error(422, "UNSUPPORTED_ACTION", "不支持的库存动作", null);
        }
        if (documentId == null || documentId <= 0) {
            return error(422, "INVALID_DOCUMENT_ID", "单据 ID 无效", null);
        }
        if (expectedVersion != null && expectedVersion < 0) {
            return error(422, "INVALID_DOCUMENT_VERSION", "单据版本无效", null);
        }
        if (operationId == null || operationId.trim().isEmpty()) {
            operationId = UUID.randomUUID().toString();
        } else {
            try {
                operationId = UUID.fromString(operationId.trim()).toString();
            } catch (IllegalArgumentException ex) {
                return error(422, "INVALID_OPERATION_ID", "操作 ID 必须是 UUID", null);
            }
        }
        String documentType = RECEIPT.equals(actionType) ? "IR" : "ISSUE";
        InventoryOperationReceipt receipt = new InventoryOperationReceipt();
        receipt.setOperationId(operationId);
        receipt.setActionType(actionType);
        receipt.setDocumentType(documentType);
        receipt.setDocumentId(documentId);
        receipt.setRequestDigest(digest(actionType, documentType, documentId,
                normalizedExpectedStatus, expectedVersion));
        receipt.setExecutedById(SecurityUtils.getUserId());
        receipt.setExecutedByName(SecurityUtils.getUsername());

        final String stableOperationId = operationId;
        try {
            transaction().execute(status -> {
                mapper.reserve(receipt);
                return null;
            });
        } catch (DuplicateKeyException ex) {
            return replayOrConflict(mapper.find(stableOperationId), receipt, true);
        }

        for (int attempt = 1; attempt <= 3; attempt++) {
            try {
                transaction().execute(status -> {
                    executeReserved(receipt, normalizedExpectedStatus, expectedVersion);
                    return null;
                });
                log.info("Inventory execution operationId={} documentType={} documentId={} "
                                + "initiatorId={} classification=succeeded",
                        stableOperationId, documentType, documentId, receipt.getExecutedById());
                return replayOrConflict(mapper.find(stableOperationId), receipt, false);
            } catch (PessimisticLockingFailureException ex) {
                if (attempt == 3) {
                    log.error("Inventory operation {} exhausted lock retries", stableOperationId, ex);
                    return persistFailure(receipt, "failed", "EXECUTION_FAILED", "库存执行失败", null);
                }
                log.warn("Inventory operation {} retrying after lock conflict (attempt {})",
                        stableOperationId, attempt);
                try {
                    Thread.sleep(25L * attempt);
                } catch (InterruptedException interrupted) {
                    Thread.currentThread().interrupt();
                    return persistFailure(receipt, "failed", "EXECUTION_FAILED", "库存执行失败", null);
                }
            } catch (DuplicateKeyException ex) {
                String owner = mapper.executionOwner(actionType, documentType, documentId);
                if (owner != null && !owner.equals(stableOperationId)) {
                    return persistFailure(receipt, "rejected", "DOCUMENT_ALREADY_EXECUTED",
                            "单据已经执行", owner);
                }
                log.error("Inventory operation {} hit an unrelated unique-key conflict", stableOperationId, ex);
                return persistFailure(receipt, "failed", "EXECUTION_FAILED", "库存执行失败", null);
            } catch (InventoryOperationException ex) {
                return persistFailure(receipt,
                        "LEDGER_MISMATCH".equals(ex.code) || "INSUFFICIENT_STOCK".equals(ex.code)
                                ? "failed" : "rejected",
                        ex.code, ex.getMessage(), null);
            } catch (InsufficientStockException ex) {
                return persistFailure(receipt, "failed", "INSUFFICIENT_STOCK", ex.getMessage(), null);
            } catch (RuntimeException ex) {
                log.error("Inventory operation {} failed and was rolled back", stableOperationId, ex);
                return persistFailure(receipt, "failed", "EXECUTION_FAILED", "库存执行失败", null);
            }
        }
        throw new IllegalStateException("Unreachable inventory retry state");
    }

    private void executeReserved(InventoryOperationReceipt receipt, String expectedStatus, Long expectedVersion) {
        InventoryOperationReceipt locked = mapper.lock(receipt.getOperationId());
        if (!"executing".equals(locked.getOperationStatus())) {
            throw new InventoryOperationException("RECEIPT_COMMIT_CONFLICT", "回执状态已改变");
        }
        mapper.claimDocument(receipt);
        Map<String, Object> document = mapper.lockDocument(receipt.getDocumentType(), receipt.getDocumentId());
        if (document == null) {
            throw new InventoryOperationException("DOCUMENT_NOT_FOUND", "单据不存在");
        }
        String status = String.valueOf(document.get("status"));
        if (!"APPROVED".equals(status) || (expectedStatus != null && !expectedStatus.equals(status))) {
            throw new InventoryOperationException("DOCUMENT_STATE_CONFLICT", "单据状态不允许执行");
        }
        if (expectedVersion != null && expectedVersion.longValue() != ((Number) document.get("version")).longValue()) {
            throw new InventoryOperationException("DOCUMENT_VERSION_CONFLICT", "单据版本已变化，请重新预览");
        }
        if (blank((String) document.get("document_code"))) {
            throw new InventoryOperationException("INVALID_DOCUMENT_LINES", "单据编号不能为空");
        }
        if (!mapper.transactions(receipt.getDocumentType(), receipt.getDocumentId()).isEmpty()) {
            throw new InventoryOperationException("DOCUMENT_ALREADY_EXECUTED", "已有库存流水的单据不能重新执行");
        }

        int expectedTransactionCount;
        Map<Long, BigDecimal> expectedQuantities = new LinkedHashMap<>();
        Map<Long, String> expectedUnits = new LinkedHashMap<>();
        Map<Long, IssueTxBean> issueLines = new LinkedHashMap<>();
        Map<String, Object> issueTarget = null;
        if (RECEIPT.equals(receipt.getActionType())) {
            List<ItemRecptTxBean> lines = itemRecptService.getTxBeans(receipt.getDocumentId());
            validateReceiptLines(receipt.getDocumentId(), lines);
            for (ItemRecptTxBean line : lines) {
                if (expectedQuantities.put(line.getSourceDocLineId(), line.getTransactionQuantity()) != null)
                    throw new InventoryOperationException("INVALID_DOCUMENT_LINES", "入库单明细 ID 重复");
                expectedUnits.put(line.getSourceDocLineId(), line.getUnitOfMeasure());
            }
            expectedTransactionCount = lines.size();
            storageCoreService.processItemRecpt(lines);
        } else {
            List<IssueTxBean> lines = issueHeaderService.getTxBeans(receipt.getDocumentId());
            validateIssueLines(receipt.getDocumentId(), lines);
            issueTarget = mapper.issueTarget();
            requireIssueTarget(issueTarget);
            requirePhysicalIssueSource(lines, issueTarget);
            for (IssueTxBean line : lines) {
                expectedQuantities.put(line.getSourceDocLineId(), line.getTransactionQuantity());
                expectedUnits.put(line.getSourceDocLineId(), line.getUnitOfMeasure());
                issueLines.put(line.getSourceDocLineId(), line);
            }
            expectedTransactionCount = lines.size() * 2;
            storageCoreService.processIssue(lines);
        }

        List<Map<String, Object>> transactions = mapper.transactions(
                receipt.getDocumentType(), receipt.getDocumentId());
        if (transactions.size() != expectedTransactionCount) {
            throw new InventoryOperationException("LEDGER_MISMATCH", "库存流水数量与单据明细不符");
        }
        verifyLedger(receipt.getActionType(), transactions, expectedQuantities, expectedUnits);
        if (ISSUE.equals(receipt.getActionType())) {
            verifyIssueLedger(transactions, issueLines, issueTarget);
        }
        if (mapper.finishDocument(receipt.getDocumentType(), receipt.getDocumentId()) != 1) {
            throw new InventoryOperationException("DOCUMENT_STATE_CONFLICT", "单据状态已改变");
        }
        for (int i = 0; i < transactions.size(); i++) {
            Number id = (Number) transactions.get(i).get("transactionId");
            mapper.linkTransaction(receipt.getOperationId(), id.longValue(), i + 1);
        }
        Map<String, Object> result = baseResult(receipt, "succeeded");
        result.put("documentCode", document.get("document_code"));
        result.put("documentStatus", "FINISHED");
        result.put("executedBy", actor(receipt));
        result.put("completedAt", OffsetDateTime.now().toString());
        result.put("transactions", transactions);
        if (mapper.complete(receipt.getOperationId(), "succeeded", null, json(result)) != 1) {
            throw new InventoryOperationException("RECEIPT_COMMIT_CONFLICT", "回执状态已改变");
        }
    }

    private AjaxResult persistFailure(InventoryOperationReceipt receipt, String status,
                                      String code, String message, String existingOperationId) {
        Map<String, Object> result = baseResult(receipt, status);
        result.put("message", message);
        int httpCode = "INSUFFICIENT_STOCK".equals(code) ? 409
                : "failed".equals(status) ? 500
                : "INVALID_DOCUMENT_LINES".equals(code) || "INVALID_ISSUE_TARGET".equals(code) ? 422
                : "DOCUMENT_NOT_FOUND".equals(code) ? 404 : 409;
        result.put("responseCode", httpCode);
        if (existingOperationId != null) {
            result.put("existingOperationId", existingOperationId);
        }
        transaction().execute(tx -> {
            if (mapper.complete(receipt.getOperationId(), status, code, json(result)) != 1) {
                throw new IllegalStateException("Inventory operation receipt is no longer executing");
            }
            return null;
        });
        log.info("Inventory execution operationId={} documentType={} documentId={} "
                        + "initiatorId={} classification={} errorCode={}",
                receipt.getOperationId(), receipt.getDocumentType(), receipt.getDocumentId(),
                receipt.getExecutedById(), status, code);
        return error(httpCode, code, message, result);
    }

    private AjaxResult replayOrConflict(InventoryOperationReceipt existing,
                                        InventoryOperationReceipt request, boolean replayed) {
        if (existing == null) {
            return error(202, "RESULT_UNKNOWN", "执行结果待核实", baseResult(request, "unknown"));
        }
        if (!existing.getRequestDigest().equals(request.getRequestDigest())) {
            return error(409, "OPERATION_ID_CONFLICT", "操作 ID 对应另一请求",
                    baseResult(existing, "rejected"));
        }
        Map<String, Object> result;
        try {
            result = existing.getResultJson() == null
                    ? baseResult(existing, existing.getOperationStatus()) : parse(existing.getResultJson());
        } catch (RuntimeException ex) {
            log.error("Inventory receipt {} has unreadable result", existing.getOperationId(), ex);
            return error(202, "RESULT_UNKNOWN", "库存结果待核实", baseResult(existing, "unknown"));
        }
        result.put("replayed", replayed);
        if ("succeeded".equals(existing.getOperationStatus())) {
            return AjaxResult.success(result);
        }
        if ("executing".equals(existing.getOperationStatus())) {
            return error(202, "EXECUTING", "操作正在执行", result);
        }
        Number responseCode = (Number) result.get("responseCode");
        return error(responseCode == null ? 500 : responseCode.intValue(),
                existing.getErrorCode(), String.valueOf(result.get("message")), result);
    }

    private TransactionTemplate transaction() {
        return new TransactionTemplate(transactionManager);
    }

    private Map<String, Object> baseResult(InventoryOperationReceipt receipt, String status) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("operationId", receipt.getOperationId());
        result.put("operationStatus", status);
        result.put("actionType", receipt.getActionType());
        result.put("documentType", receipt.getDocumentType());
        result.put("documentId", receipt.getDocumentId());
        return result;
    }

    private Map<String, Object> actor(InventoryOperationReceipt receipt) {
        Map<String, Object> actor = new LinkedHashMap<>();
        actor.put("userId", receipt.getExecutedById());
        actor.put("userName", receipt.getExecutedByName());
        return actor;
    }

    private String json(Map<String, Object> result) {
        try {
            return objectMapper.writeValueAsString(result);
        } catch (Exception ex) {
            throw new IllegalStateException("Cannot serialize inventory receipt", ex);
        }
    }

    private Map<String, Object> parse(String json) {
        try {
            return objectMapper.readValue(json, new TypeReference<Map<String, Object>>() {});
        } catch (Exception ex) {
            throw new IllegalStateException("Cannot read inventory receipt", ex);
        }
    }

    private boolean blank(String value) {
        return value == null || value.trim().isEmpty();
    }

    private void validateReceiptLines(Long documentId, List<ItemRecptTxBean> lines) {
        if (lines == null || lines.isEmpty()) {
            throw new InventoryOperationException("INVALID_DOCUMENT_LINES", "入库单没有有效明细");
        }
        Set<Long> ids = new HashSet<>();
        for (ItemRecptTxBean line : lines) {
            List<String> invalidFields = new java.util.ArrayList<>();
            if (line.getSourceDocLineId() == null || !ids.add(line.getSourceDocLineId())) invalidFields.add("明细 ID");
            if (line.getTransactionQuantity() == null || line.getTransactionQuantity().signum() <= 0
                    || line.getTransactionQuantity().scale() > 2) invalidFields.add("入库数量");
            if (line.getItemId() == null || line.getItemId() <= 0 || blank(line.getItemCode())) invalidFields.add("物料");
            if (blank(line.getUnitOfMeasure())) invalidFields.add("单位");
            if (blank(line.getBatchCode())) invalidFields.add("批次号");
            if (line.getWarehouseId() == null) invalidFields.add("目标仓库");
            if (line.getLocationId() == null) invalidFields.add("目标库位");
            if (line.getAreaId() == null) invalidFields.add("目标库区");
            if (!Boolean.TRUE.equals(line.getReferenceValid())) invalidFields.add("物料、单位或仓储引用无效");
            if (!documentId.equals(line.getSourceDocId()) || !"IR".equals(line.getSourceDocType())) invalidFields.add("来源单据");
            if (!invalidFields.isEmpty()) {
                String lineLabel = line.getSourceDocLineId() == null ? "未分配明细" : "明细 " + line.getSourceDocLineId();
                throw new InventoryOperationException("INVALID_DOCUMENT_LINES",
                        "入库单" + lineLabel + "缺少或无效字段：" + String.join("、", invalidFields));
            }
        }
    }

    private void validateIssueLines(Long documentId, List<IssueTxBean> lines) {
        if (lines == null || lines.isEmpty()) {
            throw new InventoryOperationException("INVALID_DOCUMENT_LINES", "领料单没有有效明细");
        }
        Set<Long> ids = new HashSet<>();
        Map<Long, BigDecimal> required = new LinkedHashMap<>();
        Map<Long, BigDecimal> available = new LinkedHashMap<>();
        for (IssueTxBean line : lines) {
            if (line.getSourceDocLineId() == null || !ids.add(line.getSourceDocLineId())
                    || !Boolean.TRUE.equals(line.getReferenceValid())
                    || line.getTransactionQuantity() == null
                    || line.getTransactionQuantity().signum() <= 0
                    || line.getTransactionQuantity().scale() > 2
                    || line.getMaterialStockId() == null || line.getMaterialStockId() <= 0
                    || line.getItemId() == null || line.getItemId() <= 0
                    || blank(line.getItemCode()) || blank(line.getUnitOfMeasure())
                    || blank(line.getBatchCode()) || line.getWarehouseId() == null
                    || line.getLocationId() == null || line.getAreaId() == null
                    || !documentId.equals(line.getSourceDocId())
                    || !"ISSUE".equals(line.getSourceDocType())) {
                throw new InventoryOperationException("INVALID_DOCUMENT_LINES", "领料单明细或来源库存不匹配");
            }
            required.merge(line.getMaterialStockId(), line.getTransactionQuantity(), BigDecimal::add);
            available.put(line.getMaterialStockId(), line.getAvailableQuantity());
        }
        for (Map.Entry<Long, BigDecimal> entry : required.entrySet()) {
            BigDecimal balance = available.get(entry.getKey());
            if (balance == null || balance.compareTo(entry.getValue()) < 0) {
                throw new InventoryOperationException("INSUFFICIENT_STOCK",
                        "来源库存不足，库存 ID：" + entry.getKey());
            }
        }
    }

    private void requireIssueTarget(Map<String, Object> target) {
        if (target == null || target.get("warehouseId") == null
                || target.get("locationId") == null || target.get("areaId") == null) {
            throw new InventoryOperationException("INVALID_ISSUE_TARGET", "线边库位置不存在或层级不匹配");
        }
    }

    private void requirePhysicalIssueSource(List<IssueTxBean> lines, Map<String, Object> target) {
        long virtualWarehouseId = ((Number) target.get("warehouseId")).longValue();
        for (IssueTxBean line : lines) {
            if (line.getWarehouseId() == virtualWarehouseId) {
                throw new InventoryOperationException("INVALID_DOCUMENT_LINES", "领料来源不能是线边库");
            }
        }
    }

    private void verifyIssueLedger(List<Map<String, Object>> transactions,
                                   Map<Long, IssueTxBean> lines, Map<String, Object> target) {
        Map<Long, Long> outIds = new LinkedHashMap<>();
        for (Map<String, Object> tx : transactions) {
            Long lineId = ((Number) tx.get("sourceDocLineId")).longValue();
            IssueTxBean line = lines.get(lineId);
            boolean outgoing = UserConstants.TRANSACTION_TYPE_ITEM_ISSUE_OUT.equals(tx.get("transactionType"));
            if (line == null || !line.getItemId().equals(((Number) tx.get("itemId")).longValue())
                    || !line.getBatchCode().equals(tx.get("batchCode"))) {
                throw new InventoryOperationException("LEDGER_MISMATCH", "领料流水物料或批次不符");
            }
            if (outgoing) {
                if (!line.getMaterialStockId().equals(((Number) tx.get("materialStockId")).longValue())
                        || !line.getWarehouseId().equals(((Number) tx.get("warehouseId")).longValue())
                        || !line.getLocationId().equals(((Number) tx.get("locationId")).longValue())
                        || !line.getAreaId().equals(((Number) tx.get("areaId")).longValue())
                        || tx.get("relatedTransactionId") != null) {
                    throw new InventoryOperationException("LEDGER_MISMATCH", "领料出库位置不符");
                }
                outIds.put(lineId, ((Number) tx.get("transactionId")).longValue());
            } else if (((Number) target.get("warehouseId")).longValue()
                    != ((Number) tx.get("warehouseId")).longValue()
                    || ((Number) target.get("locationId")).longValue()
                    != ((Number) tx.get("locationId")).longValue()
                    || ((Number) target.get("areaId")).longValue()
                    != ((Number) tx.get("areaId")).longValue()) {
                throw new InventoryOperationException("LEDGER_MISMATCH", "领料入库位置不符");
            }
        }
        for (Map<String, Object> tx : transactions) {
            if (UserConstants.TRANSACTION_TYPE_ITEM_ISSUE_IN.equals(tx.get("transactionType"))) {
                Long lineId = ((Number) tx.get("sourceDocLineId")).longValue();
                if (!outIds.get(lineId).equals(tx.get("relatedTransactionId"))) {
                    throw new InventoryOperationException("LEDGER_MISMATCH", "领料出入流水未配对");
                }
            }
        }
    }

    private void verifyLedger(String actionType, List<Map<String, Object>> transactions,
                              Map<Long, BigDecimal> quantities, Map<Long, String> units) {
        Set<String> seen = new HashSet<>();
        for (Map<String, Object> tx : transactions) {
            Number sourceLineId = (Number) tx.get("sourceDocLineId");
            if (sourceLineId == null) {
                throw new InventoryOperationException("LEDGER_MISMATCH", "库存流水缺少来源行");
            }
            Long lineId = sourceLineId.longValue();
            BigDecimal expected = quantities.get(lineId);
            String type = (String) tx.get("transactionType");
            BigDecimal actual = (BigDecimal) tx.get("transactionQuantity");
            int flag = ((Number) tx.get("transactionFlag")).intValue();
            boolean receipt = RECEIPT.equals(actionType);
            boolean incoming = UserConstants.TRANSACTION_TYPE_ITEM_RECPT.equals(type)
                    || UserConstants.TRANSACTION_TYPE_ITEM_ISSUE_IN.equals(type);
            boolean typeValid = receipt ? UserConstants.TRANSACTION_TYPE_ITEM_RECPT.equals(type)
                    : UserConstants.TRANSACTION_TYPE_ITEM_ISSUE_OUT.equals(type)
                      || UserConstants.TRANSACTION_TYPE_ITEM_ISSUE_IN.equals(type);
            if (expected == null || !typeValid || actual == null
                    || flag != (incoming ? 1 : -1)
                    || actual.compareTo(incoming ? expected : expected.negate()) != 0
                    || !units.get(lineId).equals(tx.get("unitOfMeasure"))
                    || !seen.add(lineId + ":" + type)) {
                throw new InventoryOperationException("LEDGER_MISMATCH", "库存流水与单据明细不一致");
            }
        }
        int legs = RECEIPT.equals(actionType) ? 1 : 2;
        if (seen.size() != quantities.size() * legs) {
            throw new InventoryOperationException("LEDGER_MISMATCH", "库存流水与单据明细不一致");
        }
    }

    private String digest(String action, String type, Long id, String expectedStatus, Long expectedVersion) {
        String canonical = String.join("\n", Arrays.asList(action, type, id.toString(),
                expectedStatus == null ? "" : expectedStatus.trim()));
        if (expectedVersion != null) canonical += "\n" + expectedVersion;
        try {
            byte[] bytes = MessageDigest.getInstance("SHA-256")
                    .digest(canonical.getBytes(StandardCharsets.UTF_8));
            StringBuilder hex = new StringBuilder(bytes.length * 2);
            for (byte value : bytes) {
                hex.append(String.format("%02x", value & 0xff));
            }
            return hex.toString();
        } catch (NoSuchAlgorithmException ex) {
            throw new IllegalStateException(ex);
        }
    }

    private AjaxResult error(int code, String errorCode, String message, Object data) {
        AjaxResult result = AjaxResult.error(code, message).put("errorCode", errorCode);
        if (data != null) {
            result.put("data", data);
        }
        return result;
    }

    private static class InventoryOperationException extends RuntimeException {
        private final String code;
        private InventoryOperationException(String code, String message) {
            super(message);
            this.code = code;
        }
    }
}
