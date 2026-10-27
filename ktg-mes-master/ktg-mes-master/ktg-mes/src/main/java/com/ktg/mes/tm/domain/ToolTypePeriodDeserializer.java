package com.ktg.mes.tm.domain;

import java.io.IOException;
import java.math.BigInteger;

import com.fasterxml.jackson.core.JsonParser;
import com.fasterxml.jackson.core.JsonToken;
import com.fasterxml.jackson.databind.DeserializationContext;
import com.fasterxml.jackson.databind.JsonDeserializer;
import com.fasterxml.jackson.databind.JsonMappingException;

/** Strictly reads tool maintenance periods without Jackson's float-to-long coercion. */
public class ToolTypePeriodDeserializer extends JsonDeserializer<Long>
{
    private static final String ERROR_MESSAGE = "保养周期格式不正确，请输入大于0且不超过2147483647的整数";

    @Override
    public Long deserialize(JsonParser parser, DeserializationContext context) throws IOException
    {
        JsonToken token = parser.currentToken();
        if (token == JsonToken.VALUE_NULL)
        {
            return null;
        }
        if (token == JsonToken.VALUE_NUMBER_INT)
        {
            BigInteger value = parser.getBigIntegerValue();
            if (value.compareTo(BigInteger.valueOf(Long.MIN_VALUE)) < 0
                    || value.compareTo(BigInteger.valueOf(Long.MAX_VALUE)) > 0)
            {
                throw JsonMappingException.from(parser, ERROR_MESSAGE);
            }
            return value.longValue();
        }
        if (token == JsonToken.VALUE_STRING)
        {
            String value = parser.getText();
            if (value.isEmpty())
            {
                return null;
            }
            if (!value.matches("[0-9]+"))
            {
                throw JsonMappingException.from(parser, ERROR_MESSAGE);
            }
            try
            {
                return Long.valueOf(value);
            }
            catch (NumberFormatException ex)
            {
                throw JsonMappingException.from(parser, ERROR_MESSAGE);
            }
        }
        throw JsonMappingException.from(parser, ERROR_MESSAGE);
    }

    @Override
    public Long getNullValue(DeserializationContext context)
    {
        return null;
    }
}
