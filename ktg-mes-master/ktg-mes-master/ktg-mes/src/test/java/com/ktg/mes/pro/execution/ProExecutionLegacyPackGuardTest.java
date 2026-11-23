package com.ktg.mes.pro.execution;
import org.junit.jupiter.api.Test;
import org.springframework.aop.aspectj.annotation.AspectJProxyFactory;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.embedded.*;
import com.ktg.mes.wm.service.impl.WmPackageLineServiceImpl;
import com.ktg.mes.wm.domain.WmPackageLine;
import static org.mockito.Mockito.*;
import static org.junit.jupiter.api.Assertions.*;
class ProExecutionLegacyPackGuardTest {
 @Test void oldPackageLineWritesReachServerGuard() {
  EmbeddedDatabase source=new EmbeddedDatabaseBuilder().generateUniqueName(true).setType(EmbeddedDatabaseType.H2).build();
  try {
   ExecutionOwnershipService ownership=mock(ExecutionOwnershipService.class);
   doThrow(new ExecutionException(409,"LEGACY_WRITE_PROTECTED","protected")).when(ownership).requireLegacyWritable(anyList());
   AspectJProxyFactory factory=new AspectJProxyFactory(new WmPackageLineServiceImpl());factory.setProxyTargetClass(true);
   factory.addAspect(new LegacyExecutionGuard(ownership,new DataSourceTransactionManager(source)));
   WmPackageLineServiceImpl service=factory.getProxy();WmPackageLine line=new WmPackageLine();line.setLineId(1L);line.setPackageId(2L);
   assertThrows(ExecutionException.class,()->service.updateWmPackageLine(line));
   assertThrows(ExecutionException.class,()->service.deleteWmPackageLineByLineIds(new Long[]{1L}));
   verify(ownership,times(2)).requireLegacyWritable(anyList());
  } finally {source.shutdown();}
 }
}
