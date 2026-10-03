package com.ktg.mes.pro.controller.vo;

import com.ktg.mes.pro.domain.ProWorkorder;

import java.util.ArrayList;
import java.util.List;

public class ProWorkorderHomeVO extends ProWorkorder
{
    private List<ProRouteHomeVO> routeHomg = new ArrayList<>();

    public List<ProRouteHomeVO> getRouteHomg()
    {
        return routeHomg;
    }

    public void setRouteHomg(List<ProRouteHomeVO> routeHomg)
    {
        this.routeHomg = routeHomg;
    }
}
