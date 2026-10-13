package com.ktg.mes.wm.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.ktg.common.core.domain.AjaxResult;
import com.ktg.common.utils.SecurityUtils;
import com.ktg.mes.wm.domain.InventoryOperationReceipt;
import com.ktg.mes.wm.domain.tx.IssueTxBean;
import com.ktg.mes.wm.domain.tx.ItemRecptTxBean;
import com.ktg.mes.wm.mapper.InventoryOperationMapper;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.OffsetDateTime;
import java.util.Arrays;
import java.util.LinkedHashMap;
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

    public AjaxResult executeReceipt(Long documentId, String operationId) {
        return execute(RECEIPT, documentId, operationId, null);
    }

    public AjaxResult executeIssue(Long documentId, String operationId) {
        return execute(ISSUE, documentId, operationId, null);
    }

    public AjaxResult execute(String actionType, Long documentId, String operationId, String expectedStatus) {
        final String normalizedExpectedStatus = expectedStatus == null || expectedStatus.trim().isEmpty()
                ? null : expectedStatus.trim().toUpperCase(Locale.ROOT);
        if (!RECEIPT.equals(actionType) && !ISSUE.equals(actionType)) {
            return error(422, "UNSUPPORTED_ACTION", "不支持的库存动作", null);
        }
        if (documentId == null || documentId <= 0) {
            return error(422, "INVALID_DOCUMENT_ID", "单据 ID 无效", null);
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
        receipt.setRequestDigest(digest(actionType, documentType, documentId, normalizedExpectedStatus));
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

        try {
            transaction().execute(status -> {
                executeReserved(receipt, normalizedExpectedStatus);
                return null;
            });
        } catch (DuplicateKeyException ex) {
            String owner = mapper.executionOwner(actionType, documentType, documentId);
            if (owner != null && !owner.equals(stableOperationId)) {
                return persistFailure(receipt, "rejected", "DOCUMENT_ALREADY_EXECUTED",
                        "单据已经执行", owner);
            }
            log.error("Inventory operation {} hit an unrelated unique-key conflict", stableOperationId, ex);
            return persistFailure(receipt, "failed", "EXECUTION_FAILED", "库存执行失败", null);
        } catch (InventoryOperationException ex) {
            return persistFailure(receipt, "rejected", ex.code, ex.getMessage(), null);
        } catch (RuntimeException ex) {
            log.error("Inventory operation {} failed and was rolled back", stableOperationId, ex);
            return persistFailure(receipt, "failed", "EXECUTION_FAILED", "库存执行失败", null);
        }
        return replayOrConflict(mapper.find(stableOperationId), receipt, false);
    }

    private void executeReserved(InventoryOperationReceipt receipt, String expectedStatus) {
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
        if (!mapper.transactions(receipt.getDocumentType(), receipt.getDocumentId()).isEmpty()) {
            throw new InventoryOperationException("DOCUMENT_ALREADY_EXECUTED", "已有库存流水的单据不能重新执行");
        }

        int expectedTransactionCount;
        if (RECEIPT.equals(receipt.getActionType())) {
            List<ItemRecptTxBean> lines = itemRecptService.getTxBeans(receipt.getDocumentId());
            if (lines == null || lines.isEmpty()) {
                throw new InventoryOperationException("INVALID_DOCUMENT_LINES", "入库单没有有效明细");
            }
            for (ItemRecptTxBean line : lines) {
                if (line.getSourceDocLineId() == null || line.getTransactionQuantity() == null
                        || line.getTransactionQuantity().signum() <= 0) {
                    throw new InventoryOperationException("INVALID_DOCUMENT_LINES", "入库单明细无效");
                }
            }
            expectedTransactionCount = lines.size();
            storageCoreService.processItemRecpt(lines);
        } else {
            List<IssueTxBean> lines = issueHeaderService.getTxBeans(receipt.getDocumentId());
            if (lines == null || lines.isEmpty()) {
                throw new InventoryOperationException("INVALID_DOCUMENT_LINES", "领料单没有有效明细");
            }
            for (IssueTxBean line : lines) {
                if (line.getSourceDocLineId() == null || line.getTransactionQuantity() == null
                        || line.getTransactionQuantity().signum() <= 0) {
                    throw new InventoryOperationException("INVALID_DOCUMENT_LINES", "领料单明细无效");
                }
            }
            expectedTransactionCount = lines.size() * 2;
            storageCoreService.processIssue(lines);
        }

        List<Map<String, Object>> transactions = mapper.transactions(
                receipt.getDocumentType(), receipt.getDocumentId());
        if (transactions.size() != expectedTransactionCount) {
            throw new InventoryOperationException("LEDGER_MISMATCH", "库存流水数量与单据明细不符");
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
        int httpCode = "failed".equals(status) ? 500
                : "INVALID_DOCUMENT_LINES".equals(code) ? 422
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
        Map<String, Object> result = existing.getResultJson() == null
                ? baseResult(existing, existing.getOperationStatus()) : parse(existing.getResultJson());
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

    private String digest(String action, String type, Long id, String expectedStatus) {
        String canonical = String.join("\n", Arrays.asList(action, type, id.toString(),
                expectedStatus == null ? "" : expectedStatus.trim()));
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
