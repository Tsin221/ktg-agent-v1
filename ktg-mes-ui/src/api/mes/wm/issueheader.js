import request from '@/utils/request'

// 查询生产领料单头列表
export function listIssueheader(query) {
  return request({
    url: '/mes/wm/issueheader/list',
    method: 'get',
    params: query
  })
}

// 查询生产领料单头详细
export function getIssueheader(issueId) {
  return request({
    url: '/mes/wm/issueheader/' + issueId,
    method: 'get'
  })
}

/**
 * 检查领料数量与拣货数量是否一致
 * @param  issueId 
 * @returns 
 */
export function checkQuantity(issueId) {
  return request({
    url: '/mes/wm/issueheader/checkQuantity/' + issueId,
    method: 'get'
  })
}


// 新增生产领料单头
export function addIssueheader(data) {
  return request({
    url: '/mes/wm/issueheader',
    method: 'post',
    data: data
  })
}

// 修改生产领料单头
export function updateIssueheader(data) {
  return request({
    url: '/mes/wm/issueheader',
    method: 'put',
    data: data
  })
}

// 删除生产领料单头
export function delIssueheader(issueId) {
  return request({
    url: '/mes/wm/issueheader/' + issueId,
    method: 'delete'
  })
}

export function previewExecution(issueId) {
  return request({ url: '/mes/wm/issueheader/' + issueId + '/execution-preview', method: 'get' })
}

export function getExecutionResult(issueId, operationId) {
  return request({ url: '/mes/wm/issueheader/' + issueId + '/operations/' + operationId, method: 'get' })
}

//执行领料
export function execute(issueId, version, operationId) {
  return request({
    url: '/mes/wm/issueheader/' + issueId,
    method: 'put',
    headers: {
      'X-Document-Version': version,
      'X-Operation-Id': operationId,
      repeatSubmit: false
    }
  })
}
