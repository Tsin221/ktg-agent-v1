package com.ktg.mes.wm.service;

import com.ktg.common.core.domain.AjaxResult;
import com.ktg.common.core.domain.entity.SysUser;
import com.ktg.common.core.domain.model.LoginUser;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.test.context.ContextConfiguration;
import org.springframework.test.context.junit.jupiter.SpringExtension;

import java.util.Collections;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;

/** Run in a second Maven/JVM process after InventoryOperationServiceTest. */
@ExtendWith(SpringExtension.class)
@ContextConfiguration(classes = InventoryOperationServiceTest.Config.class)
class InventoryOperationRestartReplayTest {
    @Autowired private InventoryOperationService service;
    @Autowired private JdbcTemplate jdbc;

    @Test
    void committedReceiptReplaysAfterJavaProcessExit() {
        Assumptions.assumeTrue("ktg_s302_java_test".equals(System.getenv("KTG_S302_MYSQL_DB")));
        LoginUser login = new LoginUser();
        SysUser user = new SysUser();
        user.setUserName("operator");
        login.setUser(user);
        login.setUserId(7L);
        SecurityContextHolder.getContext().setAuthentication(
                new UsernamePasswordAuthenticationToken(login, null, Collections.emptyList()));

        AjaxResult replay = service.executeReceipt(1L, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa");
        AjaxResult queried = service.operationResult("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa");
        assertEquals(200, replay.get("code"));
        assertEquals(200, queried.get("code"));
        @SuppressWarnings("unchecked")
        Map<String, Object> data = (Map<String, Object>) replay.get("data");
        assertEquals(Boolean.TRUE, data.get("replayed"));
        @SuppressWarnings("unchecked")
        Map<String, Object> queriedData = (Map<String, Object>) queried.get("data");
        assertEquals("succeeded", queriedData.get("operationStatus"));
        assertEquals(1, jdbc.queryForObject("SELECT COUNT(*) FROM wm_transaction", Integer.class));
        assertEquals(1, jdbc.queryForObject(
                "SELECT COUNT(*) FROM wm_inventory_operation_transaction", Integer.class));
    }
}
