package com.ktg.mes.pro.execution;

import com.ktg.mes.qc.domain.*;
import com.ktg.mes.qc.service.impl.*;
import com.ktg.mes.wm.domain.*;
import com.ktg.mes.wm.service.impl.*;
import org.junit.jupiter.api.*;
import org.springframework.aop.aspectj.annotation.AspectJProxyFactory;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.embedded.*;
import java.util.UUID;
import static org.junit.jupiter.api.Assertions.*;

class ProExecutionLegacyQualityGuardTest {
    EmbeddedDatabase source;
    LegacyExecutionGuard guard;
    @BeforeEach void setup() {
        source=new EmbeddedDatabaseBuilder().setType(EmbeddedDatabaseType.H2).setName("quality-guard-"+UUID.randomUUID()).build();
        JdbcTemplate db=new JdbcTemplate(source);
        db.execute("create table pro_workorder(workorder_id bigint primary key,workorder_code varchar(64))");
        db.execute("create table pro_execution_order(workorder_id bigint primary key)");
        db.execute("create table pro_task(task_id bigint primary key,workorder_id bigint)");
        db.execute("create table pro_execution_task(task_id bigint primary key,workorder_id bigint)");
        db.execute("create table pro_execution_unit(sn_id bigint primary key,workorder_id bigint)");
        db.execute("create table pro_execution_inspection(sn_id bigint,ipqc_id bigint,produce_line_id bigint)");
        db.execute("create table qc_ipqc(ipqc_id bigint primary key,workorder_id bigint,task_id bigint)");
        db.execute("create table qc_ipqc_line(line_id bigint primary key,ipqc_id bigint)");
        db.execute("create table qc_defect_record(record_id bigint primary key,qc_id bigint,line_id bigint,qc_type varchar(32))");
        db.execute("create table wm_product_produce(record_id bigint primary key,workorder_id bigint,task_id bigint)");
        db.execute("create table wm_product_produce_line(line_id bigint primary key,record_id bigint)");
        db.update("insert into pro_workorder values(1,'BOUND'),(2,'OLD')");
        db.update("insert into pro_execution_order values(1)");
        db.update("insert into pro_task values(11,1),(22,2)");
        db.update("insert into pro_execution_task values(11,1)");
        db.update("insert into pro_execution_unit values(111,1)");
        // Historical headers intentionally point elsewhere: durable SN facts still protect them.
        db.update("insert into qc_ipqc values(10,2,22)");
        db.update("insert into qc_ipqc_line values(100,10)");
        db.update("insert into qc_defect_record values(1000,10,100,'IPQC')");
        db.update("insert into wm_product_produce values(20,2,22)");
        db.update("insert into wm_product_produce_line values(200,20)");
        db.update("insert into pro_execution_inspection values(111,10,200)");
        guard=new LegacyExecutionGuard(new ExecutionOwnershipService(source),new DataSourceTransactionManager(source));
    }
    @AfterEach void cleanup(){source.shutdown();}
    @Test void submittedQualityAndOutputCannotBeEditedOrDeletedThroughLegacyServices(){
        QcIpqc q=new QcIpqc();q.setIpqcId(10L);q.setWorkorderId(2L);q.setTaskId(22L);
        QcIpqcServiceImpl ipqc=proxy(new QcIpqcServiceImpl());
        refused(()->ipqc.updateQcIpqc(q));refused(()->ipqc.deleteQcIpqcByIpqcIds(new Long[]{10L}));
        QcIpqcLine l=new QcIpqcLine();l.setLineId(100L);l.setIpqcId(10L);
        QcIpqcLineServiceImpl lines=proxy(new QcIpqcLineServiceImpl());
        refused(()->lines.updateQcIpqcLine(l));refused(()->lines.deleteQcIpqcLineByLineIds(new Long[]{100L}));
        QcDefectRecord d=new QcDefectRecord();d.setRecordId(1000L);d.setQcType("IQC");d.setQcId(999L);
        QcDefectRecordServiceImpl defects=proxy(new QcDefectRecordServiceImpl());
        refused(()->defects.updateQcDefectRecord(d));refused(()->defects.deleteQcDefectRecordByRecordIds(new Long[]{1000L}));
        WmProductProduce h=new WmProductProduce();h.setRecordId(20L);h.setWorkorderId(2L);
        WmProductProduceServiceImpl output=proxy(new WmProductProduceServiceImpl());
        refused(()->output.updateWmProductProduce(h));refused(()->output.deleteWmProductProduceByRecordIds(new Long[]{20L}));
        WmProductProduceLine pl=new WmProductProduceLine();pl.setLineId(200L);pl.setRecordId(20L);
        WmProductProduceLineServiceImpl productLines=proxy(new WmProductProduceLineServiceImpl());
        refused(()->productLines.updateWmProductProduceLine(pl));refused(()->productLines.deleteWmProductProduceLineByLineIds(new Long[]{200L}));
    }
    private void refused(Runnable action){ExecutionException ex=assertThrows(ExecutionException.class,action::run);assertEquals("LEGACY_WRITE_PROTECTED",ex.getErrorCode());}
    private <T> T proxy(T target){AspectJProxyFactory factory=new AspectJProxyFactory(target);factory.setProxyTargetClass(true);factory.addAspect(guard);return factory.getProxy();}
}
