package com.ktg.mes.pro.execution;

import com.ktg.common.core.domain.model.LoginUser;
import com.ktg.framework.web.service.PermissionService;
import com.ktg.mes.pro.execution.mapper.ExecutionTraceMapper;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.SqlSessionTemplate;
import org.springframework.core.io.ClassPathResource;
import org.springframework.security.core.context.SecurityContextHolder;

import javax.sql.DataSource;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;

@EnabledIfEnvironmentVariable(named="MES_B01_MYSQL_URL", matches=".+")
class ProExecutionUnitTraceMapperTest extends ExecutionInspectionFixture {
    static ExecutionTraceMapper traceMapper(DataSource source) throws Exception {
        SqlSessionFactoryBean factory = new SqlSessionFactoryBean();
        factory.setDataSource(source);
        factory.setMapperLocations(new ClassPathResource("mapper/pro/execution/ProExecutionTraceMapper.xml"));
        return new SqlSessionTemplate(factory.getObject()).getMapper(ExecutionTraceMapper.class);
    }

    static void permitTrace() {
        ((LoginUser)SecurityContextHolder.getContext().getAuthentication().getPrincipal()).getPermissions()
                .add("mes:pro:execution:trace");
    }

    private String snCode(long sn) {
        return db.queryForObject("select sn_code from wm_sn where sn_id=?", String.class, sn);
    }

    private Long packed;

    @Test void reworkTraceRetainsBothRoundsAndOnlyItsOwnReworkAndConsumption() throws Exception {
        permitTrace();
        LoginUser actor=(LoginUser)SecurityContextHolder.getContext().getAuthentication().getPrincipal();
        actor.getPermissions().addAll(Arrays.asList("mes:pro:execution:disposition","mes:pro:execution:manageAll",
                "mes:pro:execution:rework","mes:pro:execution:start"));
        long sn=sns.get(2);
        assertEquals(200,inspections.inspect(sn,check(false)).get("code"));
        long failed=db.queryForObject("select inspection_id from pro_execution_inspection where sn_id=?",Long.class,sn);
        ExecutionDispositionRequest decision=new ExecutionDispositionRequest();
        decision.operationId=UUID.randomUUID().toString();decision.inspectionId=failed;decision.decision="REWORK";decision.reason="repair and verify";
        assertEquals(200,new ExecutionDispositionService(source,operations,json).dispose(sn,decision).get("code"));
        long rw=db.queryForObject("select e.task_id from pro_execution_task e join pro_execution_disposition d on d.disposition_id=e.source_disposition_id where d.inspection_id=?",Long.class,failed);
        db.update("update pro_execution_task set assigned_user_id=77 where task_id=?",rw);
        ExecutionTaskQueryService tasks=new ExecutionTaskQueryService(source,access,new PermissionService());
        ExecutionTaskStartRequest start=new ExecutionTaskStartRequest();start.operationId=UUID.randomUUID().toString();
        assertEquals(200,new ExecutionTaskStartService(source,tasks,operations,json).start(rw,start).get("code"));
        assertEquals(200,new ExecutionReworkService(source,tasks,operations,json).complete(rw,UUID.randomUUID().toString()).get("code"));
        assertEquals(200,inspections.inspect(sn,check(true)).get("code"));
        long packing=db.queryForObject("select task_id from pro_execution_task where workorder_id=? and step_kind='PACKING'",Long.class,released.order);
        // This is a mapper fixture, not an application E2E; packing action is separately tested.
        db.update("insert into pro_execution_pack(sn_id,task_id,packed_by,packed_at) values(?,?,77,current_timestamp(3))",sn,packing);
        packed=sn;
        db.update("update pro_execution_unit set unit_state='PACKED' where sn_id=?",sn);
        ExecutionTraceService service=new ExecutionTraceService(traceMapper(source),access,new PermissionService());
        Map<String,Object> trace=service.unit(snCode(sn));
        assertEquals("TRACKED",trace.get("traceStatus"));
        assertEquals("PACKED",trace.get("unitState"));
        assertEquals(released.order,((Number)trace.get("workorderId")).longValue());
        List<Map<String,Object>> inputs=rows(trace.get("inputs"));
        assertEquals(1,inputs.size());assertEquals("B07",inputs.get(0).get("sourceBatchCode"));
        assertEquals(1,rows(inputs.get(0).get("transactions")).size());
        List<Map<String,Object>> checks=rows(trace.get("inspections"));
        assertEquals(2,checks.size());assertEquals("REJECT",checks.get(0).get("checkResult"));
        assertEquals("ACCEPT",checks.get(1).get("checkResult"));
        assertNull(checks.get(0).get("produceLineId"));assertNotNull(checks.get(1).get("produceLineId"));
        assertFalse(rows(checks.get(0).get("defects")).isEmpty());
        assertEquals(4,rows(trace.get("tasks")).size());assertEquals(1,rows(trace.get("dispositions")).size());
        assertNotNull(trace.get("packing"));assertNotNull(trace.get("assemblyFeedback"));
        Map<String,Object> unrelated=service.unit(snCode(sns.get(3)));
        assertEquals(3,rows(unrelated.get("tasks")).size());assertTrue(rows(unrelated.get("dispositions")).isEmpty());
    }

