import request from '@/utils/request'
const base = '/mes/pro/execution'
export const releaseOptions = id => request({ url: `${base}/workorders/${id}/release-options`, method: 'get' })
export const releaseOrder = (id, data) => request({ url: `${base}/workorders/${id}/releases`, method: 'post', data })
export const executionOrder = id => request({ url: `${base}/workorders/${id}`, method: 'get' })
export const executionOperation = id => request({ url: `${base}/operations/${id}`, method: 'get' })
