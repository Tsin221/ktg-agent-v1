package com.ktg.mes.pro.service.impl;

import com.ktg.mes.pro.domain.ProWorkorderOperation;
import com.ktg.mes.pro.mapper.ProWorkorderOperationMapper;
import org.junit.jupiter.api.Test;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.SqlSessionTemplate;
import org.springframework.core.io.ClassPathResource;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabaseBuilder;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabaseType;

import javax.sql.DataSource;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.*;

class ProWorkorderOperationMapperTest {
    @Test
    void actualXmlPersistsClaimAndCommittedReceipt() throws Exception {
        DataSource db = new EmbeddedDatabaseBuilder().setType(EmbeddedDatabaseType.H2)
                .setName("mes09-mapper-" + UUID.randomUUID()).build();
        JdbcTemplate jdbc = new JdbcTemplate(db);
        jdbc.execute("create table pro_workorder_operation (operation_id varchar(36) primary key,"
                + " action_type varchar(20), request_digest varchar(71), owner_id bigint,"
                + " workorder_id bigint, expected_version bigint, outcome varchar(16),"
                + " error_code varchar(64), workorder_code varchar(64), workorder_version bigint,"
                + " committed_at timestamp)");
        SqlSessionFactoryBean factory = new SqlSessionFactoryBean();
        factory.setDataSource(db);
        factory.setMapperLocations(new ClassPathResource("mapper/pro/ProWorkorderOperationMapper.xml"));
        ProWorkorderOperationMapper mapper = new SqlSessionTemplate(factory.getObject())
                .getMapper(ProWorkorderOperationMapper.class);

        ProWorkorderOperation claim = new ProWorkorderOperation();
        claim.setOperationId(UUID.randomUUID().toString());
        claim.setAction("CREATE_DRAFT");
        claim.setRequestDigest("sha256:0000000000000000000000000000000000000000000000000000000000000000");
        claim.setOwnerId(7L);
        claim.setOutcome("executing");
        assertEquals(1, mapper.insert(claim));
        claim.setWorkorderId(42L);
        claim.setWorkorderCode("WO-100");
        claim.setWorkorderVersion(0L);
        assertEquals(1, mapper.complete(claim));
        ProWorkorderOperation saved = mapper.selectById(claim.getOperationId());
        assertEquals("succeeded", saved.getOutcome());
        assertEquals(42L, saved.getWorkorderId());
        assertNotNull(saved.getCommittedAt());
    }
}
