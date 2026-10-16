<template>
  <div class="agent-assistant">
    <button
      v-if="!open"
      class="agent-launcher"
      type="button"
      aria-label="打开 KTG 智能助手"
      @click="openPanel"
    >
      <i class="el-icon-chat-dot-round" aria-hidden="true" />
      <span>智能助手</span>
    </button>

    <section v-else class="agent-panel" role="dialog" aria-label="KTG 智能助手">
      <header class="agent-panel__header">
        <div>
          <h2>KTG 智能助手</h2>
          <p>使用当前登录账号权限</p>
        </div>
        <button ref="closeButton" type="button" aria-label="收起智能查询助手" @click="closePanel">
          <i class="el-icon-minus" aria-hidden="true" />
        </button>
      </header>

      <div class="agent-panel__sessions">
        <label for="agent-session-select">会话</label>
        <select id="agent-session-select" :value="sessionId" :disabled="sessionLoading" @change="selectSession($event.target.value)">
          <option value="">选择会话</option>
          <option v-for="session in sessions" :key="session.session_id" :value="session.session_id">{{ session.title || '未命名会话' }}</option>
        </select>
        <button type="button" :disabled="sessionLoading" @click="newSession">新会话</button>
      </div>

      <div ref="messages" class="agent-panel__messages" aria-live="polite">
        <div class="agent-message agent-message--assistant">
          我可以查询产品、分仓库存和生产工单。启用库存操作后，还可预览已准备的物料入库单和生产领料单。
        </div>
        <button
          v-for="example in examples"
          :key="example"
          type="button"
          class="agent-example"
          :disabled="loading"
          @click="submitExample(example)"
        >{{ example }}</button>

        <div v-for="message in messages" :key="message.id" :class="['agent-entry', `agent-entry--${message.role}`]">
          <div class="agent-message">{{ message.text }}</div>
          <div
            v-for="(result, index) in message.results || []"
            :key="`${message.id}-${index}`"
          >
            <agent-workorder-draft-card
              v-if="isWorkorderResult(result)"
              :result="result"
              :confirming="confirmingPreviewId === result.preview_id"
              :completed="completedPreviewIds.includes(result.preview_id)"
              @confirm="confirmPreview"
              @select-product="selectProduct($event, message.sourceText)"
              @view-workorder="viewWorkorder"
            />
            <agent-inventory-card
              v-else-if="isInventoryResult(result)"
              :result="result"
              :confirming="confirmingInventoryId === result.operation_id"
              :completed="completedInventoryIds.includes(result.operation_id)"
              @confirm="confirmInventory"
              @refresh="refreshInventory"
              @view="viewInventory"
            />
            <agent-result-card v-else :result="result" />
          </div>
        </div>
        <div v-if="pendingQuestion" class="agent-entry agent-entry--user">
          <div class="agent-message">{{ pendingQuestion }}</div>
        </div>
        <div v-if="progress.text || Object.keys(progress.tools).length" class="agent-progress" aria-label="本轮临时进度">
          <div v-if="progress.text" class="agent-message">{{ progress.text }}</div>
          <div v-for="(tool, id) in progress.tools" :key="id" class="agent-progress__tool">
            {{ tool.name }}：{{ tool.message || (tool.status === 'running' ? '执行中' : tool.status) }}
          </div>
        </div>
        <div v-if="activeRun || loading" class="agent-message agent-message--assistant agent-message--loading" role="status">
          {{ loading ? '正在提交问题…' : runStatusText(activeRun) }}
          <button v-if="streamError && activeRun && !isTerminalRun(activeRun.status)" type="button" @click="followRun">重新连接</button>
        </div>
        <div v-if="panelError" class="agent-message agent-message--error" role="alert">{{ panelError }}</div>
      </div>

      <form class="agent-panel__composer" @submit.prevent="send">
        <label class="sr-only" for="agent-query-input">输入查询问题</label>
        <textarea
          id="agent-query-input"
          ref="input"
          v-model="draft"
          rows="2"
          maxlength="2000"
          placeholder="例如：查询产品 A 的库存"
          :disabled="busy"
          @keydown.enter.exact.prevent="send"
        />
        <button type="submit" :disabled="busy || !draft.trim()">
          <i class="el-icon-s-promotion" aria-hidden="true" />
          <span>发送</span>
        </button>
      </form>
    </section>
  </div>
</template>

