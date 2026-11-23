package com.ktg.mes.pro.execution;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.embedded.*;
import java.util.*;
import static org.mockito.Mockito.*;
import static org.junit.jupiter.api.Assertions.*;
class ProExecutionPackingQueryTest {
 @Test void onlyReleasedQualifiedUnpackedCurrentTaskUnitsAreSelectable() {
  EmbeddedDatabase source=new EmbeddedDatabaseBuilder().generateUniqueName(true).setType(EmbeddedDatabaseType.H2).build();
  try {
   JdbcTemplate db=new JdbcTemplate(source);
   db.execute("create table pro_execution_unit(sn_id bigint,workorder_id bigint,current_task_id bigint,unit_state varchar(32),version bigint)");
   db.execute("create table wm_sn(sn_id bigint,sn_code varchar(64))");
   db.execute("create table pro_workorder(workorder_id bigint,status varchar(32))");
   db.execute("create table pro_execution_inspection(sn_id bigint,ipqc_id bigint,produce_line_id bigint)");
   db.execute("create table qc_ipqc(ipqc_id bigint,check_result varchar(32),workorder_id bigint)");
   db.execute("create table pro_execution_pack(sn_id bigint)");
   db.execute("insert into pro_workorder values(2,'CONFIRMED')");
   for(int i=1;i<=4;i++) {db.update("insert into pro_execution_unit values(?,2,3,'WAIT_PACKING',0)",i);db.update("insert into wm_sn values(?,?)",i,"SN"+i);}
   db.execute("insert into pro_execution_inspection values(1,11,21),(2,12,22),(3,13,23)");
   db.execute("insert into qc_ipqc values(11,'ACCEPT',2),(12,'REJECT',2),(13,'ACCEPT',2)");
   db.execute("insert into pro_execution_pack values(3)");
   ExecutionTaskQueryService query=spy(new ExecutionTaskQueryService(source,mock(ExecutionAccessService.class),mock(com.ktg.framework.web.service.PermissionService.class)));
   Map<String,Object> task=new HashMap<>();task.put("workorder_id",2L);task.put("task_kind","NORMAL");task.put("step_kind","PACKING");doReturn(task).when(query).authorizedTask(3L);
   assertEquals(1,query.units(3L,1,10,null,null).getTotal());
   db.execute("update pro_workorder set status='FINISHED'");assertEquals(0,query.units(3L,1,10,null,null).getTotal());
  } finally {source.shutdown();}
 }
}
