package com.ktg.mes.pro.execution;

import com.ktg.mes.pro.execution.mapper.ExecutionOperationMapper;
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

class ProExecutionOperationMapperTest {
    static final String DIGEST = "sha256:" + String.format("%064d", 0);

    @Test
    void xmlLocksWorkorderAndPersistsOnlyOneCommittedReceipt() throws Exception {
        DataSource db = new EmbeddedDatabaseBuilder().setType(EmbeddedDatabaseType.H2)
                .setName("mes-b01-" + UUID.randomUUID()).build();
        JdbcTemplate jdbc = new JdbcTemplate(db);
        jdbc.execute("CREATE TABLE pro_workorder (workorder_id BIGINT PRIMARY KEY)");
        jdbc.execute("CREATE TABLE pro_execution_operation (operation_id CHAR(36) PRIMARY KEY,"
                + " action_type VARCHAR(32), workorder_id BIGINT, actor_id BIGINT,"
                + " request_digest CHAR(71), result_ref VARCHAR(4096), committed_at TIMESTAMP)");
        jdbc.update("INSERT INTO pro_workorder VALUES (19)");
        ExecutionOperationMapper mapper = mapper(db);
        String id = UUID.randomUUID().toString();
        assertEquals(19L, mapper.lockWorkorder(19L));
        assertNull(mapper.lockWorkorder(20L));
        assertEquals(1, mapper.insertClaim(new ExecutionOperation(id, "REPORT", 19L, 7L, DIGEST)));
        assertNull(mapper.selectById(id).getCommittedAt());
        assertEquals(1, mapper.complete(id, "{\"schemaVersion\":1}"));
        assertEquals(0, mapper.complete(id, "{}"));
        ExecutionOperation receipt = mapper.selectById(id);
        assertEquals("{\"schemaVersion\":1}", receipt.getResultRef());
        assertEquals(7L, receipt.getActorId());
        assertEquals(DIGEST, receipt.getRequestDigest());
        assertNotNull(receipt.getCommittedAt());
        assertThrows(org.springframework.dao.DuplicateKeyException.class,
                () -> mapper.insertClaim(new ExecutionOperation(id, "REPORT", 19L, 7L, DIGEST)));
    }

    static ExecutionOperationMapper mapper(DataSource source) throws Exception {
        SqlSessionFactoryBean factory = new SqlSessionFactoryBean();
        factory.setDataSource(source);
        factory.setMapperLocations(new ClassPathResource("mapper/pro/execution/ProExecutionOperationMapper.xml"));
        return new SqlSessionTemplate(factory.getObject()).getMapper(ExecutionOperationMapper.class);
    }
}
