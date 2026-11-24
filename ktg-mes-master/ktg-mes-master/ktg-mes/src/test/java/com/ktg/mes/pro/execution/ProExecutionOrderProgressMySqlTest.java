package com.ktg.mes.pro.execution;

import com.ktg.common.core.domain.entity.SysUser;
import com.ktg.common.core.domain.model.LoginUser;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.aop.framework.ProxyFactory;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.transaction.annotation.AnnotationTransactionAttributeSource;
import org.springframework.transaction.interceptor.TransactionInterceptor;

import java.util.*;
import java.util.concurrent.*;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

@EnabledIfEnvironmentVariable(named="MES_B01_MYSQL_URL",matches=".+")
class ProExecutionOrderProgressMySqlTest extends ExecutionConsumptionFixture {
    private ExecutionOrderController controller(ExecutionReleaseService release) {
        ExecutionOrderController target=new ExecutionOrderController(release,new ExecutionOrderProgressService(source,access),mock(ExecutionCompletionService.class));
        ProxyFactory proxy=new ProxyFactory(target);
        proxy.setProxyTargetClass(true);
        proxy.addAdvice(new TransactionInterceptor(new DataSourceTransactionManager(source),new AnnotationTransactionAttributeSource()));
        return (ExecutionOrderController)proxy.getProxy();
    }

    private void loginAs(long userId, boolean manager) {
        SysUser user=new SysUser();user.setUserId(userId);user.setUserName("B07-"+userId);
        Set<String> permissions=new HashSet<>(Collections.singleton("mes:pro:execution:query"));
        if(manager)permissions.add("mes:pro:execution:manageAll");
        LoginUser principal=new LoginUser(userId,1L,user,permissions);
        SecurityContextHolder.getContext().setAuthentication(new UsernamePasswordAuthenticationToken(principal,null,Collections.emptyList()));
    }

    @Test void actorsSeeWholeOrderCountsButOnlyTheirOwnTaskAndCurrentSnDetails() {
        assertEquals(200,reports.report(task,request(0,2,group(b06,0,2))).get("code"));
        long inspection=db.queryForObject("select task_id from pro_execution_task where workorder_id=? and step_kind='INSPECTION'",Long.class,released.order);
        db.update("update pro_execution_task set assigned_user_id=78 where task_id=?",task);
        db.update("update pro_execution_task set assigned_user_id=79 where task_id=?",inspection);
        ExecutionOrderController controller=controller(released.service);
        loginAs(78,false);
        assertView(read(controller),Collections.singletonList(task),sns.subList(2,10),2L);
        loginAs(79,false);
        assertView(read(controller),Collections.singletonList(inspection),sns.subList(0,2),2L);
        loginAs(80,true);
        List<Long> allTasks=db.queryForList("select task_id from pro_execution_task where workorder_id=? order by normal_step_no,task_id",Long.class,released.order);
        assertView(read(controller),allTasks,sns,2L);
        released.login();
        assertView(read(controller),allTasks,sns,2L);
        loginAs(80,false);
        assertEquals(404,assertThrows(ExecutionException.class,()->read(controller)).getCode());
    }

    @Test void concurrentCommittedReportCannotSplitDetailAndProgressSnapshots() throws Exception {
        ExecutionReleaseService release=spy(released.service);
        ExecutorService writer=Executors.newSingleThreadExecutor();
        try {
            doAnswer(call->{
                Object before=call.callRealMethod();
                assertEquals(200,writer.submit(()->{
                    login();try{return reports.report(task,request(0,2,group(b06,0,2))).get("code");}finally{SecurityContextHolder.clearContext();}
                }).get(20,TimeUnit.SECONDS));
                return before;
            }).when(release).detail(released.order);
            Map<String,Object> detail=read(controller(release));
            assertEquals(10,rows(detail.get("units")).stream().filter(u->"WAIT_ASSEMBLY".equals(u.get("unitState"))).count());
            assertCounts(detail,0L);
            Map<String,Object> fresh=read(controller(released.service));
            assertCounts(fresh,2L);
        } finally {writer.shutdownNow();}
    }

    @SuppressWarnings("unchecked")
    private Map<String,Object> read(ExecutionOrderController controller) {return (Map<String,Object>)controller.detail(released.order).get("data");}

    private void assertView(Map<String,Object> view,List<Long> tasks,List<Long> units,long assembled) {
        assertCounts(view,assembled);
        assertEquals(tasks,ids(view.get("tasks"),"taskId"));
        assertEquals(tasks,ids(((Map<?,?>)view.get("progress")).get("tasks"),"taskId"));
        assertEquals(units,ids(view.get("units"),"snId"));
    }

    private void assertCounts(Map<String,Object> detail,long assembled) {
        Map<?,?> progress=(Map<?,?>)detail.get("progress"),summary=(Map<?,?>)detail.get("summary");
        assertEquals("CONFIRMED",progress.get("status"));
        assertEquals(10L,number(progress.get("planned")));
        assertEquals(10L,number(progress.get("allocated")));
        assertEquals(assembled,number(progress.get("assembled")));
        assertEquals(10-assembled,number(progress.get("waitingAssembly")));
        assertEquals(assembled,number(progress.get("waitingInspection")));
        for(String key:Arrays.asList("qualified","packed","scrapped","packable"))assertEquals(0L,number(progress.get(key)));
        for(String key:Arrays.asList("allocated","assembled","qualified","packed","scrapped"))assertEquals(number(progress.get(key)),number(summary.get(key)),key);
        assertEquals(false,progress.get("completionAllowed"));
    }

    private long number(Object value) {return ((Number)value).longValue();}
    private List<Long> ids(Object value,String key) {List<Long> ids=new ArrayList<>();for(Map<?,?> row:rows(value))ids.add(number(row.get(key)));return ids;}
    @SuppressWarnings("unchecked")
    private List<Map<String,Object>> rows(Object value) {return (List<Map<String,Object>>)value;}
}
