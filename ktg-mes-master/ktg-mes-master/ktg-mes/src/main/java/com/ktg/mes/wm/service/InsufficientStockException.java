package com.ktg.mes.wm.service;

import com.ktg.common.exception.BussinessException;

/** Raised when an atomic stock deduction cannot satisfy the requested quantity. */
public class InsufficientStockException extends BussinessException {
    public InsufficientStockException() {
        super("库存数量不足！");
    }
}