<script>
import { agentSession, createAgentSession, listAgentSessions, getAgentSession, listAgentMessages,
  getAgentRun, confirmWorkorderDraft, confirmInventoryOperation, getInventoryOperation } from '@/api/agent'
import AgentResultCard from './ResultCard.vue'
import AgentInventoryCard from './InventoryCard.vue'
import AgentWorkorderDraftCard from './WorkorderDraftCard.vue'
import { isWorkorderAssistantResult } from './workorderPresentation'
import { isTerminalRun, pageContext, presentMessages, progressFromEvent, runStatusText } from './sessionPresentation'

const requestId = () => (window.crypto && window.crypto.randomUUID)
  ? window.crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`

async function allMessages(sessionId) {
  const items = []
  let cursor
  do {
    const page = await listAgentMessages(sessionId, cursor ? { cursor } : undefined)
    items.push(...page.items)
    cursor = page.next_cursor
  } while (cursor)
  return items
}

export default {
  name: 'AgentAssistant',
  components: { AgentResultCard, AgentInventoryCard, AgentWorkorderDraftCard },
  data() {
    return {
      open: false,
      loading: false,
      sessionLoading: false,
      draft: '',
      sessions: [],
      sessionId: '',
      activeRunId: '',
      activeRun: null,
      pendingQuestion: '',
      pendingSubmission: null,
      progress: { text: '', tools: {}},
      panelError: '',
      streamError: false,
      epoch: 0,
      unsubscribeStore: null,
      confirmingPreviewId: '',
      confirmingInventoryId: '',
      completedInventoryIds: [],
      completedPreviewIds: [],
      messages: [],
      examples: ['查询编码为 A-01 的产品', '查询 A-01 的库存', '为产品 A 创建 100 件、指定日期的工单草稿']
    }
  },
  computed: {
    busy() {
      return this.loading || this.sessionLoading || (this.activeRun && !isTerminalRun(this.activeRun.status)) ||
        Boolean(this.confirmingPreviewId) || Boolean(this.confirmingInventoryId)
    }
  },
  mounted() {
    this.unsubscribeStore = this.$store.subscribe(mutation => {
      if (mutation.type === 'SET_TOKEN' || mutation.type === 'SET_ID') {
        this.resetIdentity()
        this.bootstrap()
      }
    })
    this.bootstrap()
  },
  beforeDestroy() {
    this.epoch++
    agentSession.close()
    if (this.unsubscribeStore) this.unsubscribeStore()
  },
  methods: {
    isTerminalRun,
    runStatusText,
    resetIdentity() {
      this.epoch++
      agentSession.checkIdentity()
      agentSession.close()
      this.sessions = []
      this.sessionId = ''
      this.activeRunId = ''
      this.activeRun = null
      this.pendingQuestion = ''
      this.pendingSubmission = null
      this.progress = { text: '', tools: {}}
      this.messages = []
      this.draft = ''
      this.panelError = ''
      this.streamError = false
      this.loading = false
      this.sessionLoading = false
      this.completedInventoryIds = []
      this.completedPreviewIds = []
      this.confirmingInventoryId = ''
      this.confirmingPreviewId = ''
    },
    async bootstrap() {
      if (!agentSession.checkIdentity()) return
      const epoch = this.epoch
      this.sessionLoading = true
      try {
        const page = await listAgentSessions({ limit: 50 })
        if (epoch !== this.epoch) return
        this.sessions = page.items || []
        let pointer
        try { pointer = await agentSession.restoreSession() } catch (_) { /* Saved session may have expired. */ }
        if (epoch !== this.epoch) return
        const sessionId = pointer && pointer.sessionId || (this.sessions[0] && this.sessions[0].session_id)
        if (sessionId) await this.loadSession(sessionId, epoch)
      } catch (error) {
        if (epoch === this.epoch) this.panelError = this.errorText(error, '会话读取失败，请稍后重试。')
      } finally {
        if (epoch === this.epoch) this.sessionLoading = false
      }
    },
    async loadSession(sessionId, epoch = ++this.epoch) {
      agentSession.close()
      this.sessionLoading = true
      this.panelError = ''
      this.streamError = false
      this.pendingQuestion = ''
      this.pendingSubmission = null
      this.progress = { text: '', tools: {}}
      this.activeRun = null
      this.activeRunId = ''
      this.messages = []
      try {
        const session = await getAgentSession(sessionId)
        if (epoch !== this.epoch) return
        if (!this.sessions.some(item => item.session_id === sessionId)) this.sessions.unshift(session)
        if (!agentSession.getState() || agentSession.getState().sessionId !== sessionId) agentSession.setSession(sessionId)
        this.sessionId = sessionId
        const items = await allMessages(sessionId)
        if (epoch !== this.epoch) return
        this.messages = presentMessages(items)
        const pointer = agentSession.getState()
        const runId = session.active_run_id || pointer && pointer.runId
        if (runId) {
          agentSession.setRun(runId)
          this.activeRunId = runId
          const run = await getAgentRun(runId)
          if (epoch !== this.epoch) return
          this.activeRun = run
          if (isTerminalRun(run.status)) {
            const finalItems = await allMessages(sessionId)
            if (epoch !== this.epoch) return
            this.messages = presentMessages(finalItems)
          } else this.followRun(epoch)
        }
        this.scrollToBottom()
      } catch (error) {
        if (epoch === this.epoch) this.panelError = this.errorText(error, '会话读取失败，请重新选择。')
      } finally {
        if (epoch === this.epoch) this.sessionLoading = false
      }
    },
    selectSession(sessionId) {
      if (sessionId && sessionId !== this.sessionId) this.loadSession(sessionId)
    },
    async newSession() {
      const epoch = ++this.epoch
      agentSession.close()
      this.sessionLoading = true
      try {
        const session = await createAgentSession()
        if (epoch !== this.epoch) return
        this.sessions.unshift(session)
        agentSession.setSession(session.session_id)
        await this.loadSession(session.session_id, epoch)
        this.$nextTick(() => this.$refs.input && this.$refs.input.focus())
      } catch (error) {
        if (epoch === this.epoch) this.panelError = this.errorText(error, '新建会话失败。')
      } finally {
        if (epoch === this.epoch) this.sessionLoading = false
      }
    },
    async followRun(epoch = this.epoch) {
      if (!this.activeRunId) return
      this.streamError = false
      try {
        await agentSession.subscribeRun({
          onEvent: event => {
            if (epoch !== this.epoch || event.run_id !== this.activeRunId) return
            if (event.type !== 'run_settled') this.progress = progressFromEvent(this.progress, event)
          },
          onSnapshot: snapshot => {
            if (epoch !== this.epoch || snapshot.run.run_id !== this.activeRunId) return
            this.activeRun = snapshot.run
            this.messages = presentMessages(snapshot.messages.items)
            if (isTerminalRun(snapshot.run.status)) {
              this.pendingQuestion = ''
              this.progress = { text: '', tools: {}}
            }
            this.scrollToBottom()
          }
        })
        if (epoch === this.epoch && this.activeRun && !isTerminalRun(this.activeRun.status)) {
          window.setTimeout(() => {
            if (epoch === this.epoch && this.activeRun && !isTerminalRun(this.activeRun.status)) this.followRun(epoch)
          }, 1000)
        }
      } catch (error) {
        if (epoch === this.epoch) {
          this.streamError = true
          this.panelError = this.errorText(error, '进度连接中断，请重新连接。')
        }
      }
    },
    errorText(error, fallback) {
      const data = error && error.response && error.response.data
      return data && data.error && data.error.message || error && error.message || fallback
    },
    openPanel() {
      this.open = true
      this.$nextTick(() => this.$refs.input && this.$refs.input.focus())
    },
    closePanel() {
      this.open = false
    },
    submitExample(example) {
      this.draft = example
      this.send()
    },
    async send() {
      const text = this.draft.trim()
      if (!text || this.busy) return
      this.draft = ''
      this.loading = true
      this.activeRun = null
      this.activeRunId = ''
      this.progress = { text: '', tools: {}}
      this.pendingQuestion = text
      this.panelError = ''
      const epoch = this.epoch
      this.scrollToBottom()
      try {
        if (!this.sessionId) {
          const session = await createAgentSession(text.slice(0, 60))
          if (epoch !== this.epoch) return
          this.sessions.unshift(session)
          agentSession.setSession(session.session_id)
          this.sessionId = session.session_id
        }
        const submission = this.pendingSubmission && this.pendingSubmission.message === text &&
          this.pendingSubmission.sessionId === this.sessionId
          ? this.pendingSubmission
          : { sessionId: this.sessionId, requestId: requestId(), message: text, pageContext: pageContext(this.$route) }
        this.pendingSubmission = submission
        const run = await agentSession.createRun(submission)
        if (epoch !== this.epoch || !run) return
        this.pendingSubmission = null
        this.activeRunId = run.run_id
        this.activeRun = { run_id: run.run_id, status: run.status }
        this.followRun(epoch)
      } catch (error) {
        if (epoch === this.epoch) {
          this.panelError = this.errorText(error, '提交状态未确认；再次发送相同问题会沿用同一请求。')
          this.draft = text
          this.pendingQuestion = ''
        }
      } finally {
        if (epoch === this.epoch) {
          this.loading = false
          this.scrollToBottom()
          if (this.open && !this.busy) this.$nextTick(() => this.$refs.input && this.$refs.input.focus())
        }
      }
    },
    isWorkorderResult(result) {
      return isWorkorderAssistantResult(result)
    },
    isInventoryResult(result) {
      return result && ['inventory_preview', 'inventory_result'].includes(result.kind)
    },
    showInventoryResult(result) {
      this.messages.push({ id: `inventory-${Date.now()}-${Math.random()}`, role: 'assistant', text: '', results: [result] })
      if (result.kind === 'inventory_result' && !this.completedInventoryIds.includes(result.operation_id)) {
        this.completedInventoryIds.push(result.operation_id)
      }
      if (result.outcome === 'succeeded') {
        window.dispatchEvent(new CustomEvent('ktg:inventory-changed', { detail: result }))
      }
      this.scrollToBottom()
    },
    async refreshInventory(operationId) {
      if (this.confirmingInventoryId) return
      const epoch = this.epoch
      this.confirmingInventoryId = operationId
      try {
        const response = await getInventoryOperation(operationId)
        if (epoch === this.epoch) this.showInventoryResult(response.result)
      } catch (_) {
        if (epoch === this.epoch) this.showInventoryResult({ kind: 'inventory_result', operation_id: operationId, outcome: 'unknown', label: '回执暂时无法读取，请稍后查询。' })
      } finally { if (epoch === this.epoch) this.confirmingInventoryId = '' }
    },
    async confirmInventory(operationId) {
      if (!operationId || this.confirmingInventoryId || this.completedInventoryIds.includes(operationId)) return
      const epoch = this.epoch
      this.confirmingInventoryId = operationId
      this.completedInventoryIds.push(operationId)
      try {
        const response = await confirmInventoryOperation(operationId)
        if (epoch === this.epoch) this.showInventoryResult(response.result)
      } catch (_) {
        try {
          const response = await getInventoryOperation(operationId)
          if (epoch === this.epoch) this.showInventoryResult(response.result)
        } catch (_) {
          if (epoch === this.epoch) this.showInventoryResult({ kind: 'inventory_result', operation_id: operationId, outcome: 'unknown', label: '请求结果待核实，请查询同一操作回执。' })
        }
      } finally { if (epoch === this.epoch) this.confirmingInventoryId = '' }
    },
    viewInventory(result) {
      const isReceipt = result.action_type === 'ITEM_RECEIPT_EXECUTE'
      this.$router.push({ path: isReceipt ? '/mes/wm/itemrecpt' : '/mes/wm/issue',
        query: isReceipt ? { recptId: result.document_id } : { issueId: result.document_id }}).catch(() => {})
    },
    async confirmPreview(previewId) {
      if (!previewId || this.confirmingPreviewId) return
      const epoch = this.epoch
      this.confirmingPreviewId = previewId
      try {
        const response = await confirmWorkorderDraft(previewId)
        if (epoch !== this.epoch) return
        const result = response.result
        this.completedPreviewIds.push(previewId)
        this.messages.push({
          id: `preview-${Date.now()}-${Math.random()}`,
          role: 'assistant',
          text: result.outcome === 'succeeded' ? '工单草稿已写入并完成回读核验。' : (result.label || '工单操作未完成。'),
          results: [result]
        })
        if (result.outcome === 'succeeded') {
          window.dispatchEvent(new CustomEvent('ktg:workorder-changed', { detail: result }))
        }
      } catch (error) {
        if (epoch !== this.epoch) return
        const data = error && error.response && error.response.data
        const message = data && data.error && data.error.message
        this.messages.push({
          id: `preview-error-${Date.now()}-${Math.random()}`,
          role: 'assistant',
          text: message || '确认请求失败。系统不会自动重新提交，请先核对工单列表。'
        })
      } finally {
        if (epoch === this.epoch) {
          this.confirmingPreviewId = ''
          this.scrollToBottom()
        }
      }
    },
    selectProduct(candidate, sourceText) {
      const code = candidate && candidate.itemCode
      if (!code || this.busy) return
      this.draft = `${sourceText || '继续生成工单草稿'}\n产品请使用编码 ${code}。`
      this.send()
    },
    viewWorkorder(result) {
      window.dispatchEvent(new CustomEvent('ktg:workorder-changed', { detail: result }))
      this.$router.push({ path: '/mes/pro/workorder', query: { workorderId: result.workorderId }}).catch(() => {})
    },
    scrollToBottom() {
      this.$nextTick(() => {
        const container = this.$refs.messages
        if (container) container.scrollTop = container.scrollHeight
      })
    }
  }
}
</script>

<style lang="scss" scoped>
.agent-launcher,
.agent-panel {
  position: fixed;
  right: 24px;
  bottom: 24px;
  z-index: 1900;
}

.agent-launcher {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 44px;
  padding: 0 16px;
  border: 0;
  border-radius: 22px;
  background: var(--current-color, #409eff);
  color: #fff;
  box-shadow: 0 6px 18px rgba(31, 45, 61, 0.2);
  cursor: pointer;
}

.agent-launcher:focus,
.agent-panel button:focus,
.agent-panel textarea:focus {
  outline: 2px solid #303133;
  outline-offset: 2px;
}

.agent-panel {
  display: flex;
  flex-direction: column;
  width: 390px;
  height: min(620px, calc(100vh - 48px));
  overflow: hidden;
  border: 1px solid #dcdfe6;
  border-radius: 8px;
  background: #f5f7fa;
  box-shadow: 0 12px 36px rgba(31, 45, 61, 0.22);
}

.agent-panel__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 16px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
}

.agent-panel__header h2,
.agent-panel__header p {
  margin: 0;
}

.agent-panel__header h2 { font-size: 16px; color: #303133; }
.agent-panel__header p { margin-top: 4px; font-size: 12px; color: #909399; }
.agent-panel__header button { border: 0; background: transparent; color: #606266; cursor: pointer; font-size: 18px; }

.agent-panel__sessions {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  color: #606266;
  font-size: 12px;
}
.agent-panel__sessions select { flex: 1; min-width: 0; padding: 6px; border: 1px solid #dcdfe6; border-radius: 4px; background: #fff; }
.agent-panel__sessions button,
.agent-message--loading button { border: 0; background: transparent; color: #409eff; cursor: pointer; }

.agent-panel__messages {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
}

.agent-entry { display: flex; flex-direction: column; margin-top: 12px; }
.agent-entry--user { align-items: flex-end; }
.agent-entry--assistant { align-items: stretch; }

.agent-message {
  max-width: 88%;
  padding: 10px 12px;
  border-radius: 6px;
  background: #fff;
  color: #303133;
  font-size: 14px;
  line-height: 1.55;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.agent-entry--user .agent-message { background: var(--current-color, #409eff); color: #fff; }
.agent-message--loading { margin-top: 12px; color: #606266; }
.agent-message--error { margin-top: 12px; color: #c45656; }
.agent-progress { margin-top: 12px; }
.agent-progress__tool { margin-top: 6px; padding: 7px 10px; border-left: 3px solid #a0cfff; background: #fff; color: #606266; font-size: 12px; overflow-wrap: anywhere; }

.agent-example {
  display: block;
  margin-top: 8px;
  padding: 7px 10px;
  border: 1px solid #dcdfe6;
  border-radius: 4px;
  background: #fff;
  color: #409eff;
  cursor: pointer;
}

.agent-panel__composer {
  display: flex;
  gap: 8px;
  padding: 12px;
  border-top: 1px solid #e4e7ed;
  background: #fff;
}

.agent-panel__composer textarea {
  flex: 1;
  resize: none;
  padding: 9px 10px;
  border: 1px solid #dcdfe6;
  border-radius: 4px;
  color: #303133;
  font: inherit;
}

.agent-panel__composer button {
  min-width: 72px;
  border: 0;
  border-radius: 4px;
  background: var(--current-color, #409eff);
  color: #fff;
  cursor: pointer;
}

.agent-panel__composer button:disabled,
.agent-example:disabled { opacity: 0.55; cursor: not-allowed; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }

@media (max-width: 600px) {
  .agent-launcher { right: 16px; bottom: 16px; }
  .agent-panel { inset: 0; width: 100%; height: 100%; border: 0; border-radius: 0; }
}
</style>

