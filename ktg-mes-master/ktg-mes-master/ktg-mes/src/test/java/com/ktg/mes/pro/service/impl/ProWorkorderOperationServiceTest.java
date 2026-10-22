package com.ktg.mes.pro.service.impl;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.ktg.common.core.domain.AjaxResult;
import com.ktg.common.exception.ServiceException;
import com.ktg.mes.pro.controller.dto.ProWorkorderOperationRequest;
import com.ktg.mes.pro.domain.ProWorkorder;
import com.ktg.mes.pro.domain.ProWorkorderOperation;
import com.ktg.mes.pro.mapper.ProWorkorderOperationMapper;
import com.ktg.mes.pro.service.IProWorkorderService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabaseBuilder;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabaseType;
import org.springframework.test.util.ReflectionTestUtils;

import javax.sql.DataSource;
import javax.validation.Validation;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

/** Isolated H2 transaction checks; MySQL concurrency remains a separate gate. */
class ProWorkorderOperationServiceTest {
    private static final String DRAFT = "{\"workorderCode\":\"WO-100\",\"workorderName\":\"Sample workorder\","
            + "\"workorderType\":\"SELF\",\"orderSource\":\"MANUAL\",\"productId\":12,"
            + "\"quantity\":\"10.000\",\"requestDate\":\"2026-10-23\",\"parentId\":0}";
    private ObjectMapper json;
    private JdbcTemplate jdbc;
    private ProWorkorderOperationService service;
    private IProWorkorderService workorders;
    private AtomicInteger writes;

    @BeforeEach
    void setup() {
        DataSource db = new EmbeddedDatabaseBuilder().setType(EmbeddedDatabaseType.H2)
                .setName("mes09-" + UUID.randomUUID()).build();
        jdbc = new JdbcTemplate(db);
        jdbc.execute("create table pro_workorder_operation (operation_id varchar(36) primary key,"
                + " action_type varchar(20), request_digest varchar(71), owner_id bigint,"
                + " workorder_id bigint, expected_version bigint, outcome varchar(16),"
                + " error_code varchar(64), workorder_code varchar(64), workorder_version bigint,"
                + " committed_at timestamp)");
        jdbc.execute("create table pro_workorder (workorder_id bigint primary key, workorder_code varchar(64), version bigint)");
        json = new ObjectMapper();
        service = new ProWorkorderOperationService();
        workorders = mock(IProWorkorderService.class);
        writes = new AtomicInteger();
        ReflectionTestUtils.setField(service, "mapper", new JdbcOperationMapper(jdbc));
        ReflectionTestUtils.setField(service, "workorders", workorders);
        ReflectionTestUtils.setField(service, "transactionManager", new DataSourceTransactionManager(db));
        ReflectionTestUtils.setField(service, "objectMapper", json);
        ReflectionTestUtils.setField(service, "validator", Validation.buildDefaultValidatorFactory().getValidator());
        when(workorders.createDraftWorkorder(any())).thenAnswer(invocation -> {
            ProWorkorder order = invocation.getArgument(0);
            writes.incrementAndGet();
            jdbc.update("insert into pro_workorder values (42, ?, 0)", order.getWorkorderCode());
            return 42L;
        });
        when(workorders.selectProWorkorderByWorkorderId(42L)).thenAnswer(invocation -> {
            ProWorkorder order = new ProWorkorder();
            order.setWorkorderId(42L);
            order.setWorkorderCode(jdbc.queryForObject("select workorder_code from pro_workorder where workorder_id=42", String.class));
            order.setVersion(jdbc.queryForObject("select version from pro_workorder where workorder_id=42", Long.class));
            return order;
        });
    }

    @Test
    void canonicalVectorAndReplaySurviveNewServiceInstance() throws Exception {
        ProWorkorderOperationRequest command = command(DRAFT);
        assertEquals("sha256:877e68bb22edbf476576ac950402b13ff40e0dc1b7b385a50f37ce2e1cdcd206",
                command.getRequestDigest());
        assertEquals(200, service.execute(command, 7L, "alice").get("code"));
        ProWorkorderOperationService restarted = new ProWorkorderOperationService();
        for (String field : new String[]{"mapper", "workorders", "transactionManager", "objectMapper", "validator"}) {
            ReflectionTestUtils.setField(restarted, field, ReflectionTestUtils.getField(service, field));
        }
        assertEquals(200, restarted.execute(command, 7L, "alice").get("code"));
        assertEquals(1, writes.get());
        assertEquals(1, count("pro_workorder"));
        assertEquals(1, count("pro_workorder_operation"));
        assertEquals(200, restarted.result(command.getOperationId(), 7L).get("code"));
    }

    @Test
    void changedPayloadAndOtherOwnerCannotReuseOrRead() throws Exception {
        ProWorkorderOperationRequest command = command(DRAFT);
        service.execute(command, 7L, "alice");
        ProWorkorderOperationRequest changed = command(DRAFT.replace("WO-100", "WO-101"));
        changed.setOperationId(command.getOperationId());
        assertEquals("OPERATION_CONFLICT", service.execute(changed, 7L, "alice").get("errorCode"));
        assertEquals("OPERATION_CONFLICT", service.execute(command, 8L, "bob").get("errorCode"));
        assertEquals("OPERATION_NOT_FOUND", service.result(command.getOperationId(), 8L).get("errorCode"));
        assertEquals(1, writes.get());
    }

