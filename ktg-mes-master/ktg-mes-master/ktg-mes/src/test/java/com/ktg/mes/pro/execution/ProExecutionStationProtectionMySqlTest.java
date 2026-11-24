package com.ktg.mes.pro.execution;

import com.ktg.common.exception.ServiceException;
import com.ktg.mes.md.domain.MdWorkstation;
import com.ktg.mes.md.mapper.MdWorkstationMapper;
import com.ktg.mes.md.service.impl.MdWorkstationServiceImpl;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.SqlSessionTemplate;
import org.springframework.core.io.ClassPathResource;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.transaction.support.TransactionTemplate;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

/** Real isolated schema, real workstation mapper, explicit transaction around service calls. */
@EnabledIfEnvironmentVariable(named="MES_B01_MYSQL_URL",matches=".+")
class ProExecutionStationProtectionMySqlTest {
    @Test void activeOrderProtectsAllStationsIncludingFinishedAssemblyButAllowsRepairsAndUnrelatedEdits() throws Exception {
        ProExecutionReleaseMySqlTest f=new ProExecutionReleaseMySqlTest();
        try {
            f.setup();
            assertEquals(200,f.service.release(f.order,f.request()).get("code"));
            SqlSessionFactoryBean factory=new SqlSessionFactoryBean();
            factory.setDataSource(f.db.getDataSource());
            factory.setTypeAliasesPackage("com.ktg.mes.md.domain");
            factory.setMapperLocations(new ClassPathResource("mapper/md/MdWorkstationMapper.xml"));
            MdWorkstationMapper mapper=new SqlSessionTemplate(factory.getObject()).getMapper(MdWorkstationMapper.class);
            MdWorkstationServiceImpl service=new MdWorkstationServiceImpl();
            ReflectionTestUtils.setField(service,"mdWorkstationMapper",mapper);
            ReflectionTestUtils.setField(service,"executionOwnershipService",new ExecutionOwnershipService(f.db.getDataSource()));
            TransactionTemplate tx=new TransactionTemplate(new DataSourceTransactionManager(f.db.getDataSource()));
            Long station=f.stations.get(0), process=f.processes.get(0);
            // A completed assembly station is still the future station for any newly created rework task.
            f.db.update("update pro_task set status='FINISHED' where workstation_id=?",station);
            MdWorkstation disable=patch(station);disable.setEnableFlag("N");
            assertThrows(ServiceException.class,()->tx.execute(s->service.updateMdWorkstation(disable)));
            MdWorkstation move=patch(station);move.setProcessId(f.processes.get(1));
            assertThrows(ServiceException.class,()->tx.execute(s->service.updateMdWorkstation(move)));
            MdWorkstation moveStock=patch(station);moveStock.setWarehouseId(2L);
            assertThrows(ServiceException.class,()->tx.execute(s->service.updateMdWorkstation(moveStock)));
            assertThrows(ServiceException.class,()->tx.execute(s->service.deleteMdWorkstationByWorkstationId(station)));
            assertEquals("Y",mapper.selectMdWorkstationByWorkstationId(station).getEnableFlag());
            assertEquals(process,mapper.selectMdWorkstationByWorkstationId(station).getProcessId());

            MdWorkstation free=new MdWorkstation();free.setWorkstationCode(f.tag+"-free");free.setWorkstationName(f.tag+"-free");free.setProcessId(process);free.setEnableFlag("Y");free.setWarehouseId(1L);free.setLocationId(1L);free.setAreaId(1L);
            service.insertMdWorkstation(free);f.stations.add(free.getWorkstationId());
            assertThrows(ServiceException.class,()->tx.execute(s->service.deleteMdWorkstationByWorkstationIds(new Long[]{free.getWorkstationId(),station})));
            assertNotNull(mapper.selectMdWorkstationByWorkstationId(free.getWorkstationId()));
            assertEquals(1,tx.execute(s->service.updateMdWorkstation(patchWithRemark(station))).intValue());
            assertEquals("allowed note",mapper.selectMdWorkstationByWorkstationId(station).getRemark());
            // Simulate legacy drift in the test fixture; real service must permit restoring the required station.
            f.db.update("update md_workstation set enable_flag='N',process_id=? where workstation_id=?",f.processes.get(1),station);
            MdWorkstation repair=patch(station);repair.setEnableFlag("Y");repair.setProcessId(process);
            assertEquals(1,tx.execute(s->service.updateMdWorkstation(repair)).intValue());
            assertEquals(process,mapper.selectMdWorkstationByWorkstationId(station).getProcessId());
            assertEquals("Y",mapper.selectMdWorkstationByWorkstationId(station).getEnableFlag());

            // Retiring an unused station and one after order closure must remain possible.
            assertEquals(1,tx.execute(s->service.deleteMdWorkstationByWorkstationId(free.getWorkstationId())).intValue());
            f.db.update("update pro_workorder set status='FINISHED' where workorder_id=?",f.order);
            assertEquals(1,tx.execute(s->service.updateMdWorkstation(disable)).intValue());
            assertEquals(1,tx.execute(s->service.deleteMdWorkstationByWorkstationId(station)).intValue());
        } finally { f.cleanup(); }
    }

