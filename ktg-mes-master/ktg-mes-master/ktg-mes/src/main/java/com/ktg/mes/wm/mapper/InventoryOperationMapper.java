package com.ktg.mes.wm.mapper;

import com.ktg.mes.wm.domain.InventoryOperationReceipt;
import com.ktg.common.constant.UserConstants;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.stereotype.Repository;

import java.sql.Timestamp;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

@Repository
public class InventoryOperationMapper {
    @Autowired
    private JdbcTemplate jdbc;

    private final RowMapper<InventoryOperationReceipt> receiptMapper = (rs, row) -> {
        InventoryOperationReceipt receipt = new InventoryOperationReceipt();
        receipt.setOperationId(rs.getString("operation_id"));
        receipt.setActionType(rs.getString("action_type"));
        receipt.setDocumentType(rs.getString("document_type"));
        receipt.setDocumentId(rs.getLong("document_id"));
        receipt.setRequestDigest(rs.getString("request_digest"));
        receipt.setExecutedById(rs.getLong("executed_by_id"));
        receipt.setExecutedByName(rs.getString("executed_by_name"));
        receipt.setOperationStatus(rs.getString("operation_status"));
        receipt.setResultJson(rs.getString("result_json"));
        receipt.setErrorCode(rs.getString("error_code"));
        Timestamp created = rs.getTimestamp("created_at");
        Timestamp completed = rs.getTimestamp("completed_at");
        receipt.setCreatedAt(created == null ? null : created.toLocalDateTime());
        receipt.setCompletedAt(completed == null ? null : completed.toLocalDateTime());
        return receipt;
    };

    public void reserve(InventoryOperationReceipt receipt) {
        jdbc.update("INSERT INTO wm_inventory_operation "
                + "(operation_id, action_type, document_type, document_id, request_digest, "
                + "executed_by_id, executed_by_name, operation_status, created_at) "
                + "VALUES (?, ?, ?, ?, ?, ?, ?, 'executing', NOW(6))",
                receipt.getOperationId(), receipt.getActionType(), receipt.getDocumentType(),
                receipt.getDocumentId(), receipt.getRequestDigest(), receipt.getExecutedById(),
                receipt.getExecutedByName());
    }

    public InventoryOperationReceipt find(String operationId) {
        List<InventoryOperationReceipt> rows = jdbc.query(
                "SELECT * FROM wm_inventory_operation WHERE operation_id = ?", receiptMapper, operationId);
        return rows.isEmpty() ? null : rows.get(0);
    }

    public InventoryOperationReceipt findByDocument(String actionType, String documentType, Long documentId) {
        List<InventoryOperationReceipt> rows = jdbc.query(
                "SELECT o.* FROM wm_inventory_executed_document d "
                + "JOIN wm_inventory_operation o ON o.operation_id = d.operation_id "
                + "WHERE d.action_type = ? AND d.document_type = ? AND d.document_id = ?",
                receiptMapper, actionType, documentType, documentId);
        return rows.isEmpty() ? null : rows.get(0);
    }

    public InventoryOperationReceipt lock(String operationId) {
        return jdbc.queryForObject("SELECT * FROM wm_inventory_operation WHERE operation_id = ? FOR UPDATE",
                receiptMapper, operationId);
    }

    public String executionOwner(String actionType, String documentType, Long documentId) {
        List<String> rows = jdbc.queryForList("SELECT operation_id FROM wm_inventory_executed_document "
                + "WHERE action_type = ? AND document_type = ? AND document_id = ?",
                String.class, actionType, documentType, documentId);
        return rows.isEmpty() ? null : rows.get(0);
    }

    public void claimDocument(InventoryOperationReceipt receipt) {
        jdbc.update("INSERT INTO wm_inventory_executed_document "
                + "(action_type, document_type, document_id, operation_id) VALUES (?, ?, ?, ?)",
                receipt.getActionType(), receipt.getDocumentType(), receipt.getDocumentId(),
                receipt.getOperationId());
    }

    public Map<String, Object> lockDocument(String documentType, Long documentId) {
        String sql = "IR".equals(documentType)
                ? "SELECT recpt_code AS document_code, status, version FROM wm_item_recpt WHERE recpt_id = ? FOR UPDATE"
                : "SELECT issue_code AS document_code, status, version FROM wm_issue_header WHERE issue_id = ? FOR UPDATE";
        List<Map<String, Object>> rows = jdbc.query(sql, (rs, row) -> {
            Map<String, Object> document = new LinkedHashMap<>();
            document.put("document_code", rs.getString("document_code"));
            document.put("status", rs.getString("status"));
            document.put("version", rs.getLong("version"));
            return document;
        }, documentId);
        return rows.isEmpty() ? null : rows.get(0);
    }

    public Map<String, Object> readReceiptDocument(Long documentId) {
        List<Map<String, Object>> rows = jdbc.query(
                "SELECT recpt_code AS document_code, status, version FROM wm_item_recpt WHERE recpt_id = ?",
                (rs, row) -> {
                    Map<String, Object> document = new LinkedHashMap<>();
                    document.put("document_code", rs.getString("document_code"));
                    document.put("status", rs.getString("status"));
                    document.put("version", rs.getLong("version"));
                    return document;
                }, documentId);
        return rows.isEmpty() ? null : rows.get(0);
    }

