package com.ktg.mes.pro.execution;

import com.ktg.common.core.domain.entity.SysUser;
import com.ktg.common.core.domain.model.LoginUser;
import com.ktg.common.core.page.TableDataInfo;
import com.ktg.framework.web.service.PermissionService;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.context.SecurityContextHolder;
import java.util.*;
import java.util.stream.Collectors;
import static org.junit.jupiter.api.Assertions.*;

@EnabledIfEnvironmentVariable(named="MES_B01_MYSQL_URL",matches=".+")
class ProExecutionMaterialTraceMapperTest extends ExecutionConsumptionFixture {
    @Test void batchIdentityPreservesCaseAccentsAndWhitespace() throws Exception {
        assertEquals(200,reports.report(task,request(0,10,group(b06,0,2),group(b07,2,5),group(b08,5,10))).get("code"));
        ProExecutionUnitTraceMapperTest.permitTrace();
        ExecutionTraceService service=new ExecutionTraceService(ProExecutionUnitTraceMapperTest.traceMapper(source),access,new PermissionService());
        // Distinct actual inputs can have batch identifiers that compare equal under MySQL's
        // default collation, but stock matching uses exact byte semantics.
        db.update("update pro_execution_input set source_batch_code=? where sn_id=?","b07",sns.get(0));
        db.update("update pro_execution_input set source_batch_code=? where sn_id=?","B07 ",sns.get(1));
        db.update("update pro_execution_input set source_batch_code=? where sn_id=?","BÓ7",sns.get(5));
        db.update("update pro_execution_input set source_batch_code=? where sn_id=?","BO7",sns.get(6));
        assertEquals(sns.subList(2,5),ids(service.material(raw,"B07",1,20)));
        assertEquals(Collections.singletonList(sns.get(0)),ids(service.material(raw,"b07",1,20)));
        assertEquals(Collections.singletonList(sns.get(1)),ids(service.material(raw,"B07 ",1,20)));
        assertEquals(Collections.singletonList(sns.get(5)),ids(service.material(raw,"BÓ7",1,20)));
        assertEquals(Collections.singletonList(sns.get(6)),ids(service.material(raw,"BO7",1,20)));
    }

    @Test void correctMaterialBatchExactAndDeduplicatedWithScopeAppliedBeforePagination() throws Exception {
        assertEquals(200,reports.report(task,request(0,10,group(b06,0,2),group(b07,2,5),group(b08,5,10))).get("code"));
        ProExecutionUnitTraceMapperTest.permitTrace();
        ExecutionTraceService service=new ExecutionTraceService(ProExecutionUnitTraceMapperTest.traceMapper(source),access,new PermissionService());
        TableDataInfo page=service.material(raw,"B07",1,2);
        assertEquals(3L,page.getTotal());assertEquals(sns.subList(2,4),ids(page));
        assertEquals(sns.subList(4,5),ids(service.material(raw,"B07",2,2)));
        assertEquals(0L,service.material(raw,"B0",1,20).getTotal());
        assertEquals(0L,service.material(released.product,"B07",1,20).getTotal());

        // Arrange extra historical fact links in this disposable mapper fixture, not in the real closure E2E.
        long input=db.queryForObject("select input_id from pro_execution_input where sn_id=?",Long.class,sns.get(2));
        long originalLine=db.queryForObject("select consume_line_id from pro_execution_input where input_id=?",Long.class,input);
        Map<String,Object> original=db.queryForMap("select * from wm_item_consume_line where line_id=?",originalLine);
        Map<String,Object> copy=ExecutionFactWriter.copy(original,"record_id,material_stock_id,item_id,item_code,item_name,unit_of_measure,batch_code");
        copy.put("quantity_consume",1);
        long otherLine=ExecutionFactWriter.insert(db,"wm_item_consume_line","line_id",copy);
        db.update("insert into pro_execution_input(sn_id,task_id,feedback_id,item_id,material_stock_id,source_batch_code,consume_line_id,quantity,unit_of_measure,used_by,used_at) "+
                "select sn_id,task_id,feedback_id,item_id,material_stock_id,source_batch_code,?,quantity,unit_of_measure,used_by,used_at from pro_execution_input where input_id=?",otherLine,input);
        copy.put("item_id",released.product);
        long otherMaterialLine=ExecutionFactWriter.insert(db,"wm_item_consume_line","line_id",copy);
        db.update("insert into pro_execution_input(sn_id,task_id,feedback_id,item_id,material_stock_id,source_batch_code,consume_line_id,quantity,unit_of_measure,used_by,used_at) "+
                "select ?,task_id,feedback_id,?,material_stock_id,source_batch_code,?,quantity,unit_of_measure,used_by,used_at from pro_execution_input where input_id=?",
                sns.get(0),released.product,otherMaterialLine,input);
        try {
            // Same SN appears twice in matching input facts, not twice in affected rows.
            assertEquals(3L,service.material(raw,"B07",1,20).getTotal());
            assertEquals(sns.subList(2,5),ids(service.material(raw,"B07",1,20)));
            assertEquals(2L,((Number)ProExecutionUnitTraceMapperTest.rows(service.material(raw,"B07",1,20).getRows()).get(0).get("inputCount")).longValue());
            assertEquals(Collections.singletonList(sns.get(0)),ids(service.material(released.product,"B07",1,20)));
        } finally {
            db.update("delete from pro_execution_input where sn_id=? and consume_line_id=? and consume_line_id<>?",sns.get(2),otherLine,originalLine);
            db.update("delete from pro_execution_input where consume_line_id=?",otherMaterialLine);
            db.update("delete from wm_item_consume_line where line_id in (?,?)",otherLine,otherMaterialLine);
        }
        long qc=db.queryForObject("select task_id from pro_execution_task where workorder_id=? and step_kind='INSPECTION'",Long.class,released.order);
        db.update("update pro_execution_task set assigned_user_id=88 where task_id=?",qc);
        loginAs(88L);
        assertEquals(3L,service.material(raw,"B07",1,20).getTotal());
        loginAs(89L);
        assertEquals(0L,service.material(raw,"B07",1,20).getTotal());
        String code=db.queryForObject("select sn_code from wm_sn where sn_id=?",String.class,sns.get(2));
        assertEquals("NOT_FOUND",assertThrows(ExecutionException.class,()->service.unit(code)).getErrorCode());
    }
    static void loginAs(long id) {
        SysUser user=new SysUser();user.setUserId(id);user.setUserName("B06-test-"+id);
        LoginUser login=new LoginUser(id,1L,user,new HashSet<>(Arrays.asList("mes:pro:execution:query","mes:pro:execution:trace")));
        SecurityContextHolder.getContext().setAuthentication(new UsernamePasswordAuthenticationToken(login,null,Collections.emptyList()));
    }
    private static List<Long> ids(TableDataInfo page) {
        return ProExecutionUnitTraceMapperTest.rows(page.getRows()).stream().map(row->((Number)row.get("snId")).longValue()).collect(Collectors.toList());
    }
}
