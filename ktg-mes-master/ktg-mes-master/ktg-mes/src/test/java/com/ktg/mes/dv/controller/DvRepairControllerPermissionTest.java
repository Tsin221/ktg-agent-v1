package com.ktg.mes.dv.controller;

import org.junit.jupiter.api.Test;
import org.springframework.security.access.prepost.PreAuthorize;

import static org.junit.jupiter.api.Assertions.assertEquals;

class DvRepairControllerPermissionTest {
    @Test
    void createEndpointUsesRepairPageAddPermission() throws Exception {
        PreAuthorize permission = DvRepairController.class
                .getMethod("add", com.ktg.mes.dv.domain.DvRepair.class)
                .getAnnotation(PreAuthorize.class);

        assertEquals("@ss.hasPermi('mes:dv:repair:add')", permission.value());
    }
}
