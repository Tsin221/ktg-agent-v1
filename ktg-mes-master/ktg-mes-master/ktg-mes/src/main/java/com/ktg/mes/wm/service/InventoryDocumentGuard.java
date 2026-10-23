package com.ktg.mes.wm.service;

import com.ktg.common.exception.ServiceException;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

import java.util.Arrays;
import java.util.List;
import java.util.Objects;
import java.util.TreeSet;

/** Serializes every document mutation with execution through the header row. */
@Service
public class InventoryDocumentGuard {
    @Autowired private JdbcTemplate jdbc;

    private String header(String type) {
        if ("IR".equals(type)) return "wm_item_recpt";
        if ("ISSUE".equals(type)) return "wm_issue_header";
        throw new IllegalArgumentException("Unknown inventory document type");
    }

    private String idColumn(String type) {
        return "IR".equals(type) ? "recpt_id" : "issue_id";
    }

    public long lockEditable(String type, Long id, Long expectedVersion) {
        if (id == null) throw conflict("单据 ID 不能为空");
        List<Header> rows = jdbc.query("SELECT status, version FROM " + header(type)
                + " WHERE " + idColumn(type) + " = ? FOR UPDATE", (rs, row) ->
                new Header(rs.getString(1), rs.getLong(2)), id);
        if (rows.isEmpty()) throw conflict("单据不存在");
        Header current = rows.get(0);
        if ("FINISHED".equals(current.status) || "CANCELED".equals(current.status)) {
            throw conflict("单据已冻结，不能修改");
        }
        if (expectedVersion != null && !Objects.equals(expectedVersion, current.version)) {
            throw conflict("单据版本已变化，请重新加载");
        }
        return current.version;
    }

    public void lockHeaderUpdate(String type, Long id, Long expectedVersion, String nextStatus) {
        long version = lockEditable(type, id, expectedVersion);
        if (nextStatus == null) return;
        String current = jdbc.queryForObject("SELECT status FROM " + header(type)
                + " WHERE " + idColumn(type) + " = ?", String.class, id);
        if (nextStatus.equals(current)) return;
        boolean valid = ("PREPARE".equals(current) && ("APPROVING".equals(nextStatus)
                || "CANCELED".equals(nextStatus) || "CONFIRMED".equals(nextStatus)))
                || ("APPROVING".equals(current) && ("APPROVED".equals(nextStatus)
                || "CANCELED".equals(nextStatus)))
                || ("APPROVED".equals(current) && "CANCELED".equals(nextStatus));
        if (!valid) throw conflict("非法单据状态流转，当前版本 " + version);
    }

    public void requireDraft(String type, Long id) {
        List<String> rows = jdbc.queryForList("SELECT status FROM " + header(type)
                + " WHERE " + idColumn(type) + " = ? FOR UPDATE", String.class, id);
        if (rows.isEmpty() || !"PREPARE".equals(rows.get(0))) {
            throw conflict("只能删除草稿状态的单据");
        }
    }

    public Long lineParent(String type, Long lineId) {
        if (lineId == null) throw conflict("单据行 ID 不能为空");
        String table = "IR".equals(type) ? "wm_item_recpt_line" : "wm_issue_line";
        List<Long> rows = jdbc.queryForList("SELECT " + idColumn(type) + " FROM " + table
                + " WHERE line_id = ?", Long.class, lineId);
        if (rows.isEmpty() || rows.get(0) == null) throw conflict("单据行不存在或未关联单据");
        return rows.get(0);
    }

    public void lockLineMutation(String type, Long parentId, Long lineId) {
        lockEditable(type, parentId, null);
        if (lineId != null && !parentId.equals(lineParent(type, lineId))) {
            throw conflict("单据行所属单据已变化");
        }
    }

    /** The caller must have called lockLineMutation first, keeping the header locked. */
    public String lockedDocumentStatus(String type, Long id) {
        return jdbc.queryForObject("SELECT status FROM " + header(type) + " WHERE " + idColumn(type) + " = ?",
                String.class, id);
    }

    public TreeSet<Long> lockLines(String type, Long[] lineIds) {
        if (lineIds == null || lineIds.length == 0 || Arrays.asList(lineIds).contains(null)) {
            throw conflict("单据行 ID 无效");
        }
        TreeSet<Long> parents = new TreeSet<>();
        for (Long id : lineIds) parents.add(lineParent(type, id));
        for (Long parent : parents) lockEditable(type, parent, null);
        for (Long id : lineIds) {
            if (!parents.contains(lineParent(type, id))) throw conflict("单据行所属单据已变化");
        }
        return parents;
    }

    public void bumpVersion(String type, Long id) {
        if (jdbc.update("UPDATE " + header(type) + " SET version = version + 1 WHERE "
                + idColumn(type) + " = ?", id) != 1) {
            throw conflict("单据已冻结，不能修改");
        }
    }

    public static ServiceException conflict(String message) {
        return new ServiceException(message, 409);
    }

    private static final class Header {
        final String status;
        final Long version;
        Header(String status, Long version) { this.status = status; this.version = version; }
    }
}
