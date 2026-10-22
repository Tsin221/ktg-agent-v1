package com.ktg.mes.pro.domain;

import com.fasterxml.jackson.annotation.JsonFormat;
import java.util.Date;

/** Immutable identity and committed result of one draft command. */
public class ProWorkorderOperation {
    private String operationId;
    private String action;
    private String requestDigest;
    private Long ownerId;
    private Long workorderId;
    private Long expectedVersion;
    private String outcome;
    private String errorCode;
    private String workorderCode;
    private Long workorderVersion;
    @JsonFormat(shape = JsonFormat.Shape.STRING, pattern = "yyyy-MM-dd'T'HH:mm:ss.SSS'Z'", timezone = "UTC")
    private Date committedAt;

    public String getOperationId() { return operationId; }
    public void setOperationId(String value) { operationId = value; }
    public String getAction() { return action; }
    public void setAction(String value) { action = value; }
    public String getRequestDigest() { return requestDigest; }
    public void setRequestDigest(String value) { requestDigest = value; }
    public Long getOwnerId() { return ownerId; }
    public void setOwnerId(Long value) { ownerId = value; }
    public Long getWorkorderId() { return workorderId; }
    public void setWorkorderId(Long value) { workorderId = value; }
    public Long getExpectedVersion() { return expectedVersion; }
    public void setExpectedVersion(Long value) { expectedVersion = value; }
    public String getOutcome() { return outcome; }
    public void setOutcome(String value) { outcome = value; }
    public String getErrorCode() { return errorCode; }
    public void setErrorCode(String value) { errorCode = value; }
    public String getWorkorderCode() { return workorderCode; }
    public void setWorkorderCode(String value) { workorderCode = value; }
    public Long getWorkorderVersion() { return workorderVersion; }
    public void setWorkorderVersion(Long value) { workorderVersion = value; }
    public Date getCommittedAt() { return committedAt; }
    public void setCommittedAt(Date value) { committedAt = value; }
}
