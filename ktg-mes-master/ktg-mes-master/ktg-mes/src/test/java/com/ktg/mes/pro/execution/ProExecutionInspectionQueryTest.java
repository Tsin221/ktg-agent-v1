package com.ktg.mes.pro.execution;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.embedded.*;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;
class ProExecutionInspectionQueryTest {
 @Test void queryUsesFrozenTemplateAndNeverReturnsAnotherTask() {
  EmbeddedDatabase source=new EmbeddedDatabaseBuilder().generateUniqueName(true).setType(EmbeddedDatabaseType.H2).build();
  try {
   JdbcTemplate db=new JdbcTemplate(source);
   db.execute("create table pro_execution_task(task_id bigint,workorder_id bigint,task_kind varchar(32),step_kind varchar(32),inspection_snapshot_json varchar(2000))");
   db.execute("create table qc_defect(defect_id bigint,defect_name varchar(64),defect_level varchar(32),index_type varchar(32))");
   db.update("insert into pro_execution_task values(1,2,'NORMAL','INSPECTION',?)","{\"schemaVersion\":1,\"indices\":[{\"index_id\":7,\"index_type\":\"QUANTITATIVE\",\"threshold_min\":9,\"threshold_max\":11}]}");
   ExecutionAccessService access=mock(ExecutionAccessService.class);
   ExecutionInspectionQueryService query=new ExecutionInspectionQueryService(source,access,new ExecutionInspectionRecordService(source,new ObjectMapper()));
   assertNotNull(query.requirements(1L).get("template"));verify(access).requireTaskQuery(2L,1L);
   assertThrows(ExecutionException.class,()->query.requirements(3L));
   doThrow(ExecutionException.notFound()).when(access).requireTaskQuery(2L,1L);
   assertThrows(ExecutionException.class,()->query.requirements(1L));
  } finally {source.shutdown();}
 }
 @Test void releasedSnHistoryRemainsVisibleOnlyThroughCurrentOrHistoricalTaskScope() {
  EmbeddedDatabase source=new EmbeddedDatabaseBuilder().generateUniqueName(true).setType(EmbeddedDatabaseType.H2).build();
  try {
   JdbcTemplate db=new JdbcTemplate(source);
   db.execute("create table pro_execution_unit(sn_id bigint,workorder_id bigint)");
   db.execute("create table pro_execution_inspection(inspection_id bigint,sn_id bigint,task_id bigint,ipqc_id bigint,round_no int)");
   db.execute("create table qc_ipqc(ipqc_id bigint,check_result varchar(32),inspect_date timestamp,quantity_check int,quantity_qualified int,quantity_unqualified int)");
   db.execute("create table qc_ipqc_line(ipqc_id bigint,line_id bigint)");
   db.execute("create table qc_defect_record(qc_type varchar(32),qc_id bigint,record_id bigint)");
   db.execute("insert into pro_execution_unit values(1,2)");
   db.execute("insert into pro_execution_inspection values(5,1,3,4,1)");
   db.execute("insert into qc_ipqc values(4,'ACCEPT',current_timestamp,1,1,0)");
   ExecutionAccessService access=mock(ExecutionAccessService.class);
   doThrow(ExecutionException.notFound()).when(access).requireUnitQuery(2L,1L);
   ExecutionInspectionQueryService query=new ExecutionInspectionQueryService(source,access,new ExecutionInspectionRecordService(source,new ObjectMapper()));
   assertEquals(1,query.history(1L).size());verify(access).requireTaskQuery(2L,3L);
   doThrow(ExecutionException.notFound()).when(access).requireTaskQuery(2L,3L);
   assertThrows(ExecutionException.class,()->query.history(1L));
  } finally {source.shutdown();}
 }
}
