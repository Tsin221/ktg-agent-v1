package com.ktg.mes.pro.service.impl;

import com.github.pagehelper.Page;
import com.github.pagehelper.PageHelper;
import com.github.pagehelper.PageInterceptor;
import com.ktg.common.exception.ServiceException;
import com.ktg.mes.pro.controller.ProFeedbackTimeRange;
import com.ktg.mes.pro.domain.ProFeedback;
import com.ktg.mes.pro.domain.ProTask;
import com.ktg.mes.pro.mapper.ProFeedbackMapper;
import com.ktg.mes.pro.mapper.ProTaskMapper;
import org.apache.ibatis.mapping.ResultMapping;
import org.apache.ibatis.session.SqlSessionFactory;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.SqlSessionTemplate;
import org.springframework.core.io.support.PathMatchingResourcePatternResolver;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabase;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabaseBuilder;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabaseType;
import java.math.BigDecimal;
import java.util.Date;
import java.util.List;
import java.util.Properties;
import java.util.stream.Collectors;
import static org.junit.jupiter.api.Assertions.*;

class ProductionQueryMapperTest {
    private EmbeddedDatabase db;
    private JdbcTemplate jdbc;
    private SqlSessionTemplate session;

    @BeforeEach
    void setup() throws Exception {
        db = new EmbeddedDatabaseBuilder().generateUniqueName(true).setType(EmbeddedDatabaseType.H2).build();
        jdbc = new JdbcTemplate(db);
        SqlSessionFactoryBean bean = new SqlSessionFactoryBean();
        bean.setDataSource(db);
        bean.setTypeAliasesPackage("com.ktg.mes.pro.domain");
        PathMatchingResourcePatternResolver resolver = new PathMatchingResourcePatternResolver();
        bean.setMapperLocations(resolver.getResource("classpath:mapper/pro/ProTaskMapper.xml"),
                resolver.getResource("classpath:mapper/pro/ProFeedbackMapper.xml"));
        PageInterceptor interceptor = new PageInterceptor();
        Properties properties = new Properties();
        properties.setProperty("helperDialect", "h2");
        interceptor.setProperties(properties);
        bean.setPlugins(interceptor);
        SqlSessionFactory factory = bean.getObject();
        session = new SqlSessionTemplate(factory);
        createTable(factory, "ProTask", "pro_task");
        createTable(factory, "ProFeedback", "pro_feedback");
        for (long id : new long[]{9, 2, 7, 1}) {
            jdbc.update("insert into pro_task(task_id,workorder_id) values (?,10)", id);
            jdbc.update("insert into pro_feedback(record_id,workorder_id,task_id,status,feedback_time) values (?,10,2,'APPROVED',?)",
                    id, java.sql.Timestamp.valueOf(id == 1 ? "2026-01-01 07:59:59" : id == 9 ? "2026-01-02 08:00:00" : "2026-01-01 08:00:00"));
        }
    }

    private void createTable(SqlSessionFactory factory, String type, String table) {
        List<ResultMapping> mappings = factory.getConfiguration().getResultMap(
                "com.ktg.mes.pro.mapper." + type + "Mapper." + type + "Result").getResultMappings();
        String columns = mappings.stream().map(m -> m.getColumn() + " " + sqlType(m.getJavaType()))
                .collect(Collectors.joining(","));
        jdbc.execute("create table " + table + " (" + columns + ")");
    }

    private String sqlType(Class<?> type) {
        if (Date.class.isAssignableFrom(type)) return "timestamp";
        if (type == Long.class || type == Integer.class) return "bigint";
        if (type == BigDecimal.class || type == Double.class) return "decimal(24,6)";
        return "varchar(255)";
    }

    @AfterEach
    void cleanup() {
        PageHelper.clearPage();
        if (db != null) db.shutdown();
    }

    @Test
    void taskPagesHaveFixedOrderAndServerTotal() {
        ProTask filter = new ProTask();
        filter.setWorkorderId(10L);
        PageHelper.startPage(1, 2);
        List<ProTask> first = session.getMapper(ProTaskMapper.class).selectProTaskList(filter);
        assertEquals(java.util.Arrays.asList(1L, 2L), first.stream().map(ProTask::getTaskId).collect(Collectors.toList()));
        assertEquals(4, ((Page<?>) first).getTotal());
        PageHelper.startPage(2, 2);
        List<ProTask> second = session.getMapper(ProTaskMapper.class).selectProTaskList(filter);
        assertEquals(java.util.Arrays.asList(7L, 9L), second.stream().map(ProTask::getTaskId).collect(Collectors.toList()));
    }

    @Test
    void feedbackRangeIsHalfOpenAndConvertsOffsetsBeforePagination() {
        ProFeedback filter = new ProFeedback();
        filter.setWorkorderId(10L);
        filter.setTaskId(2L);
        filter.setStatus("APPROVED");
        ProFeedbackTimeRange.apply(filter, "2026-01-01T00:00:00Z", "2026-01-02T08:00:00+08:00");
        PageHelper.startPage(1, 1);
        List<ProFeedback> rows = session.getMapper(ProFeedbackMapper.class).selectProFeedbackList(filter);
        assertEquals(2L, rows.get(0).getRecordId());
        assertEquals(2, ((Page<?>) rows).getTotal());
        PageHelper.startPage(2, 1);
        rows = session.getMapper(ProFeedbackMapper.class).selectProFeedbackList(filter);
        assertEquals(7L, rows.get(0).getRecordId());
        filter.setStatus("REJECTED");
        assertTrue(session.getMapper(ProFeedbackMapper.class).selectProFeedbackList(filter).isEmpty());
    }

    @Test
    void openBoundsAndLegacyUnfilteredRequestsWork() {
        ProFeedback filter = new ProFeedback();
        ProFeedbackTimeRange.apply(filter, null, "2026-01-01T08:00:00+08:00");
        assertEquals(1, session.getMapper(ProFeedbackMapper.class).selectProFeedbackList(filter).size());
        ProFeedbackTimeRange.apply(filter, "2026-01-02T00:00:00Z", null);
        assertEquals(1, session.getMapper(ProFeedbackMapper.class).selectProFeedbackList(filter).size());
        ProFeedbackTimeRange.apply(filter, null, null);
        List<ProFeedback> rows = session.getMapper(ProFeedbackMapper.class).selectProFeedbackList(filter);
        assertEquals(java.util.Arrays.asList(1L, 2L, 7L, 9L), rows.stream().map(ProFeedback::getRecordId).collect(Collectors.toList()));
    }

    @Test
    void invalidRangesAreRejected() {
        ProFeedback filter = new ProFeedback();
        for (String value : new String[]{"", "2026-01-01", "2026-01-01T00:00:00", "x' OR 1=1"}) {
            assertThrows(ServiceException.class, () -> ProFeedbackTimeRange.apply(filter, value, null));
        }
        assertThrows(ServiceException.class, () -> ProFeedbackTimeRange.apply(filter,
                "2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z"));
        assertThrows(ServiceException.class, () -> ProFeedbackTimeRange.apply(filter,
                "2026-01-01T00:00:00Z", "2026-01-01T08:00:00+08:00"));
    }
}
