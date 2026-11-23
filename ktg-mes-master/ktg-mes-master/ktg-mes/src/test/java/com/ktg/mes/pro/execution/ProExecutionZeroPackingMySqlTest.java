package com.ktg.mes.pro.execution;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import com.ktg.common.core.domain.model.LoginUser;
import org.springframework.security.core.context.SecurityContextHolder;
import com.ktg.framework.web.service.PermissionService;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;
@EnabledIfEnvironmentVariable(named="MES_B01_MYSQL_URL",matches=".+")
class ProExecutionZeroPackingMySqlTest extends ExecutionInspectionFixture {
 @Test void allExplicitlyScrappedCanFinishWithoutInventingPackingStart() {
  ((LoginUser)SecurityContextHolder.getContext().getAuthentication().getPrincipal()).getPermissions().addAll(Arrays.asList("mes:pro:execution:disposition","mes:pro:execution:manageAll","mes:pro:execution:pack","mes:pro:execution:complete"));
  ExecutionDispositionService dispositions=new ExecutionDispositionService(source,operations,json);
  for(Long sn:sns) {
   assertEquals(200,inspections.inspect(sn,check(false)).get("code"));
   long failed=db.queryForObject("select inspection_id from pro_execution_inspection where sn_id=?",Long.class,sn);
   ExecutionDispositionRequest d=new ExecutionDispositionRequest();d.operationId=UUID.randomUUID().toString();d.inspectionId=failed;d.decision="SCRAP";d.reason="not repairable";
   assertEquals(200,dispositions.dispose(sn,d).get("code"));
  }
  assertEquals(200,reports.complete(task,UUID.randomUUID().toString()).get("code"));
  assertEquals(200,inspections.complete(inspectionTask,UUID.randomUUID().toString()).get("code"));
  long packing=db.queryForObject("select task_id from pro_execution_task where workorder_id=? and step_kind='PACKING'",Long.class,released.order);
  db.update("update pro_execution_task set assigned_user_id=77 where task_id=?",packing);
  ExecutionOrderProgressService progress=new ExecutionOrderProgressService(source,access);
  ExecutionCompletionService completion=new ExecutionCompletionService(source,operations,progress,new ExecutionTaskQueryService(source,access,new PermissionService()),json);
  String operation=UUID.randomUUID().toString();assertEquals(200,completion.completePacking(packing,operation).get("code"));assertEquals(200,completion.completePacking(packing,operation).get("code"));
  assertNull(db.queryForObject("select actual_started_at from pro_execution_task where task_id=?",java.sql.Timestamp.class,packing));
  assertEquals(0,db.queryForObject("select count(*) from pro_execution_pack where task_id=?",Integer.class,packing));
  assertEquals(0,db.queryForObject("select count(*) from wm_transaction where workorder_id=? and transaction_type='PRODUCT_PRODUCE'",Integer.class,released.order));
  ExecutionCompletionRequest r=new ExecutionCompletionRequest();r.operationId=UUID.randomUUID().toString();r.version=db.queryForObject("select version from pro_workorder where workorder_id=?",Long.class,released.order);
  assertEquals(200,completion.complete(released.order,r).get("code"));
 }
}
