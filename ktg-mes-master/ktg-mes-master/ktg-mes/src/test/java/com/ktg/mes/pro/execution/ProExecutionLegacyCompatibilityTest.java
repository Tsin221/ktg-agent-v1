package com.ktg.mes.pro.execution;

import com.ktg.mes.pro.domain.ProWorkorder;
import com.ktg.mes.pro.mapper.ProWorkorderMapper;
import com.ktg.mes.pro.service.impl.ProWorkorderServiceImpl;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.SqlSessionTemplate;
import org.springframework.aop.aspectj.annotation.AspectJProxyFactory;
import org.springframework.core.io.ClassPathResource;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.test.util.ReflectionTestUtils;

import static org.junit.jupiter.api.Assertions.*;

/** Real MySQL, legacy mapper and guard: bound and unbound business can coexist. */
@EnabledIfEnvironmentVariable(named = "MES_B01_MYSQL_URL", matches = ".+")
class ProExecutionLegacyCompatibilityTest extends ExecutionConsumptionFixture {
    @Test void legacyWorkorderReadAndUpdateRemainAvailableWhileBoundOrderIsReadOnly() throws Exception {
        SqlSessionFactoryBean factory = new SqlSessionFactoryBean();
        factory.setDataSource(source);
        factory.setTypeAliasesPackage("com.ktg.mes.pro.domain");
        factory.setMapperLocations(new ClassPathResource("mapper/pro/ProWorkorderMapper.xml"));
        ProWorkorderMapper mapper = new SqlSessionTemplate(factory.getObject()).getMapper(ProWorkorderMapper.class);
        ProWorkorderServiceImpl target = new ProWorkorderServiceImpl();
        ReflectionTestUtils.setField(target, "proWorkorderMapper", mapper);
        AspectJProxyFactory proxy = new AspectJProxyFactory(target);
        proxy.setProxyTargetClass(true);
        proxy.addAspect(new LegacyExecutionGuard(ownership, new DataSourceTransactionManager(source)));
        ProWorkorderServiceImpl orders = proxy.getProxy();

        ProWorkorder legacy = orders.selectProWorkorderByWorkorderId(foreignOrder);
        ProWorkorder controlled = orders.selectProWorkorderByWorkorderId(released.order);
        assertNotNull(legacy);
        assertNotNull(controlled);
        assertNull(ownership.order(foreignOrder).releaseUserId);
        assertNotNull(ownership.order(released.order).releaseUserId);

        ProWorkorder legacyEdit = new ProWorkorder();
        legacyEdit.setWorkorderId(foreignOrder);
        legacyEdit.setWorkorderName(released.tag + "-legacy-edited");
        assertEquals(1, orders.updateProWorkorder(legacyEdit));
        assertEquals(legacyEdit.getWorkorderName(), orders.selectProWorkorderByWorkorderId(foreignOrder).getWorkorderName());

        ProWorkorder illegalEdit = new ProWorkorder();
        illegalEdit.setWorkorderId(released.order);
        illegalEdit.setStatus("FINISHED");
        ExecutionException rejected = assertThrows(ExecutionException.class, () -> orders.updateProWorkorder(illegalEdit));
        assertEquals("LEGACY_WRITE_PROTECTED", rejected.getErrorCode());
        assertEquals(controlled.getStatus(), orders.selectProWorkorderByWorkorderId(released.order).getStatus());
        assertEquals(10, countUnits("WAIT_ASSEMBLY"));
    }
}
