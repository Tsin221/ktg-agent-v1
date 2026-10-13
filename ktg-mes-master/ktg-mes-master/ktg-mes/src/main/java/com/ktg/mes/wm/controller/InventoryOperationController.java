package com.ktg.mes.wm.controller;

import com.ktg.common.core.domain.AjaxResult;
import com.ktg.mes.wm.service.InventoryOperationService;
import com.ktg.framework.web.service.PermissionService;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/** Explicit receipt execution entry point for clients that keep an operation ID. */
@RestController
@RequestMapping("/mes/wm/inventory-operations")
public class InventoryOperationController {
    @Autowired private InventoryOperationService service;
    @Autowired private PermissionService permissions;

    @PreAuthorize("@ss.hasPermi('mes:wm:itemrecpt:edit') or @ss.hasPermi('mes:wm:issueheader:edit')")
    @PostMapping
    public ResponseEntity<AjaxResult> execute(@RequestBody Command command) {
        AjaxResult result;
        if (!"ITEM_RECEIPT_EXECUTE".equals(command.getActionType())
                && !"PRODUCTION_ISSUE_EXECUTE".equals(command.getActionType())) {
            result = AjaxResult.error(422, "此入口暂不支持该库存动作")
                    .put("errorCode", "UNSUPPORTED_ACTION");
        } else if (("ITEM_RECEIPT_EXECUTE".equals(command.getActionType())
                && !permissions.hasPermi("mes:wm:itemrecpt:edit"))
                || ("PRODUCTION_ISSUE_EXECUTE".equals(command.getActionType())
                && !permissions.hasPermi("mes:wm:issueheader:edit"))) {
            result = AjaxResult.error(403, "没有库存动作权限").put("errorCode", "FORBIDDEN");
        } else if (command.getOperationId() == null || command.getOperationId().trim().isEmpty()) {
            result = AjaxResult.error(422, "操作 ID 不能为空")
                    .put("errorCode", "INVALID_OPERATION_ID");
        } else {
            result = service.execute(command.getActionType(), command.getDocumentId(),
                    command.getOperationId(), command.getExpectedStatus(), command.getExpectedVersion());
        }
        return ResponseEntity.status((Integer) result.get("code")).body(result);
    }

    public static class Command {
        private String actionType;
        private Long documentId;
        private String operationId;
        private String expectedStatus;
        private Long expectedVersion;

        public String getActionType() { return actionType; }
        public void setActionType(String actionType) { this.actionType = actionType; }
        public Long getDocumentId() { return documentId; }
        public void setDocumentId(Long documentId) { this.documentId = documentId; }
        public String getOperationId() { return operationId; }
        public void setOperationId(String operationId) { this.operationId = operationId; }
        public String getExpectedStatus() { return expectedStatus; }
        public void setExpectedStatus(String expectedStatus) { this.expectedStatus = expectedStatus; }
        public Long getExpectedVersion() { return expectedVersion; }
        public void setExpectedVersion(Long expectedVersion) { this.expectedVersion = expectedVersion; }
    }
}
