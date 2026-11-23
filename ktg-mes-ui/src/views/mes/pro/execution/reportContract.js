const cents = value => {
  const raw = String(value == null ? '' : value).trim()
  if (!/^\d+(\.\d{1,2})?$/.test(raw)) return null
  const parts = raw.split('.')
  const number = Number(parts[0]) * 100 + Number((parts[1] || '').padEnd(2, '0'))
  return Number.isSafeInteger(number) ? number : null
}
const decimal = value => `${Math.floor(value / 100)}.${String(value % 100).padStart(2, '0')}`
const validId = id => Number.isSafeInteger(id) && id > 0

// The backend is authoritative: this only prevents accidental invalid submissions.
export function validateReport(selected, groups) {
  const fail = error => ({ error, totals: [], payload: null })
  const snIds = selected.map(row => row.snId)
  const unique = new Set(snIds)
  if (!snIds.length || unique.size !== snIds.length || !snIds.every(validId)) return fail('请选择有效且不重复的待装配 SN。')
  const pairs = new Set(), covered = new Set(), stockTotals = new Map(), inputs = []
  for (const group of groups) {
    const { stock, quantityPerUnit } = group
    const amount = cents(quantityPerUnit)
    if (!stock || !validId(stock.materialStockId) || !amount || !group.snIds.length) return fail('每组须选择真实库存、SN 和正数每件用量（最多两位小数）。')
    const id = stock.materialStockId
    for (const snId of group.snIds) {
      if (!unique.has(snId)) return fail('投入分组只能包含本次报工选中的 SN。')
      const key = `${snId}/${id}`
      if (pairs.has(key)) return fail('同一 SN 不得重复使用同一库存行，请调整分组。')
      pairs.add(key); covered.add(snId)
    }
    const total = (stockTotals.get(id) || {}).cents || 0
    const next = total + amount * group.snIds.length
    const balance = cents(stock.quantityOnhand)
    if (!Number.isSafeInteger(next) || balance === null || next > balance) return fail(`库存 ${stock.itemCode || id} / ${stock.sourceBatchCode || '无批次'} 用量超过可用余额。`)
    stockTotals.set(id, { stock, cents: next })
    inputs.push({ snIds: group.snIds.slice(), materialStockId: id, quantityPerUnit: String(quantityPerUnit).trim() })
  }
  if (covered.size !== unique.size) return fail('每个选中的 SN 至少需要一组真实投入；完整 BOM 要求由服务器校验。')
  return { error: '', payload: { snIds, inputs }, totals: Array.from(stockTotals.values(), v => ({ stock: v.stock, quantity: decimal(v.cents) })) }
}

export function pendingKey(userId) { return `mes-execution-pending-${userId}` }
const newId = () => {
  if (crypto.randomUUID) return crypto.randomUUID()
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128
  const hex = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('')
  return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join('-')
}
const errorCode = error => error.errorCode || (error.data && error.data.errorCode) || (error.response && error.response.data && error.response.data.errorCode)
const definite = ['VALIDATION_ERROR', 'FORBIDDEN', 'NOT_FOUND', 'STATE_CONFLICT', 'VERSION_CONFLICT', 'OPERATION_CONFLICT', 'INSUFFICIENT_STOCK', 'LEGACY_WRITE_PROTECTED']

// Preserve the exact request before dispatch. A timeout is never treated as a failure.
export class ExecutionCommand {
  constructor(storage, key, commands, lookup) { this.storage = storage; this.key = key; this.commands = commands; this.lookup = lookup; this.retryable = false }
  pending() {
    const raw = this.storage.getItem(this.key)
    if (!raw) return null
    return JSON.parse(raw)
  }
  async begin(action, taskId, payload) {
    this.retryable = false
    const pending = this.pending()
    if (pending) return { pending, message: '已有操作结果待确认，请先查询原回执。' }
    const request = { action, taskId, payload: { ...payload, operationId: newId() } }
    this.storage.setItem(this.key, JSON.stringify(request))
    return this.perform(request, false)
  }
  settle(request, response) {
    if (response && response.code === 200 && response.data && response.data.operationId === request.payload.operationId) {
      this.storage.removeItem(this.key); this.retryable = false
      return { response }
    }
    return { pending: request, message: '操作结果待确认，请查询原操作回执。' }
  }
  async perform(request, replay) {
    this.retryable = false
    try { return this.settle(request, await this.commands[request.action](request.taskId, request.payload)) } catch (error) {
      const code = errorCode(error)
      if (definite.includes(code) && !(replay && (code === 'FORBIDDEN' || code === 'NOT_FOUND'))) {
        this.storage.removeItem(this.key)
        return { message: error.message || '操作未提交' }
      }
      return { pending: request, message: error.message || '操作结果待确认' }
    }
  }
  async recover() {
    const request = this.pending()
    this.retryable = false
    if (!request) return {}
    try { return this.settle(request, await this.lookup(request.payload.operationId)) } catch (error) {
      this.retryable = errorCode(error) === 'NOT_FOUND'
      return { pending: request, retryable: this.retryable, message: this.retryable ? '暂无已提交回执，可按原操作 ID 和原请求重试。' : (error.message || '回执暂不可用') }
    }
  }
  async retry() {
    const request = this.pending()
    if (!request || !this.retryable) return { pending: request, message: '请先查询原操作回执。' }
    return this.perform(request, true)
  }
}
