package com.ktg.mes.wm.domain;

import java.time.LocalDateTime;

/** A committed record of an inventory execution request. */
public class InventoryOperationReceipt {
    private String operationId;
    private String actionType;
    private String documentType;
    private Long documentId;
    private String requestDigest;
    private Long executedById;
    private String executedByName;
    private String operationStatus;
    private String resultJson;
    private String errorCode;
    private LocalDateTime createdAt;
    private LocalDateTime completedAt;

    public String getOperationId() { return operationId; }
    public void setOperationId(String value) { operationId = value; }
    public String getActionType() { return actionType; }
    public void setActionType(String value) { actionType = value; }
    public String getDocumentType() { return documentType; }
    public void setDocumentType(String value) { documentType = value; }
    public Long getDocumentId() { return documentId; }
    public void setDocumentId(Long value) { documentId = value; }
    public String getRequestDigest() { return requestDigest; }
    public void setRequestDigest(String value) { requestDigest = value; }
    public Long getExecutedById() { return executedById; }
    public void setExecutedById(Long value) { executedById = value; }
    public String getExecutedByName() { return executedByName; }
    public void setExecutedByName(String value) { executedByName = value; }
    public String getOperationStatus() { return operationStatus; }
    public void setOperationStatus(String value) { operationStatus = value; }
    public String getResultJson() { return resultJson; }
    public void setResultJson(String value) { resultJson = value; }
    public String getErrorCode() { return errorCode; }
    public void setErrorCode(String value) { errorCode = value; }
    public LocalDateTime getCreatedAt() { return createdAt; }
    public void setCreatedAt(LocalDateTime value) { createdAt = value; }
    public LocalDateTime getCompletedAt() { return completedAt; }
    public void setCompletedAt(LocalDateTime value) { completedAt = value; }
}
