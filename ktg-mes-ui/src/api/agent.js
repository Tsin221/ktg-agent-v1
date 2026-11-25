import request from '@/utils/request'
import store from '@/store'
import { getToken } from '@/utils/auth'
import { createAgentSessionClient } from './agentSession'

const sessionUrl = '/agent-api/v1/sessions'
const runUrl = '/agent-api/v1/runs'
const id = value => encodeURIComponent(value)

export const createAgentSession = title => request({
  baseURL: '', url: sessionUrl, method: 'post', data: title ? { title } : {},
  headers: { repeatSubmit: false }
})

export const renameAgentSession = (sessionId, title) => request({
  baseURL: '', url: sessionUrl + '/' + id(sessionId) + '/title', method: 'post', data: { title },
  headers: { repeatSubmit: false }
})

export const listAgentSessions = params => request({
  baseURL: '', url: sessionUrl, method: 'get', params
})

export const getAgentSession = (sessionId, { restoring = false } = {}) => request({
  baseURL: '', url: sessionUrl + '/' + id(sessionId), method: 'get',
  expectedErrorStatuses: restoring ? [404] : []
})

export const getAgentSessionStats = sessionId => request({
  baseURL: '', url: sessionUrl + '/' + id(sessionId) + '/stats', method: 'get'
})

export const listAgentMessages = (sessionId, params) => request({
  baseURL: '', url: sessionUrl + '/' + id(sessionId) + '/messages', method: 'get', params
})

export const listAgentTasks = sessionId => request({
  baseURL: '', url: sessionUrl + '/' + id(sessionId) + '/tasks', method: 'get'
})

export const listAgentOperations = sessionId => request({
  baseURL: '', url: sessionUrl + '/' + id(sessionId) + '/operations', method: 'get', expectedErrorStatuses: [404]
})

export const actOnAgentOperation = (sessionId, planId, action, digest) => request({
  baseURL: '', url: sessionUrl + '/' + id(sessionId) + '/operations/' + id(planId) + '/' + action,
  method: 'post', data: action === 'reconcile' ? {} : { digest },
  timeout: 120000, headers: { repeatSubmit: false }
})

export const getAgentTask = (sessionId, taskId) => request({
  baseURL: '', url: sessionUrl + '/' + id(sessionId) + '/tasks/' + id(taskId), method: 'get'
})

export const createAgentRun = (sessionId, { requestId, message, pageContext, taskId }) => request({
  baseURL: '', url: sessionUrl + '/' + id(sessionId) + '/runs', method: 'post',
  data: { request_id: requestId, message, page_context: pageContext || null, ...(taskId ? { task_id: taskId } : {}) },
  headers: { repeatSubmit: false }
})

export const getAgentRun = (runId, { restoring = false } = {}) => request({
  baseURL: '', url: runUrl + '/' + id(runId), method: 'get',
  expectedErrorStatuses: restoring ? [404] : []
})

export const cancelAgentRun = (runId, requestId) => request({
  baseURL: '', url: runUrl + '/' + id(runId) + '/cancel', method: 'post',
  data: { request_id: requestId }, headers: { repeatSubmit: false }
})

export const addAgentRunInput = (runId, { requestId, mode, message }) => request({
  baseURL: '', url: runUrl + '/' + id(runId) + '/inputs', method: 'post',
  data: { request_id: requestId, mode, message }, headers: { repeatSubmit: false }
})

export const listAgentRunInputs = (runId, params) => request({
  baseURL: '', url: runUrl + '/' + id(runId) + '/inputs', method: 'get', params
})

export const agentSession = createAgentSessionClient({
  api: { getSession: getAgentSession, getRun: getAgentRun, listMessages: listAgentMessages,
    createRun: createAgentRun, cancelRun: cancelAgentRun, addInput: addAgentRunInput,
    listInputs: listAgentRunInputs },
  getIdentity: () => ({
    token: store.state.user.token && store.state.user.token === getToken() ? store.state.user.token : null,
    userId: store.state.user.id
  }),
  storage: typeof window !== 'undefined' ? window.localStorage : null,
  fetchStream: (url, options) => fetch(url, options)
})

export function queryAgent(message) {
  return request({
    baseURL: '',
    url: '/agent-api/v1/query',
    method: 'post',
    data: { message },
    timeout: 120000,
    headers: { repeatSubmit: false }
  })
}
