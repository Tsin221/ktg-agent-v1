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

export const listAgentSessions = params => request({
  baseURL: '', url: sessionUrl, method: 'get', params
})

export const getAgentSession = sessionId => request({
  baseURL: '', url: sessionUrl + '/' + id(sessionId), method: 'get'
})

export const getAgentSessionStats = sessionId => request({
  baseURL: '', url: sessionUrl + '/' + id(sessionId) + '/stats', method: 'get'
})

export const listAgentMessages = (sessionId, params) => request({
  baseURL: '', url: sessionUrl + '/' + id(sessionId) + '/messages', method: 'get', params
})

export const createAgentRun = (sessionId, { requestId, message, pageContext }) => request({
  baseURL: '', url: sessionUrl + '/' + id(sessionId) + '/runs', method: 'post',
  data: { request_id: requestId, message, page_context: pageContext || null },
  headers: { repeatSubmit: false }
})

export const getAgentRun = runId => request({
  baseURL: '', url: runUrl + '/' + id(runId), method: 'get'
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

export function confirmWorkorderDraft(previewId) {
  return request({
    baseURL: '',
    url: '/agent-api/v1/workorder-drafts/confirm',
    method: 'post',
    data: { preview_id: previewId },
    timeout: 120000,
    headers: { repeatSubmit: false }
  })
}

export function confirmInventoryOperation(operationId) {
  return request({
    baseURL: '', url: '/agent-api/v1/inventory-operations/confirm', method: 'post',
    data: { operation_id: operationId }, timeout: 120000,
    headers: { repeatSubmit: false }
  })
}

export function confirmDvMachineryType(previewId) {
  return request({
    baseURL: '',
    url: '/agent-api/v1/dv-machinerytype/confirm',
    method: 'post',
    data: { preview_id: previewId },
    timeout: 120000,
    headers: { repeatSubmit: false }
  })
}

export function confirmDvMachinery(previewId) {
  return request({
    baseURL: '',
    url: '/agent-api/v1/dv-machinery/confirm',
    method: 'post',
    data: { preview_id: previewId },
    timeout: 120000,
    headers: { repeatSubmit: false }
  })
}

export function confirmDvSubject(previewId) {
  return request({
    baseURL: '',
    url: '/agent-api/v1/dv-subject/confirm',
    method: 'post',
    data: { preview_id: previewId },
    timeout: 120000,
    headers: { repeatSubmit: false }
  })
}

export function confirmTmToolType(previewId) {
  return request({
    baseURL: '',
    url: '/agent-api/v1/tm-tooltype/confirm',
    method: 'post',
    data: { preview_id: previewId },
    timeout: 120000,
    headers: { repeatSubmit: false }
  })
}

export function confirmTmTool(previewId) {
  return request({
    baseURL: '',
    url: '/agent-api/v1/tm-tool/confirm',
    method: 'post',
    data: { preview_id: previewId },
    timeout: 120000,
    headers: { repeatSubmit: false }
  })
}

export function getInventoryOperation(operationId) {
  return request({
    baseURL: '', url: '/agent-api/v1/inventory-operations/' + encodeURIComponent(operationId),
    method: 'get', timeout: 30000
  })
}


export function confirmDvCheckPlan(previewId) {
  return request({
    baseURL: '', url: '/agent-api/v1/dv-checkplan/confirm', method: 'post',
    data: { preview_id: previewId }, timeout: 120000, headers: { repeatSubmit: false }
  })
}

export function confirmDvRepair(previewId) {
  return request({
    baseURL: '', url: '/agent-api/v1/dv-repair/confirm', method: 'post',
    data: { preview_id: previewId }, timeout: 120000, headers: { repeatSubmit: false }
  })
}
