package com.ktg.mes.pro.execution;

import com.fasterxml.jackson.annotation.JsonAnySetter;
import com.fasterxml.jackson.databind.annotation.JsonDeserialize;
import com.fasterxml.jackson.databind.JsonDeserializer;
import com.fasterxml.jackson.databind.DeserializationContext;
import com.fasterxml.jackson.core.JsonParser;
import com.fasterxml.jackson.core.JsonToken;
import java.io.IOException;
import java.util.List;

/** Only choices are supplied by the client; identities and types are read from storage. */
public class ExecutionReleaseRequest {
    public String operationId;
    @JsonDeserialize(using=StrictLong.class) public Long expectedVersion;
    @JsonDeserialize(using=StrictLong.class) public Long routeId;
    public List<Step> steps;
    @JsonAnySetter public void unknown(String name, Object value) {
        throw ExecutionException.validation("未知下达字段: " + name);
    }
    public static class Step {
        @JsonDeserialize(using=StrictLong.class) public Long routeProcessId;
        @JsonDeserialize(using=StrictLong.class) public Long workstationId;
        @JsonDeserialize(using=StrictLong.class) public Long templateId;
        public String ipqcType;
        @JsonAnySetter public void unknown(String name, Object value) {
            throw ExecutionException.validation("未知步骤字段: " + name);
        }
    }
    public static class StrictLong extends JsonDeserializer<Long> {
        @Override public Long deserialize(JsonParser parser, DeserializationContext context) throws IOException {
            if(parser.currentToken()!=JsonToken.VALUE_NUMBER_INT) throw context.weirdStringException(parser.getText(),Long.class,"必须为整数");
            return parser.getLongValue();
        }
    }
}
