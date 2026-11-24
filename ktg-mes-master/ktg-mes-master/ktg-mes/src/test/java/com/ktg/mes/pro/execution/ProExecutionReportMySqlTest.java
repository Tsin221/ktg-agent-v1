package com.ktg.mes.pro.execution;

import com.ktg.mes.wm.domain.WmTransaction;
import com.ktg.mes.wm.service.IWmTransactionService;
import com.ktg.common.core.domain.entity.SysUser;
import com.ktg.common.core.domain.model.LoginUser;
import com.ktg.framework.web.service.PermissionService;
import com.ktg.system.service.ISysMenuService;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.context.SecurityContextHolder;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

import java.util.Collections;
import java.util.Arrays;
import java.util.HashSet;
import java.util.UUID;
import java.sql.Timestamp;
import java.util.concurrent.atomic.AtomicBoolean;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

@EnabledIfEnvironmentVariable(named="MES_B01_MYSQL_URL", matches=".+")
class ProExecutionReportMySqlTest extends ExecutionConsumptionFixture {
    @Test void partialAndFinalReportConsumesRealBatchesOnceAndKeepsYieldZero() { assertPartialThenFinalReport(); }

    @Test void startedAssemblyCanBeHandedOverWithoutChangingFirstStartOrRepeatingConsumption() {
        assertEquals(200,reports.report(task,request(0,5,group(b06,0,2),group(b07,2,5))).get("code"));
        Timestamp actual=db.queryForObject("select actual_started_at from pro_execution_task where task_id=?",Timestamp.class,task);
        Long next=db.queryForObject("select user_id from sys_user where user_id<>1 and user_id<>77 and status='0' and del_flag='0' order by user_id limit 1",Long.class);
        HashSet<String> permissions=new HashSet<>(Arrays.asList("mes:pro:execution:query","mes:pro:execution:start","mes:pro:execution:report"));
        ISysMenuService menus=mock(ISysMenuService.class);
        when(menus.selectMenuPermsByUserId(next)).thenReturn(permissions);
        ExecutionTaskQueryService tasks=new ExecutionTaskQueryService(source,access,new PermissionService());
        ExecutionAssignmentService assignment=new ExecutionAssignmentService(source,tasks,operations,access,menus,json);
        ((LoginUser)SecurityContextHolder.getContext().getAuthentication().getPrincipal()).getPermissions().add("mes:pro:execution:assign");
        assertEquals(true,tasks.detail(task).get("canAssign"));
        assertTrue(assignment.options(task).stream().anyMatch(person->next.equals(person.get("userId"))));
        ExecutionTaskAssignmentRequest handoff=new ExecutionTaskAssignmentRequest();
        handoff.operationId=UUID.randomUUID().toString();handoff.assignedUserId=next;
        assertEquals(200,assignment.assign(task,handoff).get("code"));
        assertEquals(200,assignment.assign(task,handoff).get("code"));
        assertEquals(1,db.queryForObject("select count(*) from pro_execution_operation where operation_id=? and actor_id=77 and action_type='ASSIGN'",Integer.class,handoff.operationId));
        assertEquals(403,reports.report(task,request(5,10,group(b08,5,10))).get("code"));
        SysUser user=new SysUser();user.setUserId(next);user.setUserName("B03-handoff");user.setNickName("B03 handoff");
        LoginUser login=new LoginUser(next,1L,user,permissions);
        SecurityContextHolder.getContext().setAuthentication(new UsernamePasswordAuthenticationToken(login,null,Collections.emptyList()));
        assertEquals(true,tasks.detail(task).get("canReport"));
        assertEquals(200,reports.report(task,request(5,10,group(b08,5,10))).get("code"));
        assertEquals(200,reports.complete(task,UUID.randomUUID().toString()).get("code"));
        assertEquals(actual,db.queryForObject("select actual_started_at from pro_execution_task where task_id=?",Timestamp.class,task));
        assertEquals(77L,db.queryForObject("select started_by from pro_execution_task where task_id=?",Long.class,task));
        assertEquals(next,db.queryForObject("select ended_by from pro_execution_task where task_id=?",Long.class,task));
        assertEquals(5,db.queryForObject("select count(*) from pro_execution_input where task_id=? and used_by=?",Integer.class,task,next));
        assertEquals(10,countUnits("WAIT_INSPECTION"));assertBalance(b08,"0.00");
    }

    @Test void reportRequiresStartedTaskAndAssignedSn() {
        db.update("update pro_task set status='READY' where task_id=?",task);
        db.update("update pro_execution_task set started_by=null,actual_started_at=null where task_id=?",task);
        assertEquals(409,reports.report(task,request(0,2,group(b06,0,2))).get("code"));
        db.update("update pro_task set status='STARTED' where task_id=?",task);
        db.update("update pro_execution_task set started_by=77,actual_started_at=current_timestamp(3) where task_id=?",task);
        ExecutionInputGroup invalid=group(b06,0,1);
        invalid.snIds=Collections.singletonList(Long.MAX_VALUE);
        ExecutionReportRequest request=request(0,1,invalid);
        request.snIds=invalid.snIds;
        assertEquals(404,reports.report(task,request).get("code"));
        assertUnchanged();
    }

    @Test void failureAfterFirstRealStockDeductionRollsBackBothLedgerAndFeedback() {
        AtomicBoolean firstDebited=new AtomicBoolean();
        IWmTransactionService faulty=mock(IWmTransactionService.class);
        when(faulty.processTransaction(any(WmTransaction.class))).thenAnswer(call->{
            WmTransaction transaction=call.getArgument(0);
            if (Long.valueOf(b07).equals(transaction.getMaterialStockId())) {
                assertTrue(firstDebited.get());
                throw new IllegalStateException("injected failure after first physical ledger write");
            }
            WmTransaction committedInTransaction=ledger.processTransaction(transaction);
            firstDebited.set(true);
            return committedInTransaction;
        });
        assertEquals(202,newReportService(faulty).report(task,request(0,5,group(b06,0,2),group(b07,2,5))).get("code"));
        assertTrue(firstDebited.get());
        assertUnchanged();
    }

    private void assertUnchanged() {
        assertBalance(b06,"2.00"); assertBalance(b07,"5.00"); assertEquals(10,countUnits("WAIT_ASSEMBLY"));
        assertEquals(0,db.queryForObject("select count(*) from pro_feedback where task_id=?",Integer.class,task));
        assertEquals(0,db.queryForObject("select count(*) from wm_item_consume where workorder_id=?",Integer.class,released.order));
        assertEquals(0,db.queryForObject("select count(*) from pro_execution_input where task_id=?",Integer.class,task));
        assertEquals(0,db.queryForObject("select count(*) from wm_transaction where workorder_id=? and transaction_type='ITEM_CONSUME'",Integer.class,released.order));
        assertEquals(0,db.queryForObject("select count(*) from pro_execution_operation where workorder_id=? and action_type='REPORT'",Integer.class,released.order));
    }
}
