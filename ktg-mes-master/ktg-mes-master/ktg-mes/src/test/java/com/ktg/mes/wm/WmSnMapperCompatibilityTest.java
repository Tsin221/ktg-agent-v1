package com.ktg.mes.wm;

import com.ktg.mes.wm.domain.WmSn;
import com.ktg.mes.wm.mapper.WmSnMapper;
import org.junit.jupiter.api.Test;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.SqlSessionTemplate;
import org.springframework.core.io.ClassPathResource;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabase;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabaseBuilder;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabaseType;

import java.sql.Timestamp;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;

class WmSnMapperCompatibilityTest {
    @Test
    void readsActualLegacySchemaWithoutGenerationDateColumn() throws Exception {
        EmbeddedDatabase db = new EmbeddedDatabaseBuilder().generateUniqueName(true)
                .setType(EmbeddedDatabaseType.H2).build();
        try {
            JdbcTemplate jdbc = new JdbcTemplate(db);
            jdbc.execute("CREATE TABLE wm_sn (sn_id BIGINT PRIMARY KEY, sn_code VARCHAR(64), "
                    + "item_id BIGINT, item_code VARCHAR(64), item_name VARCHAR(255), specification VARCHAR(255), "
                    + "unit_of_measure VARCHAR(64), batch_code VARCHAR(255), remark VARCHAR(500), "
                    + "attr1 VARCHAR(255), attr2 VARCHAR(255), attr3 BIGINT, attr4 BIGINT, "
                    + "create_by VARCHAR(64), create_time TIMESTAMP, update_by VARCHAR(64), update_time TIMESTAMP)");
            Timestamp created = Timestamp.valueOf("2026-11-23 09:00:00");
            jdbc.update("INSERT INTO wm_sn (sn_id,sn_code,item_id,batch_code,create_time) VALUES (1,'A1',10,'B1',?),(2,'A2',10,'B1',?)",
                    created, created);
            SqlSessionFactoryBean factory = new SqlSessionFactoryBean();
            factory.setDataSource(db);
            factory.setTypeAliasesPackage("com.ktg.mes.wm.domain");
            factory.setMapperLocations(new ClassPathResource("mapper/wm/WmSnMapper.xml"));
            WmSnMapper mapper = new SqlSessionTemplate(factory.getObject()).getMapper(WmSnMapper.class);

            List<WmSn> grouped = mapper.selectSnList(new WmSn());
            assertEquals(1, grouped.size());
            assertEquals(2, grouped.get(0).getSnNum());
            assertEquals(created.getTime(), grouped.get(0).getGenDate().getTime());
            assertEquals(created.getTime(), mapper.selectWmSnBySnId(1L).getGenDate().getTime());
        } finally {
            db.shutdown();
        }
    }
}
