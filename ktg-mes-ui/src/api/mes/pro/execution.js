import request from '@/utils/request'
const base = '/mes/pro/execution'
export const releaseOptions = id => request({ url: `${base}/workorders/${id}/release-options`, method: 'get' })
export const releaseOrder = (id, data) => request({ url: `${base}/workorders/${id}/releases`, method: 'post', data })
export const executionOrder = id => request({ url: `${base}/workorders/${id}`, method: 'get' })
export const executionOperation = id => request({ url: `${base}/operations/${id}`, method: 'get' })
export const listExecutionTasks = params => request({ url: `${base}/tasks`, method: 'get', params })
export const getExecutionTask = id => request({ url: `${base}/tasks/${id}`, method: 'get' })
export const listExecutionTaskUnits = (id, params) => request({ url: `${base}/tasks/${id}/units`, method: 'get', params })
export const executionAssignmentOptions = id => request({ url: `${base}/tasks/${id}/assignment-options`, method: 'get' })
export const listExecutionMaterials = (id, params) => request({ url: `${base}/tasks/${id}/available-materials`, method: 'get', params })
// These commands have persistent server operation IDs and support exact retries.
const taskAction = (id, action, data) => request({ url: `${base}/tasks/${id}/${action}`, method: 'post', headers: { repeatSubmit: false }, data })
export const assignExecutionTask = (id, data) => taskAction(id, 'assignments', data)
export const startExecutionTask = (id, data) => taskAction(id, 'starts', data)
export const reportExecutionTask = (id, data) => taskAction(id, 'reports', data)
export const completeExecutionTask = (id, data) => taskAction(id, 'completions', data)