    @Test void concurrentReleaseAndRetirementCannotLeaveConfirmedOrderWithDisabledStation() throws Exception {
        ProExecutionReleaseMySqlTest f=new ProExecutionReleaseMySqlTest();
        ExecutorService workers=Executors.newFixedThreadPool(2);
        CountDownLatch releaseHasLockedStations=new CountDownLatch(1), retirementAttempting=new CountDownLatch(1);
        try {
            f.setup();
            SqlSessionFactoryBean factory=new SqlSessionFactoryBean();
            factory.setDataSource(f.db.getDataSource());factory.setTypeAliasesPackage("com.ktg.mes.md.domain");
            factory.setMapperLocations(new ClassPathResource("mapper/md/MdWorkstationMapper.xml"));
            MdWorkstationServiceImpl service=new MdWorkstationServiceImpl();
            ReflectionTestUtils.setField(service,"mdWorkstationMapper",new SqlSessionTemplate(factory.getObject()).getMapper(MdWorkstationMapper.class));
            ReflectionTestUtils.setField(service,"executionOwnershipService",new ExecutionOwnershipService(f.db.getDataSource()));
            TransactionTemplate tx=new TransactionTemplate(new DataSourceTransactionManager(f.db.getDataSource()));
            // SN creation happens after RELEASE locked all chosen stations. Keep that transaction
            // open until the competing master-data write has begun.
            AtomicInteger seq=new AtomicInteger();
            doAnswer(call->{
                releaseHasLockedStations.countDown();
                assertTrue(retirementAttempting.await(10,TimeUnit.SECONDS));
                return f.tag+"-"+seq.incrementAndGet();
            }).when(f.codes).genSerialCode(anyString(),anyString());
            MdWorkstation retire=patch(f.stations.get(0));retire.setEnableFlag("N");
            Future<Object> release=workers.submit(()->{f.login();try{return f.service.release(f.order,f.request()).get("code");}finally{org.springframework.security.core.context.SecurityContextHolder.clearContext();}});
            Future<Boolean> retirement=workers.submit(()->{
                assertTrue(releaseHasLockedStations.await(10,TimeUnit.SECONDS));
                retirementAttempting.countDown();
                try {tx.execute(s->service.updateMdWorkstation(retire));return true;}
                catch(ServiceException rejected){return false;}
            });
            assertEquals(200,release.get(20,TimeUnit.SECONDS));
            assertFalse(retirement.get(20,TimeUnit.SECONDS));
            assertEquals("CONFIRMED",f.db.queryForObject("select status from pro_workorder where workorder_id=?",String.class,f.order));
            assertEquals("Y",f.db.queryForObject("select enable_flag from md_workstation where workstation_id=?",String.class,f.stations.get(0)));
            assertEquals(3,f.count("pro_execution_task"));
        } finally {workers.shutdownNow();f.cleanup();}
    }

    private static MdWorkstation patch(Long id) { MdWorkstation station=new MdWorkstation();station.setWorkstationId(id);return station; }
    private static MdWorkstation patchWithRemark(Long id) { MdWorkstation station=patch(id);station.setRemark("allowed note");return station; }
}
