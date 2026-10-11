import request from '@/utils/request'

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