    @Test
    void concurrentSameIdClaimsOnlyOneBusinessWrite() throws Exception {
        ProWorkorderOperationRequest command = command(DRAFT);
        ExecutorService workers = Executors.newFixedThreadPool(2);
        CountDownLatch start = new CountDownLatch(1);
        try {
            Future<AjaxResult> first = workers.submit(() -> { start.await(); return service.execute(command, 7L, "alice"); });
            Future<AjaxResult> second = workers.submit(() -> { start.await(); return service.execute(command, 7L, "alice"); });
            start.countDown();
            assertEquals(200, first.get(5, TimeUnit.SECONDS).get("code"));
            assertEquals(200, second.get(5, TimeUnit.SECONDS).get("code"));
            assertEquals(1, writes.get());
            assertEquals(1, count("pro_workorder"));
        } finally {
            workers.shutdownNow();
        }
    }

    @Test
    void businessFailureRollsBackClaimAndWorkorder() throws Exception {
        doThrow(new IllegalStateException("database failure")).when(workorders).createDraftWorkorder(any());
        AjaxResult result = service.execute(command(DRAFT), 7L, "alice");
        assertEquals("RESULT_UNKNOWN", result.get("errorCode"));
        assertEquals(0, count("pro_workorder"));
        assertEquals(0, count("pro_workorder_operation"));
    }

    @Test
    void editUsesVersionAndPersistsConflictForSameOperationId() throws Exception {
        jdbc.update("insert into pro_workorder values (42, 'WO-100', 0)");
        when(workorders.updateDraftWorkorder(any())).thenAnswer(invocation -> {
            ProWorkorder order = invocation.getArgument(0);
            int changed = jdbc.update("update pro_workorder set version=version+1,workorder_code=?"
                    + " where workorder_id=? and version=?", order.getWorkorderCode(),
                    order.getWorkorderId(), order.getVersion());
            if (changed != 1) throw new ServiceException("版本冲突", 409);
            writes.incrementAndGet();
            return 1;
        });
        ProWorkorderOperationRequest first = command(DRAFT.replace("\"workorderCode\":\"WO-100\"",
                "\"workorderId\":42,\"version\":0,\"workorderCode\":\"WO-101\""));
        first.setAction("EDIT_DRAFT");
        first.setExpectedVersion(0L);
        first.setRequestDigest(service.digest(first.getAction(), first.getDraft()));
        assertEquals(200, service.execute(first, 7L, "alice").get("code"));
        assertEquals(200, service.execute(first, 7L, "alice").get("code"));
        assertEquals(1, writes.get());

        ProWorkorderOperationRequest stale = command(DRAFT.replace("\"workorderCode\":\"WO-100\"",
                "\"workorderId\":42,\"version\":0,\"workorderCode\":\"WO-102\""));
        stale.setAction("EDIT_DRAFT");
        stale.setExpectedVersion(0L);
        stale.setRequestDigest(service.digest(stale.getAction(), stale.getDraft()));
        assertEquals("WORKORDER_CONFLICT", service.execute(stale, 7L, "alice").get("errorCode"));
        assertEquals("failed", ((ProWorkorderOperation) service.result(stale.getOperationId(), 7L)
                .get("data")).getOutcome());
        assertEquals("WORKORDER_CONFLICT", service.execute(stale, 7L, "alice").get("errorCode"));
        assertEquals(1, writes.get());
    }

    private ProWorkorderOperationRequest command(String draft) throws Exception {
        ProWorkorderOperationRequest command = new ProWorkorderOperationRequest();
        command.setOperationId(UUID.randomUUID().toString());
        command.setAction("CREATE_DRAFT");
        command.setDraft(json.readTree(draft));
        command.setRequestDigest(service.digest(command.getAction(), command.getDraft()));
        return command;
    }

    private int count(String table) {
        return jdbc.queryForObject("select count(*) from " + table, Integer.class);
    }

    private static class JdbcOperationMapper implements ProWorkorderOperationMapper {
        private final JdbcTemplate jdbc;
        JdbcOperationMapper(JdbcTemplate jdbc) { this.jdbc = jdbc; }

        @Override public ProWorkorderOperation selectById(String id) {
            return jdbc.query("select * from pro_workorder_operation where operation_id=?", rs -> {
                if (!rs.next()) return null;
                ProWorkorderOperation value = new ProWorkorderOperation();
                value.setOperationId(rs.getString("operation_id"));
                value.setAction(rs.getString("action_type"));
                value.setRequestDigest(rs.getString("request_digest"));
                value.setOwnerId(rs.getLong("owner_id"));
                value.setWorkorderId((Long) rs.getObject("workorder_id"));
                value.setExpectedVersion((Long) rs.getObject("expected_version"));
                value.setOutcome(rs.getString("outcome"));
                value.setErrorCode(rs.getString("error_code"));
                value.setWorkorderCode(rs.getString("workorder_code"));
                value.setWorkorderVersion((Long) rs.getObject("workorder_version"));
                value.setCommittedAt(rs.getTimestamp("committed_at"));
                return value;
            }, id);
        }

        @Override public int insert(ProWorkorderOperation value) {
            return jdbc.update("insert into pro_workorder_operation"
                    + " (operation_id,action_type,request_digest,owner_id,workorder_id,expected_version,outcome,error_code,committed_at)"
                    + " values (?,?,?,?,?,?,?,?,current_timestamp)", value.getOperationId(), value.getAction(),
                    value.getRequestDigest(), value.getOwnerId(), value.getWorkorderId(),
                    value.getExpectedVersion(), value.getOutcome(), value.getErrorCode());
        }

        @Override public int complete(ProWorkorderOperation value) {
            return jdbc.update("update pro_workorder_operation set workorder_id=?,workorder_code=?,"
                    + "workorder_version=?,outcome='succeeded' where operation_id=? and outcome='executing'",
                    value.getWorkorderId(), value.getWorkorderCode(), value.getWorkorderVersion(), value.getOperationId());
        }
    }
}
