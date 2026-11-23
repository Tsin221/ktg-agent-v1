package com.ktg.mes.pro.execution;

import java.util.Date;

/** Stored operation identity. Not a controller DTO. */
public class ExecutionOperation {
    private String operationId;
    private String actionType;
    private Long workorderId;
    private Long actorId;
    private String requestDigest;
    private String resultRef;
    private Date committedAt;

    public ExecutionOperation() {}

    public ExecutionOperation(String operationId, String actionType, Long workorderId,
                              Long actorId, String requestDigest) {
        this.operationId = operationId;
        this.actionType = actionType;
        this.workorderId = workorderId;
        this.actorId = actorId;
        this.requestDigest = requestDigest;
    }

    public String getOperationId() { return operationId; }
    public void setOperationId(String value) { operationId = value; }
    public String getActionType() { return actionType; }
    public void setActionType(String value) { actionType = value; }
    public Long getWorkorderId() { return workorderId; }
    public void setWorkorderId(Long value) { workorderId = value; }
    public Long getActorId() { return actorId; }
    public void setActorId(Long value) { actorId = value; }
    public String getRequestDigest() { return requestDigest; }
    public void setRequestDigest(String value) { requestDigest = value; }
    public String getResultRef() { return resultRef; }
    public void setResultRef(String value) { resultRef = value; }
    public Date getCommittedAt() { return committedAt; }
    public void setCommittedAt(Date value) { committedAt = value; }
}
