package com.ktg.mes.pro.controller;

import com.ktg.common.exception.ServiceException;
import com.ktg.mes.pro.domain.ProFeedback;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;

/** Optional half-open report range; database wall-clock uses configured GMT+8. */
public final class ProFeedbackTimeRange {
    private ProFeedbackTimeRange() { }

    public static void apply(ProFeedback filter, String fromTime, String toTime) {
        OffsetDateTime from = parse(fromTime);
        OffsetDateTime to = parse(toTime);
        if (from != null && to != null && !from.toInstant().isBefore(to.toInstant())) {
            throw new ServiceException("时间范围终点必须晚于起点", 400);
        }
        // Ignore caller-supplied params map values; only validated public bounds apply.
        filter.getParams().remove("feedbackFrom");
        filter.getParams().remove("feedbackTo");
        if (from != null) filter.getParams().put("feedbackFrom", local(from));
        if (to != null) filter.getParams().put("feedbackTo", local(to));
    }

    private static OffsetDateTime parse(String value) {
        if (value == null) return null;
        try {
            if (!value.matches("\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(?:\\.\\d{1,6})?(?:Z|[+-]\\d{2}:\\d{2})")) {
                throw new DateTimeParseException("offset timestamp required", value, 0);
            }
            return OffsetDateTime.parse(value);
        } catch (DateTimeParseException ex) {
            throw new ServiceException("时间必须是带时区的 ISO8601 日期时间", 400);
        }
    }

    private static String local(OffsetDateTime value) {
        return value.withOffsetSameInstant(ZoneOffset.ofHours(8)).toLocalDateTime()
                .format(DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss.SSSSSS"));
    }
}
