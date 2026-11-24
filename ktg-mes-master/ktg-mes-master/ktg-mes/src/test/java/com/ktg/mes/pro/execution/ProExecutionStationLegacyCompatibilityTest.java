package com.ktg.mes.pro.execution;

import com.ktg.mes.md.domain.MdWorkstation;
import com.ktg.mes.md.mapper.MdWorkstationMapper;
import com.ktg.mes.md.service.impl.MdWorkstationServiceImpl;
import org.junit.jupiter.api.Test;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.SqlSessionTemplate;
import org.springframework.core.io.ClassPathResource;
import org.springframework.dao.DataAccessException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.embedded.*;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.transaction.support.TransactionTemplate;

import java.util.UUID;
import static org.junit.jupiter.api.Assertions.*;

class ProExecutionStationLegacyCompatibilityTest {
    @Test void unmigratedStationRetirementWorksButPartialExecutionSchemaFailsClosed() throws Exception {
        EmbeddedDatabase db=new EmbeddedDatabaseBuilder().setType(EmbeddedDatabaseType.H2).setName("station-legacy-"+UUID.randomUUID()).build();
        try {
            JdbcTemplate jdbc=new JdbcTemplate(db);
            jdbc.execute("create table md_workstation(workstation_id bigint primary key,workstation_code varchar(64),workstation_name varchar(64),"
                    +"workstation_address varchar(64),workshop_id bigint,workshop_code varchar(64),workshop_name varchar(64),"
                    +"process_id bigint,process_code varchar(64),process_name varchar(64),warehouse_id bigint,warehouse_code varchar(64),warehouse_name varchar(64),"
                    +"location_id bigint,location_code varchar(64),location_name varchar(64),area_id bigint,area_code varchar(64),area_name varchar(64),"
                    +"enable_flag varchar(1),remark varchar(64),attr1 varchar(64),attr2 varchar(64),attr3 int,attr4 int,"
                    +"create_by varchar(64),create_time timestamp,update_by varchar(64),update_time timestamp)");
            jdbc.update("insert into md_workstation(workstation_id,process_id,enable_flag) values(1,1,'Y'),(2,1,'Y')");
            SqlSessionFactoryBean factory=new SqlSessionFactoryBean();
            factory.setDataSource(db);factory.setTypeAliasesPackage("com.ktg.mes.md.domain");
            factory.setMapperLocations(new ClassPathResource("mapper/md/MdWorkstationMapper.xml"));
            MdWorkstationServiceImpl service=new MdWorkstationServiceImpl();
            ReflectionTestUtils.setField(service,"mdWorkstationMapper",new SqlSessionTemplate(factory.getObject()).getMapper(MdWorkstationMapper.class));
            ReflectionTestUtils.setField(service,"executionOwnershipService",new ExecutionOwnershipService(db));
            TransactionTemplate tx=new TransactionTemplate(new DataSourceTransactionManager(db));
            MdWorkstation retire=new MdWorkstation();retire.setWorkstationId(1L);retire.setEnableFlag("N");
            assertEquals(1,tx.execute(s->service.updateMdWorkstation(retire)).intValue());
            assertEquals("N",jdbc.queryForObject("select enable_flag from md_workstation where workstation_id=1",String.class));
            assertEquals(1,tx.execute(s->service.deleteMdWorkstationByWorkstationId(1L)).intValue());
            // A deployment with one execution table is not a wholly old schema; missing remaining
            // tables are an error and must never be treated as permission to remove a station.
            jdbc.execute("create table pro_execution_order(workorder_id bigint primary key)");
            assertThrows(DataAccessException.class,()->tx.execute(s->service.deleteMdWorkstationByWorkstationId(2L)));
            assertEquals(1,jdbc.queryForObject("select count(*) from md_workstation where workstation_id=2",Integer.class));
        } finally {db.shutdown();}
    }
}
