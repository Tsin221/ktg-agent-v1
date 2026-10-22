package com.ktg.mes.pro.service.impl;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.ktg.common.core.domain.AjaxResult;
import com.ktg.mes.pro.controller.dto.ProWorkorderDraftRequest;
import com.ktg.mes.pro.controller.dto.ProWorkorderOperationRequest;
import com.ktg.mes.pro.domain.ProWorkorder;
import com.ktg.mes.pro.domain.ProWorkorderOperation;
import com.ktg.mes.pro.mapper.ProWorkorderOperationMapper;
import com.ktg.mes.pro.service.IProWorkorderService;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

import javax.validation.ConstraintViolation;
import javax.validation.Validator;
import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Objects;
import java.util.Set;
import java.util.TreeMap;
import java.util.UUID;

/** Transactional operation identity for the existing draft write paths. */
@Service
public class ProWorkorderOperationService {
    private static final Set<String> DRAFT_FIELDS = new HashSet<>(Arrays.asList(
            "workorderId", "version", "workorderCode", "workorderName", "workorderType",
            "orderSource", "sourceCode", "productId", "batchCode", "quantity", "clientId",
            "clientCode", "clientName", "requestDate", "parentId", "remark"));
    private static final Set<String> INTEGER_FIELDS = new HashSet<>(Arrays.asList(
            "workorderId", "version", "productId", "clientId", "parentId"));

    @Autowired private ProWorkorderOperationMapper mapper;
    @Autowired private IProWorkorderService workorders;
    @Autowired private PlatformTransactionManager transactionManager;
    @Autowired private ObjectMapper objectMapper;
    @Autowired private Validator validator;

    public AjaxResult execute(ProWorkorderOperationRequest request, Long ownerId, String username) {
        try {
            validateRequest(request);
            String digest = digest(request.getAction(), request.getDraft());
            if (!digest.equals(request.getRequestDigest())) {
                return error(422, "REQUEST_DIGEST_MISMATCH", "请求摘要不匹配");
            }
            ProWorkorderDraftRequest draft = objectMapper.treeToValue(
                    request.getDraft(), ProWorkorderDraftRequest.class);
            Set<ConstraintViolation<ProWorkorderDraftRequest>> violations = validator.validate(draft);
            if (!violations.isEmpty()) {
                return error(422, "VALIDATION_ERROR", violations.iterator().next().getMessage());
            }
            boolean create = "CREATE_DRAFT".equals(request.getAction());
            if (create ? (request.getExpectedVersion() != null || draft.getWorkorderId() != null
                    || draft.getVersion() != null)
                    : (draft.getWorkorderId() == null || draft.getWorkorderId() <= 0
                    || draft.getVersion() == null
                    || !Objects.equals(request.getExpectedVersion(), draft.getVersion()))) {
                return error(422, "VALIDATION_ERROR", "工单 ID 或预期版本不符合动作要求");
            }
            ProWorkorderOperation previous = mapper.selectById(request.getOperationId());
            if (previous != null) return replay(previous, request, ownerId);

            try {
                ProWorkorderOperation receipt = new TransactionTemplate(transactionManager).execute(status -> {
                    ProWorkorderOperation claim = new ProWorkorderOperation();
                    claim.setOperationId(request.getOperationId());
                    claim.setAction(request.getAction());
                    claim.setRequestDigest(digest);
                    claim.setOwnerId(ownerId);
                    claim.setWorkorderId(draft.getWorkorderId());
                    claim.setExpectedVersion(request.getExpectedVersion());
                    claim.setOutcome("executing");
                    mapper.insert(claim);

                    ProWorkorder workorder = draft.toWorkorder();
                    if (create) {
                        workorder.setCreateBy(username);
                        claim.setWorkorderId(workorders.createDraftWorkorder(workorder));
                    } else {
                        workorder.setUpdateBy(username);
                        workorders.updateDraftWorkorder(workorder);
                    }
                    ProWorkorder fact = workorders.selectProWorkorderByWorkorderId(claim.getWorkorderId());
                    if (fact == null) throw new IllegalStateException("Committed workorder is missing");
                    claim.setWorkorderCode(fact.getWorkorderCode());
                    claim.setWorkorderVersion(fact.getVersion());
                    if (mapper.complete(claim) != 1) throw new IllegalStateException("Receipt completion failed");
                    return claim;
                });
                return AjaxResult.success(mapper.selectById(receipt.getOperationId()));
            } catch (DuplicateKeyException conflict) {
                // The unique operation key makes a concurrent claimant wait for commit/rollback.
                ProWorkorderOperation committed = mapper.selectById(request.getOperationId());
                if (committed != null) return replay(committed, request, ownerId);
                throw conflict; // e.g. duplicate workorder code, not an operation replay.
            } catch (com.ktg.common.exception.ServiceException businessError) {
                int status = businessError.getCode() == null ? 409 : businessError.getCode();
                ProWorkorderOperation failed = new ProWorkorderOperation();
                failed.setOperationId(request.getOperationId());
                failed.setAction(request.getAction());
                failed.setRequestDigest(digest);
                failed.setOwnerId(ownerId);
                failed.setWorkorderId(draft.getWorkorderId());
                failed.setExpectedVersion(request.getExpectedVersion());
                failed.setOutcome("failed");
                failed.setErrorCode(status == 409 ? "WORKORDER_CONFLICT" : "WORKORDER_ERROR");
                try {
                    new TransactionTemplate(transactionManager).execute(tx -> { mapper.insert(failed); return null; });
                } catch (DuplicateKeyException concurrent) {
                    ProWorkorderOperation committed = mapper.selectById(request.getOperationId());
                    if (committed != null) return replay(committed, request, ownerId);
                    throw concurrent;
                }
                return error(status, failed.getErrorCode(), businessError.getMessage()).put("data", failed);
            }
        } catch (IllegalArgumentException ex) {
            return error(422, "VALIDATION_ERROR", ex.getMessage());
        } catch (com.ktg.common.exception.ServiceException ex) {
            int code = ex.getCode() == null ? 409 : ex.getCode();
            return error(code, code == 409 ? "WORKORDER_CONFLICT" : "WORKORDER_ERROR", ex.getMessage());
        } catch (Exception ex) {
            // A transport failure after commit must be reconciled by GET; never classify it as failed.
            return error(202, "RESULT_UNKNOWN", "操作结果待核实，请按原操作 ID 查询");
        }
    }

