package com.ktg.mes.pro.execution;

import com.ktg.common.core.domain.entity.*;
import com.ktg.system.strategy.*;
import com.ktg.system.service.*;
import com.ktg.system.service.impl.SysAutoCodeResultServiceImpl;
import com.ktg.system.mapper.SysAutoCodeResultMapper;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.mybatis.spring.*;
import org.springframework.core.io.ClassPathResource;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.*;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.transaction.support.TransactionTemplate;
import java.util.*;
import static org.mockito.Mockito.*;
import static org.junit.jupiter.api.Assertions.*;

@EnabledIfEnvironmentVariable(named="MES_B01_MYSQL_URL",matches=".+")
class ProExecutionAutoCodeMySqlTest {
 @Test void realAllocatorAndMapperProduceUniqueBatchAndRollbackRetryOnSameThread() throws Exception {
  DriverManagerDataSource ds=new DriverManagerDataSource(System.getenv("MES_B01_MYSQL_URL"),System.getenv("MES_B01_MYSQL_USER"),System.getenv("MES_B01_MYSQL_PASSWORD"));ds.setDriverClassName("com.mysql.cj.jdbc.Driver");
  JdbcTemplate db=new JdbcTemplate(ds);assertTrue(db.queryForObject("select database()",String.class).matches("(ktg_mes_b01_[a-z0-9_]+|ktg_mes_b04_test)"));
  SqlSessionFactoryBean f=new SqlSessionFactoryBean();f.setDataSource(ds);f.setTypeAliasesPackage("com.ktg.common.core.domain.entity");f.setMapperLocations(new ClassPathResource("mapper/system/SysAutoCodeResultMapper.xml"));
  SysAutoCodeResultServiceImpl results=new SysAutoCodeResultServiceImpl();ReflectionTestUtils.setField(results,"sysAutoCodeResultMapper",new SqlSessionTemplate(f.getObject()).getMapper(SysAutoCodeResultMapper.class));
  long ruleId=700000000000L+Math.abs(UUID.randomUUID().getLeastSignificantBits()%1000000000L);
  SysAutoCodeRule rule=new SysAutoCodeRule();rule.setRuleId(ruleId);rule.setRuleCode("B02-ALLOCATOR-TEST");rule.setIsPadded("N");
  SysAutoCodePart part=new SysAutoCodePart();part.setRuleId(ruleId);part.setPartType("SERIALNO");part.setCycleFlag("N");part.setSeriaStartNo(1);part.setSeriaStep(1);part.setPartLength(8);
  IAutoCodeRuleService rules=mock(IAutoCodeRuleService.class);when(rules.getOne(anyString())).thenReturn(rule);
  IAutoCodePartService parts=mock(IAutoCodePartService.class);when(parts.listPart(any())).thenReturn(Collections.singletonList(part));
  PartTypeSerialNoHandler serial=new PartTypeSerialNoHandler();ReflectionTestUtils.setField(serial,"iAutoCodeResultService",results);
  PartTypeHandler handler=mock(PartTypeHandler.class);when(handler.choiceExecute(any())).thenAnswer(call->serial.partHandle(call.getArgument(0)));
  AutoCodeUtil code=new AutoCodeUtil(); com.ktg.system.mapper.SysAutoCodeRuleMapper locks=mock(com.ktg.system.mapper.SysAutoCodeRuleMapper.class);when(locks.lockRule(anyString())).thenReturn(ruleId);ReflectionTestUtils.setField(code,"ruleLocks",locks);ReflectionTestUtils.setField(code,"iAutoCodeRuleService",rules);ReflectionTestUtils.setField(code,"iAutoCodePartService",parts);ReflectionTestUtils.setField(code,"iAutoCodeResultService",results);ReflectionTestUtils.setField(code,"partTypeHandler",handler);
  TransactionTemplate tx=new TransactionTemplate(new DataSourceTransactionManager(ds));
  // Entire fixture is rolled back; a second independent rollback repeats from serial 1.
  for(int round=0;round<2;round++)tx.execute(status->{Set<String> batch=new HashSet<>();for(int i=1;i<=20;i++){assertTrue(batch.add(code.genSerialCode(rule.getRuleCode(),"product")));assertNull(AutoCodeUtil.threadLocal.get());}assertEquals(1,db.queryForObject("select count(*) from sys_auto_code_result where rule_id=?",Integer.class,ruleId));status.setRollbackOnly();return null;});
  assertEquals(0,db.queryForObject("select count(*) from sys_auto_code_result where rule_id=?",Integer.class,ruleId));
 }
 @Test void legacySingleAllocatorWaitsForBatchWithoutJvmLockInversion() throws Exception {
  DriverManagerDataSource ds=new DriverManagerDataSource(System.getenv("MES_B01_MYSQL_URL"),System.getenv("MES_B01_MYSQL_USER"),System.getenv("MES_B01_MYSQL_PASSWORD"));ds.setDriverClassName("com.mysql.cj.jdbc.Driver");
  JdbcTemplate db=new JdbcTemplate(ds);assertTrue(db.queryForObject("select database()",String.class).matches("(ktg_mes_b01_[a-z0-9_]+|ktg_mes_b04_test)"));
  String ruleCode="B02-CONCURRENT-"+UUID.randomUUID();
  db.update("insert into sys_auto_code_rule(rule_code,rule_name,is_padded,enable_flag) values(?,?,'N','Y')",ruleCode,ruleCode);
  long ruleId=db.queryForObject("select rule_id from sys_auto_code_rule where rule_code=?",Long.class,ruleCode);
  java.util.concurrent.ExecutorService pool=java.util.concurrent.Executors.newFixedThreadPool(2);
  try {
   SqlSessionFactoryBean f=new SqlSessionFactoryBean();f.setDataSource(ds);f.setTypeAliasesPackage("com.ktg.common.core.domain.entity");f.setMapperLocations(new ClassPathResource("mapper/system/SysAutoCodeResultMapper.xml"));
   f.getObject().getConfiguration().addMapper(com.ktg.system.mapper.SysAutoCodeRuleMapper.class);
   SqlSessionTemplate session=new SqlSessionTemplate(f.getObject());
   SysAutoCodeResultServiceImpl results=new SysAutoCodeResultServiceImpl();ReflectionTestUtils.setField(results,"sysAutoCodeResultMapper",session.getMapper(SysAutoCodeResultMapper.class));
   SysAutoCodeRule rule=new SysAutoCodeRule();rule.setRuleId(ruleId);rule.setRuleCode(ruleCode);rule.setIsPadded("N");
   IAutoCodeRuleService rules=mock(IAutoCodeRuleService.class);when(rules.getOne(ruleCode)).thenReturn(rule);
   IAutoCodePartService parts=mock(IAutoCodePartService.class);when(parts.listPart(any())).thenAnswer(call->{SysAutoCodePart p=new SysAutoCodePart();p.setRuleId(ruleId);p.setPartType("SERIALNO");p.setCycleFlag("N");p.setSeriaStartNo(1);p.setSeriaStep(1);p.setPartLength(8);return Collections.singletonList(p);});
   PartTypeSerialNoHandler serial=new PartTypeSerialNoHandler();ReflectionTestUtils.setField(serial,"iAutoCodeResultService",results);
   PartTypeHandler handler=mock(PartTypeHandler.class);when(handler.choiceExecute(any())).thenAnswer(call->serial.partHandle(call.getArgument(0)));
   java.util.concurrent.CountDownLatch first=new java.util.concurrent.CountDownLatch(1), oldEntered=new java.util.concurrent.CountDownLatch(1);
   com.ktg.system.mapper.SysAutoCodeRuleMapper realLocks=session.getMapper(com.ktg.system.mapper.SysAutoCodeRuleMapper.class);
   com.ktg.system.mapper.SysAutoCodeRuleMapper observedLocks=mock(com.ktg.system.mapper.SysAutoCodeRuleMapper.class);
   when(observedLocks.lockRule(anyString())).thenAnswer(call->{if(Thread.currentThread().getName().equals("b02-old-allocator"))oldEntered.countDown();return realLocks.lockRule(call.getArgument(0));});
   AutoCodeUtil target=new AutoCodeUtil();ReflectionTestUtils.setField(target,"ruleLocks",observedLocks);ReflectionTestUtils.setField(target,"iAutoCodeRuleService",rules);ReflectionTestUtils.setField(target,"iAutoCodePartService",parts);ReflectionTestUtils.setField(target,"iAutoCodeResultService",results);ReflectionTestUtils.setField(target,"partTypeHandler",handler);
   DataSourceTransactionManager manager=new DataSourceTransactionManager(ds);
   org.springframework.aop.framework.ProxyFactory proxy=new org.springframework.aop.framework.ProxyFactory(target);proxy.setProxyTargetClass(true);
   proxy.addAdvice(new org.springframework.transaction.interceptor.TransactionInterceptor(manager,new org.springframework.transaction.annotation.AnnotationTransactionAttributeSource()));
   AutoCodeUtil code=(AutoCodeUtil)proxy.getProxy();
   java.util.concurrent.Future<List<String>> batch=pool.submit(()->new TransactionTemplate(manager).execute(status->{List<String> generated=new ArrayList<>();generated.add(code.genSerialCode(ruleCode,"P"));first.countDown();try{assertTrue(oldEntered.await(5,java.util.concurrent.TimeUnit.SECONDS));}catch(InterruptedException e){throw new RuntimeException(e);}for(int i=1;i<10;i++)generated.add(code.genSerialCode(ruleCode,"P"));return generated;}));
   java.util.concurrent.Future<String> old=pool.submit(()->{assertTrue(first.await(5,java.util.concurrent.TimeUnit.SECONDS));Thread.currentThread().setName("b02-old-allocator");return code.genSerialCode(ruleCode,"P");});
   List<String> codes=batch.get(15,java.util.concurrent.TimeUnit.SECONDS);String oldCode=old.get(15,java.util.concurrent.TimeUnit.SECONDS);
   assertEquals(10,new HashSet<>(codes).size());assertEquals("00000011",oldCode);assertFalse(codes.contains(oldCode));
   assertEquals(1,db.queryForObject("select count(*) from sys_auto_code_result where rule_id=?",Integer.class,ruleId));
   assertEquals(11,db.queryForObject("select last_serial_no from sys_auto_code_result where rule_id=?",Integer.class,ruleId));
  } finally {pool.shutdownNow();assertTrue(pool.awaitTermination(20,java.util.concurrent.TimeUnit.SECONDS));db.update("delete from sys_auto_code_result where rule_id=?",ruleId);db.update("delete from sys_auto_code_rule where rule_id=?",ruleId);}
 }

