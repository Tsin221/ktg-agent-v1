package com.ktg.mes.pro.execution;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.ktg.common.core.domain.entity.SysUser;
import com.ktg.common.core.domain.model.LoginUser;
import com.ktg.framework.web.service.PermissionService;
import com.ktg.system.service.ISysMenuService;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.context.SecurityContextHolder;
import javax.sql.DataSource;
import java.sql.Timestamp;
import java.util.*;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

/** Reuses a real B02 release fixture; operations run against the approved isolated MySQL schema. */
@EnabledIfEnvironmentVariable(named="MES_B01_MYSQL_URL",matches=".+")
class ProExecutionTaskMySqlTest {
    @Test void scopeAssignmentStartTimingAndSopPersistThroughReceipts() throws Exception {
        ProExecutionReleaseMySqlTest fixture=new ProExecutionReleaseMySqlTest();
        try {
            fixture.setup();
            JdbcTemplate db=fixture.db;
            assertEquals(200,fixture.service.release(fixture.order,fixture.request()).get("code"));
            DataSource source=db.getDataSource();
            ObjectMapper json=new ObjectMapper();
            PermissionService permissions=new PermissionService();
            ExecutionAccessService access=new ExecutionAccessService(new ExecutionOwnershipService(source),permissions);
            ExecutionOperationService operations=new ExecutionOperationService(ProExecutionOperationMapperTest.mapper(source),access,new DataSourceTransactionManager(source),json);
            ExecutionTaskQueryService tasks=new ExecutionTaskQueryService(source,access,permissions);
            ISysMenuService menus=mock(ISysMenuService.class);
            ExecutionAssignmentService assignments=new ExecutionAssignmentService(source,tasks,operations,access,menus,json);
            ExecutionTaskStartService starts=new ExecutionTaskStartService(source,tasks,operations,json);
            List<Long> ids=db.queryForList("select task_id from pro_execution_task where workorder_id=? order by normal_step_no",Long.class,fixture.order);
            Long assembly=ids.get(0), inspection=ids.get(1);
            Long executor=db.queryForObject("select user_id from sys_user where user_id<>1 and user_id<>77 and status='0' and del_flag='0' order by user_id limit 1",Long.class);
            when(menus.selectMenuPermsByUserId(executor)).thenReturn(perms("query","start","report","inspect"));
            db.update("insert into pro_process_content(process_id,order_num,content_text) values(?,1,'actual work guide')",fixture.processes.get(0));
            db.update("insert into md_product_sop(item_id,process_id,order_num,sop_title) values(?,?,1,'actual product SOP')",fixture.product,fixture.processes.get(0));
            db.update("insert into md_product_sop(item_id,process_id,order_num,sop_title) values(?,?,2,'other process SOP')",fixture.product,fixture.processes.get(1));
            login(executor,"query","start","report","inspect");
            assertEquals(0,tasks.list(1,20,fixture.order,null,null).getTotal());
            assertEquals(404,assertThrows(ExecutionException.class,()->tasks.detail(assembly)).getCode());
            assertEquals(403,starts.start(assembly,startRequest()).get("code"));
            login(77L,"query","assign");
            assertEquals(3,tasks.list(1,1,fixture.order,null,null).getTotal());
            assertEquals(1,tasks.list(1,1,fixture.order,null,null).getRows().size());
            assertEquals(10,tasks.detail(assembly).get("availableUnits"));
            assertTrue(assignments.options(assembly).stream().anyMatch(u->executor.equals(u.get("userId"))));
            assertEquals(400,assignments.assign(assembly,assignmentRequest(Long.MAX_VALUE)).get("code"));
            ExecutionTaskAssignmentRequest assignment=assignmentRequest(executor);
            assertEquals(200,assignments.assign(assembly,assignment).get("code"));
            assertEquals(200,assignments.assign(assembly,assignment).get("code"));
            assertEquals(200,assignments.assign(inspection,assignmentRequest(executor)).get("code"));
            login(executor,"query","start","report","inspect");
            assertEquals(2,tasks.list(1,20,fixture.order,null,null).getTotal());
            assertEquals(404,assertThrows(ExecutionException.class,()->tasks.units(ids.get(2),1,20,null,null)).getCode());
            assertEquals(10,tasks.units(assembly,1,5,null,null).getTotal());
            assertEquals(5,tasks.units(assembly,1,5,null,null).getRows().size());
            assertEquals(0,tasks.units(inspection,1,5,null,null).getTotal());
            Map<String,Object> detail=tasks.detail(assembly);
            assertEquals(true,detail.get("canStart"));
            assertEquals(1,((List<?>)detail.get("processContents")).size());
            assertEquals(1,((List<?>)detail.get("productSops")).size());
            assertEquals(409,starts.start(inspection,startRequest()).get("code"));
            Timestamp planned=db.queryForObject("select start_time from pro_task where task_id=?",Timestamp.class,assembly);
            ExecutionTaskStartRequest request=startRequest();
            assertEquals(200,starts.start(assembly,request).get("code"));
            Map<String,Object> startedDetail=tasks.detail(assembly);
            Timestamp actual=(Timestamp)startedDetail.get("actualStartedAt");
            assertNotNull(actual);
            Timestamp serverTime=(Timestamp)startedDetail.get("serverTime");
            assertFalse(serverTime.before(actual));
            assertEquals(db.queryForObject("select actual_started_at from pro_execution_task where task_id=?",Timestamp.class,assembly),actual);
            assertEquals(200,starts.start(assembly,request).get("code"));
            assertEquals(409,starts.start(assembly,startRequest()).get("code"));
            assertEquals(actual,tasks.detail(assembly).get("actualStartedAt"));
            assertEquals(executor,tasks.detail(assembly).get("startedBy"));
            assertEquals(true,tasks.detail(assembly).get("canReport"));
            assertEquals(planned,db.queryForObject("select start_time from pro_task where task_id=?",Timestamp.class,assembly));
            login(77L,"query","assign");
            assertEquals(409,assignments.assign(assembly,assignmentRequest(executor)).get("code"));
            assertEquals(4,fixture.count("pro_execution_operation")); // release, two assignments, start
            assertEquals(4L,db.queryForObject("select version from pro_workorder where workorder_id=?",Long.class,fixture.order));
        } finally {
            if(fixture.safe && !fixture.processes.isEmpty()) {
                fixture.db.update("delete from pro_process_content where process_id=?",fixture.processes.get(0));
                fixture.db.update("delete from md_product_sop where item_id=?",fixture.product);
            }
            fixture.cleanup();
        }
    }
    private static ExecutionTaskAssignmentRequest assignmentRequest(Long user) {
        ExecutionTaskAssignmentRequest request=new ExecutionTaskAssignmentRequest();request.assignedUserId=user;request.operationId=UUID.randomUUID().toString();return request;
    }
    private static ExecutionTaskStartRequest startRequest() {
        ExecutionTaskStartRequest request=new ExecutionTaskStartRequest();request.operationId=UUID.randomUUID().toString();return request;
    }
    private static Set<String> perms(String... suffixes) {
        Set<String> result=new HashSet<>();for(String suffix:suffixes)result.add("mes:pro:execution:"+suffix);return result;
    }
    private static void login(Long actor,String... suffixes) {
        SysUser user=new SysUser();user.setUserId(actor);user.setUserName("B03-test");
        LoginUser login=new LoginUser(actor,1L,user,perms(suffixes));
        SecurityContextHolder.getContext().setAuthentication(new UsernamePasswordAuthenticationToken(login,null,Collections.emptyList()));
    }
}
