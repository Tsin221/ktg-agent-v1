package com.ktg.mes.pro.service.impl;

import com.ktg.common.exception.ServiceException;
import com.ktg.mes.md.mapper.MdProductBomMapper;
import com.ktg.mes.md.service.IMdItemService;
import com.ktg.mes.pro.mapper.ProWorkorderBomMapper;
import com.ktg.mes.pro.mapper.ProWorkorderMapper;
import com.ktg.mes.pro.service.IProFeedbackService;
import com.ktg.mes.pro.service.IProRouteProcessService;
import com.ktg.mes.pro.service.IProRouteProductService;
import com.ktg.mes.pro.service.IProWorkorderService;
import org.apache.ibatis.session.SqlSessionFactory;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mockito;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.SqlSessionTemplate;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.io.Resource;
import org.springframework.core.io.support.PathMatchingResourcePatternResolver;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabaseBuilder;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabaseType;
import org.springframework.test.context.ContextConfiguration;
import org.springframework.test.context.junit.jupiter.SpringExtension;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.annotation.EnableTransactionManagement;

import javax.sql.DataSource;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

@ExtendWith(SpringExtension.class)
@ContextConfiguration(classes = ProWorkorderMyBatisIntegrationTest.Config.class)
class ProWorkorderMyBatisIntegrationTest
{
    @Autowired
    private IProWorkorderService service;
    @Autowired
    private JdbcTemplate jdbc;

    @BeforeEach
    void resetDatabase()
    {
        jdbc.update("delete from pro_workorder_bom");
        jdbc.update("delete from pro_workorder");
        insertWorkorder(1L, "WO-MYBATIS-1", "PREPARE");
        insertWorkorder(2L, "WO-MYBATIS-2", "PREPARE");
        insertBom(1L, 101L);
        insertBom(2L, 102L);
    }

    @Test
    void productionMapperXmlDeletesDeduplicatedBatchAndAllBomRows()
    {
        assertEquals(2, service.deleteProWorkorderByWorkorderIds(new Long[]{2L, 1L, 1L}));

        assertEquals(0, count("select count(*) from pro_workorder"));
        assertEquals(0, count("select count(*) from pro_workorder_bom"));
    }

    @Test
    void productionForUpdateQueryRejectsMixedStatusWithoutChangingTables()
    {
        jdbc.update("update pro_workorder set status='CONFIRMED' where workorder_id=2");

        ServiceException error = assertThrows(ServiceException.class,
                () -> service.deleteProWorkorderByWorkorderIds(new Long[]{1L, 2L}));

        assertEquals(Integer.valueOf(409), error.getCode());
        assertEquals(2, count("select count(*) from pro_workorder"));
        assertEquals(2, count("select count(*) from pro_workorder_bom"));
    }

    private void insertWorkorder(Long id, String code, String status)
    {
        jdbc.update("insert into pro_workorder(workorder_id,workorder_code,workorder_name,workorder_type,order_source,product_id,quantity,parent_id,ancestors,status,version) values (?,?,?,?,?,?,?,?,?,?,?)",
                id, code, code, "SELF", "MANUAL", 10L, 1, 0L, "0", status, 0L);
    }

    private void insertBom(Long workorderId, Long itemId)
    {
        jdbc.update("insert into pro_workorder_bom(workorder_id,item_id,item_code,item_name,quantity) values (?,?,?,?,?)",
                workorderId, itemId, "M-" + itemId, "物料" + itemId, 1);
    }

    private int count(String sql)
    {
        return jdbc.queryForObject(sql, Integer.class);
    }

    @Configuration
    @EnableTransactionManagement
    static class Config
    {
        @Bean
        DataSource dataSource()
        {
            return new EmbeddedDatabaseBuilder()
                    .setType(EmbeddedDatabaseType.H2)
                    .setName("workorder_mybatis;MODE=MySQL;DB_CLOSE_DELAY=-1")
                    .addScript("classpath:pro-workorder-transaction-schema.sql")
                    .build();
        }

        @Bean
        JdbcTemplate jdbcTemplate(DataSource dataSource) { return new JdbcTemplate(dataSource); }

        @Bean
        PlatformTransactionManager transactionManager(DataSource dataSource)
        {
            return new DataSourceTransactionManager(dataSource);
        }

        @Bean
        SqlSessionFactory sqlSessionFactory(DataSource dataSource) throws Exception
        {
            PathMatchingResourcePatternResolver resolver = new PathMatchingResourcePatternResolver();
            List<Resource> mapperResources = new ArrayList<>();
            mapperResources.addAll(Arrays.asList(
                    resolver.getResources("classpath*:mapper/pro/ProWorkorderMapper.xml")));
            mapperResources.addAll(Arrays.asList(
                    resolver.getResources("classpath*:mapper/pro/ProWorkorderBomMapper.xml")));

            SqlSessionFactoryBean factory = new SqlSessionFactoryBean();
            factory.setDataSource(dataSource);
            factory.setTypeAliasesPackage("com.ktg.mes.pro.domain");
            factory.setMapperLocations(mapperResources.toArray(new Resource[0]));
            return factory.getObject();
        }

        @Bean
        SqlSessionTemplate sqlSessionTemplate(SqlSessionFactory sqlSessionFactory)
        {
            return new SqlSessionTemplate(sqlSessionFactory);
        }

        @Bean
        ProWorkorderMapper workorderMapper(SqlSessionTemplate template)
        {
            return template.getMapper(ProWorkorderMapper.class);
        }

        @Bean
        ProWorkorderBomMapper workorderBomMapper(SqlSessionTemplate template)
        {
            return template.getMapper(ProWorkorderBomMapper.class);
        }

        @Bean MdProductBomMapper productBomMapper() { return Mockito.mock(MdProductBomMapper.class); }
        @Bean IMdItemService itemService() { return Mockito.mock(IMdItemService.class); }
        @Bean IProRouteProductService routeProductService() { return Mockito.mock(IProRouteProductService.class); }
        @Bean IProRouteProcessService routeProcessService() { return Mockito.mock(IProRouteProcessService.class); }
        @Bean IProFeedbackService feedbackService() { return Mockito.mock(IProFeedbackService.class); }
        @Bean IProWorkorderService workorderService() { return new ProWorkorderServiceImpl(); }
    }
}