 @Test void literalAndDateBatchRuleInitializesWithoutSerialAndRollsBackOrSerializesConcurrentCalls() throws Exception {
  DriverManagerDataSource ds=new DriverManagerDataSource(System.getenv("MES_B01_MYSQL_URL"),System.getenv("MES_B01_MYSQL_USER"),System.getenv("MES_B01_MYSQL_PASSWORD"));
  ds.setDriverClassName("com.mysql.cj.jdbc.Driver");JdbcTemplate db=new JdbcTemplate(ds);
  assertTrue(db.queryForObject("select database()",String.class).matches("(ktg_mes_b01_[a-z0-9_]+|ktg_mes_b04_test)"));
  String ruleCode="B04-BATCH-"+UUID.randomUUID();
  db.update("insert into sys_auto_code_rule(rule_code,rule_name,is_padded,enable_flag) values(?,?,'N','Y')",ruleCode,ruleCode);
  long ruleId=db.queryForObject("select rule_id from sys_auto_code_rule where rule_code=?",Long.class,ruleCode);
  java.util.concurrent.ExecutorService pool=java.util.concurrent.Executors.newFixedThreadPool(2);
  try {
   SqlSessionFactoryBean factory=new SqlSessionFactoryBean();factory.setDataSource(ds);factory.setTypeAliasesPackage("com.ktg.common.core.domain.entity");
   factory.setMapperLocations(new ClassPathResource("mapper/system/SysAutoCodeResultMapper.xml"));
   factory.getObject().getConfiguration().addMapper(com.ktg.system.mapper.SysAutoCodeRuleMapper.class);
   SqlSessionTemplate sessions=new SqlSessionTemplate(factory.getObject());
   SysAutoCodeResultServiceImpl results=new SysAutoCodeResultServiceImpl();ReflectionTestUtils.setField(results,"sysAutoCodeResultMapper",sessions.getMapper(SysAutoCodeResultMapper.class));
   SysAutoCodeRule rule=new SysAutoCodeRule();rule.setRuleId(ruleId);rule.setRuleCode(ruleCode);rule.setIsPadded("N");
   SysAutoCodePart literal=new SysAutoCodePart();literal.setRuleId(ruleId);literal.setPartType("FIXCHAR");literal.setFixCharacter("BAT");
   SysAutoCodePart date=new SysAutoCodePart();date.setRuleId(ruleId);date.setPartType("NOWDATE");date.setDateFormat("yyyyMMdd");
   IAutoCodeRuleService rules=mock(IAutoCodeRuleService.class);when(rules.getOne(ruleCode)).thenReturn(rule);
   IAutoCodePartService parts=mock(IAutoCodePartService.class);when(parts.listPart(any())).thenReturn(Arrays.asList(literal,date));
   PartTypeHandler handler=mock(PartTypeHandler.class);
   when(handler.choiceExecute(any())).thenAnswer(call->{SysAutoCodePart part=call.getArgument(0);return "FIXCHAR".equals(part.getPartType())?new PartTypeFixCharHandler().partHandle(part):new PartTypeNowDateHandler().partHandle(part);});
   AutoCodeUtil code=new AutoCodeUtil();ReflectionTestUtils.setField(code,"ruleLocks",sessions.getMapper(com.ktg.system.mapper.SysAutoCodeRuleMapper.class));ReflectionTestUtils.setField(code,"iAutoCodeRuleService",rules);ReflectionTestUtils.setField(code,"iAutoCodePartService",parts);ReflectionTestUtils.setField(code,"iAutoCodeResultService",results);ReflectionTestUtils.setField(code,"partTypeHandler",handler);
   TransactionTemplate tx=new TransactionTemplate(new DataSourceTransactionManager(ds));
   tx.execute(status->{assertTrue(code.genSerialCode(ruleCode,"P").matches("BAT[0-9]{8}"));assertNull(AutoCodeUtil.threadLocal.get());status.setRollbackOnly();return null;});
   assertEquals(0,db.queryForObject("select count(*) from sys_auto_code_result where rule_id=?",Integer.class,ruleId));
   String first=tx.execute(status->code.genSerialCode(ruleCode,"P"));assertNull(AutoCodeUtil.threadLocal.get());
   assertEquals(first,tx.execute(status->code.genSerialCode(ruleCode,"P")));
   java.util.concurrent.CountDownLatch gate=new java.util.concurrent.CountDownLatch(1);
   java.util.concurrent.Future<String> a=pool.submit(()->{gate.await();return tx.execute(status->code.genSerialCode(ruleCode,"P"));});
   java.util.concurrent.Future<String> b=pool.submit(()->{gate.await();return tx.execute(status->code.genSerialCode(ruleCode,"P"));});gate.countDown();
   assertEquals(first,a.get(15,java.util.concurrent.TimeUnit.SECONDS));assertEquals(first,b.get(15,java.util.concurrent.TimeUnit.SECONDS));
   assertEquals(1,db.queryForObject("select count(*) from sys_auto_code_result where rule_id=?",Integer.class,ruleId));
   assertEquals(4,db.queryForObject("select gen_index from sys_auto_code_result where rule_id=?",Integer.class,ruleId));
   assertEquals(0,db.queryForObject("select last_serial_no from sys_auto_code_result where rule_id=?",Integer.class,ruleId));
   tx.execute(status->{code.genSerialCode(ruleCode,"P");status.setRollbackOnly();return null;});
   assertEquals(4,db.queryForObject("select gen_index from sys_auto_code_result where rule_id=?",Integer.class,ruleId));
  } finally {pool.shutdownNow();db.update("delete from sys_auto_code_result where rule_id=?",ruleId);db.update("delete from sys_auto_code_rule where rule_id=?",ruleId);}
 }
}
