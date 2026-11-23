package com.ktg.mes.pro.execution;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.support.GeneratedKeyHolder;
import java.sql.Statement;
import java.util.*;
/** Only constant server-selected table/column names are accepted by callers. */
final class ExecutionFactWriter  {
    static long insert(JdbcTemplate db,String table,String key,Map<String,Object> values) {
        GeneratedKeyHolder keys=new GeneratedKeyHolder();
        String sql="insert into "+table+" ("+String.join(",",values.keySet())+") values ("+String.join(",",Collections.nCopies(values.size(),"?"))+")";
        int count=db.update(c-> {
            java.sql.PreparedStatement p=c.prepareStatement(sql,Statement.RETURN_GENERATED_KEYS);int i=1;for(Object value:values.values())p.setObject(i++,value);return p;
        }
        ,keys);
        if(count!=1||keys.getKey()==null)throw new IllegalStateException("Missing execution fact "+table);
        return keys.getKey().longValue();
    }
    static Map<String,Object> copy(Map<String,Object> source,String columns) {
        Map<String,Object> out=new LinkedHashMap<>();
        for(String c:columns.split(","))out.put(c,source.get(c));
        return out;
    }
    static Map<String,Object> values(Object... pairs) {
        Map<String,Object> out=new LinkedHashMap<>();
        for(int i=0;i<pairs.length;i+=2)out.put((String)pairs[i],pairs[i+1]);
        return out;
    }
}