    @Test void scrapTraceHasFailureAndDecisionButNoPackingOrOutput() throws Exception {
        permitTrace();
        ((LoginUser)SecurityContextHolder.getContext().getAuthentication().getPrincipal()).getPermissions().add("mes:pro:execution:disposition");
        long sn=sns.get(0);
        assertEquals(200,inspections.inspect(sn,check(false)).get("code"));
        ExecutionDispositionRequest request=new ExecutionDispositionRequest();
        request.operationId=UUID.randomUUID().toString();request.decision="SCRAP";request.reason="not repairable";
        request.inspectionId=db.queryForObject("select inspection_id from pro_execution_inspection where sn_id=?",Long.class,sn);
        assertEquals(200,new ExecutionDispositionService(source,operations,json).dispose(sn,request).get("code"));
        Map<String,Object> trace=new ExecutionTraceService(traceMapper(source),access,new PermissionService()).unit(snCode(sn));
        assertEquals("SCRAPPED",trace.get("unitState"));assertNull(trace.get("packing"));
        assertEquals(1,rows(trace.get("inspections")).size());assertNull(rows(trace.get("inspections")).get(0).get("produceLineId"));
        assertEquals("SCRAP",rows(trace.get("dispositions")).get(0).get("decision"));
    }

    @Test void legacyIdentityRequiresExistingSnReadPermissionAndDoesNotInventHistory() throws Exception {
        permitTrace();
        String legacy=released.tag+"-legacy";
        db.update("insert into wm_sn(sn_code,item_id,item_code,item_name) values(?,?,?,?)",legacy,released.product,released.tag,released.tag);
        try {
            ExecutionTraceService service=new ExecutionTraceService(traceMapper(source),access,new PermissionService());
            assertEquals("NOT_FOUND",assertThrows(ExecutionException.class,()->service.unit(legacy)).getErrorCode());
            ((LoginUser)SecurityContextHolder.getContext().getAuthentication().getPrincipal()).getPermissions().add("mes:wm:sn:query");
            Map<String,Object> trace=service.unit(legacy);
            assertEquals("LEGACY_NO_RECORD",trace.get("traceStatus"));assertNull(trace.get("workorderId"));
            assertTrue(rows(trace.get("tasks")).isEmpty());assertTrue(rows(trace.get("inputs")).isEmpty());
            assertTrue(rows(trace.get("inspections")).isEmpty());assertNull(trace.get("packing"));
            assertEquals("NOT_FOUND",assertThrows(ExecutionException.class,()->service.unit(legacy+"-absent")).getErrorCode());
        } finally {db.update("delete from wm_sn where sn_code=?",legacy);}
    }

    @SuppressWarnings("unchecked")
    static List<Map<String,Object>> rows(Object value) {return (List<Map<String,Object>>)value;}

    @AfterEach void clearPack() {
        if(packed!=null) db.update("delete from pro_execution_pack where sn_id=?",packed);
    }
}