    public Map<String, Object> readIssueDocument(Long documentId) {
        List<Map<String, Object>> rows = jdbc.queryForList(
                "SELECT issue_code AS document_code, status, version FROM wm_issue_header WHERE issue_id = ?",
                documentId);
        return rows.isEmpty() ? null : rows.get(0);
    }

    public Map<String, Object> issueTarget() {
        List<Map<String, Object>> rows = jdbc.queryForList(
                "SELECT w.warehouse_id AS warehouseId, w.warehouse_name AS warehouseName, "
                + "l.location_id AS locationId, l.location_name AS locationName, "
                + "a.area_id AS areaId, a.area_name AS areaName "
                + "FROM wm_warehouse w JOIN wm_storage_location l ON l.warehouse_id = w.warehouse_id "
                + "JOIN wm_storage_area a ON a.location_id = l.location_id "
                + "WHERE w.warehouse_code = ? AND l.location_code = ? AND a.area_code = ?",
                UserConstants.VIRTUAL_WH, UserConstants.VIRTUAL_WS, UserConstants.VIRTUAL_WA);
        return rows.size() == 1 ? rows.get(0) : null;
    }

    public int finishDocument(String documentType, Long documentId) {
        String sql = "IR".equals(documentType)
                ? "UPDATE wm_item_recpt SET status = 'FINISHED', version = version + 1 WHERE recpt_id = ? AND status = 'APPROVED'"
                : "UPDATE wm_issue_header SET status = 'FINISHED', version = version + 1 WHERE issue_id = ? AND status = 'APPROVED'";
        return jdbc.update(sql, documentId);
    }

    public int reopenReceiptForPutaway(Long documentId, Long expectedVersion) {
        return jdbc.update("UPDATE wm_item_recpt SET status = 'APPROVING', version = version + 1 "
                + "WHERE recpt_id = ? AND status = 'APPROVED' AND version = ?", documentId, expectedVersion);
    }

    public List<Map<String, Object>> transactions(String documentType, Long documentId) {
        return jdbc.query("SELECT transaction_id, transaction_type, transaction_flag, "
                + "transaction_quantity, source_doc_type, source_doc_id, source_doc_line_id, "
                + "unit_of_measure, related_transaction_id, material_stock_id, item_id, batch_code, "
                + "warehouse_id, location_id, area_id FROM wm_transaction "
                + "WHERE source_doc_type = ? AND source_doc_id = ? ORDER BY transaction_id",
                (rs, row) -> {
                    Map<String, Object> transaction = new LinkedHashMap<>();
                    transaction.put("transactionId", rs.getLong("transaction_id"));
                    transaction.put("transactionType", rs.getString("transaction_type"));
                    transaction.put("transactionFlag", rs.getInt("transaction_flag"));
                    transaction.put("transactionQuantity", rs.getBigDecimal("transaction_quantity"));
                    transaction.put("sourceDocType", rs.getString("source_doc_type"));
                    transaction.put("sourceDocId", rs.getLong("source_doc_id"));
                    transaction.put("sourceDocLineId", rs.getLong("source_doc_line_id"));
                    transaction.put("unitOfMeasure", rs.getString("unit_of_measure"));
                    transaction.put("materialStockId", rs.getLong("material_stock_id"));
                    transaction.put("itemId", rs.getLong("item_id"));
                    transaction.put("batchCode", rs.getString("batch_code"));
                    transaction.put("warehouseId", rs.getLong("warehouse_id"));
                    transaction.put("locationId", rs.getLong("location_id"));
                    transaction.put("areaId", rs.getLong("area_id"));
                    long related = rs.getLong("related_transaction_id");
                    transaction.put("relatedTransactionId", rs.wasNull() ? null : related);
                    return transaction;
                }, documentType, documentId);
    }

    public void linkTransaction(String operationId, Long transactionId, int sequence) {
        jdbc.update("INSERT INTO wm_inventory_operation_transaction "
                + "(operation_id, transaction_id, sequence_no) VALUES (?, ?, ?)",
                operationId, transactionId, sequence);
    }

    public int linkedTransactionCount(String operationId) {
        return jdbc.queryForObject("SELECT COUNT(*) FROM wm_inventory_operation_transaction WHERE operation_id = ?",
                Integer.class, operationId);
    }

    public List<Long> linkedTransactionIds(String operationId) {
        return jdbc.queryForList(
                "SELECT transaction_id FROM wm_inventory_operation_transaction WHERE operation_id = ?",
                Long.class, operationId);
    }

    public int complete(String operationId, String status, String errorCode, String resultJson) {
        return jdbc.update("UPDATE wm_inventory_operation SET operation_status = ?, error_code = ?, "
                + "result_json = ?, completed_at = NOW(6) WHERE operation_id = ? AND operation_status = 'executing'",
                status, errorCode, resultJson, operationId);
    }
}
