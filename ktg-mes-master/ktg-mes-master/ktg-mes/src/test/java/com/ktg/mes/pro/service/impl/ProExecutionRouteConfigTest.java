package com.ktg.mes.pro.service.impl;
import com.ktg.mes.pro.domain.ProRouteProcess;
import com.ktg.mes.pro.mapper.ProRouteProcessMapper;
import com.ktg.common.exception.ServiceException;
import org.junit.jupiter.api.Test;
import org.springframework.test.util.ReflectionTestUtils;
import java.lang.reflect.Proxy;
import java.util.Collections;
import static org.junit.jupiter.api.Assertions.*;
class ProExecutionRouteConfigTest {
    private ProRouteProcessServiceImpl service(ProRouteProcess existing) {
        ProRouteProcessServiceImpl s = new ProRouteProcessServiceImpl();
        ProRouteProcessMapper mapper = (ProRouteProcessMapper) Proxy.newProxyInstance(getClass().getClassLoader(), new Class[]{ProRouteProcessMapper.class}, (p,m,a) -> m.getName().equals("toString") ? "RouteMapperTestDouble" : m.getName().equals("selectProRouteProcessList") ? (existing == null ? Collections.emptyList() : Collections.singletonList(existing)) : 1);
        ReflectionTestUtils.setField(s, "proRouteProcessMapper", mapper); return s;
    }
    private ProRouteProcess step(String kind, int order, long process) {
        ProRouteProcess p = new ProRouteProcess();p.setRouteId(1L);p.setProcessId(process);p.setExecutionKind(kind);p.setOrderNum(order);return p;
    }
    @Test void acceptsHistoricalNullAndAllKinds() {
        assertEquals(1,service(null).insertProRouteProcess(step(null,1,1)));
        for(String k : new String[]{"ASSEMBLY","INSPECTION","PACKING"}) assertEquals(1,service(null).insertProRouteProcess(step(k,1,1)));
    }
    @Test void rejectsInvalidKindOrderAndDuplicates() {
        assertThrows(ServiceException.class,()->service(null).insertProRouteProcess(step("assembly",1,1)));
        assertThrows(ServiceException.class,()->service(null).updateProRouteProcess(step("ASSEMBLY",0,1)));
        ProRouteProcess old=step("ASSEMBLY",1,1);old.setRecordId(2L);
        assertThrows(ServiceException.class,()->service(old).insertProRouteProcess(step("INSPECTION",1,3)));
        assertThrows(ServiceException.class,()->service(old).insertProRouteProcess(step("INSPECTION",2,1)));
    }
}