    public AjaxResult result(String operationId, Long ownerId) {
        try { UUID.fromString(operationId); }
        catch (RuntimeException ex) { return error(404, "OPERATION_NOT_FOUND", "操作不存在"); }
        ProWorkorderOperation receipt = mapper.selectById(operationId);
        if (receipt == null || !ownerId.equals(receipt.getOwnerId())) {
            return error(404, "OPERATION_NOT_FOUND", "操作不存在");
        }
        return AjaxResult.success(receipt);
    }

    private AjaxResult replay(ProWorkorderOperation previous,
                              ProWorkorderOperationRequest request, Long ownerId) {
        if (!ownerId.equals(previous.getOwnerId()) || !request.getAction().equals(previous.getAction())
                || !request.getRequestDigest().equals(previous.getRequestDigest())
                || !Objects.equals(request.getExpectedVersion(), previous.getExpectedVersion())
                || ("EDIT_DRAFT".equals(request.getAction())
                    && !Objects.equals(request.getDraft().get("workorderId").longValue(), previous.getWorkorderId()))) {
            return error(409, "OPERATION_CONFLICT", "操作 ID 已绑定另一请求");
        }
        if ("failed".equals(previous.getOutcome()))
            return error(409, previous.getErrorCode(), "工单操作未生效").put("data", previous);
        return AjaxResult.success(previous);
    }

    private void validateRequest(ProWorkorderOperationRequest request) {
        if (request == null || request.getOperationId() == null) throw new IllegalArgumentException("operationId 不能为空");
        try {
            UUID id = UUID.fromString(request.getOperationId());
            if (!id.toString().equals(request.getOperationId())) throw new IllegalArgumentException("operationId 格式无效");
        } catch (RuntimeException ex) { throw new IllegalArgumentException("operationId 格式无效"); }
        if (!"CREATE_DRAFT".equals(request.getAction()) && !"EDIT_DRAFT".equals(request.getAction()))
            throw new IllegalArgumentException("action 不受支持");
        if (request.getDraft() == null || !request.getDraft().isObject())
            throw new IllegalArgumentException("draft 必须是对象");
        request.getDraft().fieldNames().forEachRemaining(name -> {
            if (!DRAFT_FIELDS.contains(name)) throw new IllegalArgumentException("draft 含未知字段: " + name);
            JsonNode value = request.getDraft().get(name);
            if (INTEGER_FIELDS.contains(name) && !value.isNull()
                    && (!value.isIntegralNumber() || !value.canConvertToLong()))
                throw new IllegalArgumentException(name + " 必须是整数");
        });
    }

    /** contract-v1 canonical JSON: present non-null DTO fields, sorted keys, normalized quantity. */
    public String digest(String action, JsonNode draft) {
        try {
            TreeMap<String, Object> fields = new TreeMap<>();
            draft.fields().forEachRemaining(entry -> {
                JsonNode value = entry.getValue();
                if (value.isNull()) return;
                if ("quantity".equals(entry.getKey())) {
                    if (!value.isNumber() && !value.isTextual()) throw new IllegalArgumentException("quantity 格式无效");
                    BigDecimal decimal = new BigDecimal(value.asText()).stripTrailingZeros();
                    fields.put(entry.getKey(), decimal.toPlainString());
                } else if (INTEGER_FIELDS.contains(entry.getKey())) {
                    fields.put(entry.getKey(), value.longValue());
                } else if (value.isTextual()) {
                    fields.put(entry.getKey(), value.textValue());
                } else throw new IllegalArgumentException(entry.getKey() + " 格式无效");
            });
            TreeMap<String, Object> canonical = new TreeMap<>();
            canonical.put("action", action);
            canonical.put("draft", fields);
            byte[] bytes = objectMapper.writeValueAsString(canonical).getBytes(StandardCharsets.UTF_8);
            byte[] hash = MessageDigest.getInstance("SHA-256").digest(bytes);
            StringBuilder hex = new StringBuilder("sha256:");
            for (byte b : hash) hex.append(String.format("%02x", b & 0xff));
            return hex.toString();
        } catch (NoSuchAlgorithmException ex) {
            throw new IllegalStateException(ex);
        } catch (com.fasterxml.jackson.core.JsonProcessingException ex) {
            throw new IllegalArgumentException("draft 无法规范化", ex);
        }
    }

    private AjaxResult error(int status, String code, String message) {
        return AjaxResult.error(status, message).put("errorCode", code);
    }
}
