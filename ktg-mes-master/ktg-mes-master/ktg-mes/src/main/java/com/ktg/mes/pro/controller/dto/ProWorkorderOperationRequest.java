package com.ktg.mes.pro.controller.dto;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonAnySetter;
import com.fasterxml.jackson.databind.JsonNode;

/** Raw draft is retained so the operation digest can distinguish omitted fields. */
@JsonIgnoreProperties(ignoreUnknown = false)
public class ProWorkorderOperationRequest {
    private String operationId;
    private String action;
    private String requestDigest;
    private Long expectedVersion;
    private JsonNode draft;

    public String getOperationId() { return operationId; }
    public void setOperationId(String operationId) { this.operationId = operationId; }
    public String getAction() { return action; }
    public void setAction(String action) { this.action = action; }
    public String getRequestDigest() { return requestDigest; }
    public void setRequestDigest(String requestDigest) { this.requestDigest = requestDigest; }
    public Long getExpectedVersion() { return expectedVersion; }
    public void setExpectedVersion(Long expectedVersion) { this.expectedVersion = expectedVersion; }
    public JsonNode getDraft() { return draft; }
    public void setDraft(JsonNode draft) { this.draft = draft; }

    @JsonAnySetter
    public void rejectUnknown(String name, JsonNode ignored) {
        throw new IllegalArgumentException("Unknown operation field: " + name);
    }
}
