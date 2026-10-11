import request from '@/utils/request'
import { buildWorkorderDraftPayload } from './workorderContract'

// 查询生产工单列表
export function listWorkorder(query) {
  return request({
    url: '/mes/pro/workorder/list',
    method: 'get',
    params: query
  })
}

// 查询生产工单详细
export function getWorkorder(workorderId) {
  return request({
    url: '/mes/pro/workorder/' + workorderId,
    method: 'get'
  })
}

// 新增生产工单
export function addWorkorder(data) {
  return request({
    url: '/mes/pro/workorder',
    method: 'post',
    data: buildWorkorderDraftPayload(data, false)
  })
}

// 修改生产工单
export function updateWorkorder(data) {
  return request({
    url: '/mes/pro/workorder',
    method: 'put',
    data: buildWorkorderDraftPayload(data, true)
  })
}

// 删除生产工单
export function delWorkorder(workorderId) {
  return request({
    url: '/mes/pro/workorder/' + workorderId,
    method: 'delete'
  })
}

// 查询物料依赖列表
export function listItems(query) {
  return request({
    url: '/mes/pro/workorder/listItems',
    method: 'get',
    params: query
  })
}

// 首页查询生产工单列表
export function getHomeList(query) {
  return request({
    url: '/mes/pro/workorder/getHomeList',
    method: 'get',
    params: query
  })
}
