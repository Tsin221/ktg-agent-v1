package com.ktg.mes.pro.controller;

import com.github.pagehelper.PageHelper;
import com.ktg.mes.pro.domain.ProFeedback;
import com.ktg.mes.pro.service.IProFeedbackService;
import com.ktg.common.exception.ServiceException;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import java.util.Collections;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

class ProFeedbackQueryControllerTest {
    @AfterEach void cleanup() { PageHelper.clearPage(); }

    @Test
    void httpParametersAreValidatedAndPassedToService() throws Exception {
        IProFeedbackService service = mock(IProFeedbackService.class);
        when(service.selectProFeedbackList(any(ProFeedback.class))).thenReturn(Collections.emptyList());
        ProFeedbackController controller = new ProFeedbackController();
        ReflectionTestUtils.setField(controller, "proFeedbackService", service);
        MockMvcBuilders.standaloneSetup(controller).build().perform(get("/mes/pro/feedback/list")
                .param("taskId", "2").param("fromTime", "2026-01-01T00:00:00Z")
                .param("toTime", "2026-01-02T08:00:00+08:00"))
                .andExpect(status().isOk());
        ArgumentCaptor<ProFeedback> capture = ArgumentCaptor.forClass(ProFeedback.class);
        verify(service).selectProFeedbackList(capture.capture());
        assertEquals(2L, capture.getValue().getTaskId());
        assertEquals("2026-01-01 08:00:00.000000", capture.getValue().getParams().get("feedbackFrom"));
        assertEquals("2026-01-02 08:00:00.000000", capture.getValue().getParams().get("feedbackTo"));
    }

    @Test
    void invalidRangeNeverReachesService() {
        IProFeedbackService service = mock(IProFeedbackService.class);
        ProFeedbackController controller = new ProFeedbackController();
        ReflectionTestUtils.setField(controller, "proFeedbackService", service);
        assertThrows(ServiceException.class, () -> controller.list(new ProFeedback(), "not-a-date", null));
        verifyNoInteractions(service);
    }
}
