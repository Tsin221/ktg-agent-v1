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

    <section v-else class="agent-panel" :style="panelStyle" role="dialog" aria-label="KTG 智能助手">
      <button class="agent-panel__resize" type="button" aria-label="调整助手窗口大小：方向键调整宽高" title="拖动调整宽高；方向键每次调整20像素" @pointerdown.prevent="startResize" @keydown="resizeKey">⤢</button>
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
        <select id="agent-session-select" :value="sessionId" :disabled="sessionLoading || !sessions.length"
                @change="selectSession($event.target.value)">
          <option v-if="!sessionId" value="" disabled hidden>{{ sessions.length ? '未选中会话' : '暂无会话' }}</option>
          <option v-for="session in sessions" :key="session.session_id" :value="session.session_id">{{ session.title || '未命名会话' }}</option>
        </select>
        <button type="button" :disabled="sessionLoading || titleSaving" @click="editTitle('create')">新会话</button>
        <button type="button" :disabled="sessionLoading || titleSaving || !sessionId" @click="editTitle('rename')">重命名</button>
      </div>

      <form v-if="titleMode" class="agent-panel__title-form" @submit.prevent="saveTitle">
        <div class="agent-title-form__header">
          <label for="agent-session-title">{{ titleMode === 'create' ? '新建会话' : '重命名会话' }}</label>
          <button type="button" class="agent-title-form__close" aria-label="取消" title="取消"
                  :disabled="titleSaving" @click="cancelTitle">
            <i class="el-icon-close" aria-hidden="true"></i>
          </button>
        </div>
        <div class="agent-title-form__fields">
          <input id="agent-session-title" ref="titleInput" v-model="titleDraft" :disabled="titleSaving"
                 :placeholder="titleMode === 'create' ? '会话名称（可选）' : '输入会话名称'"
                 :aria-invalid="titleError ? 'true' : null" :aria-describedby="titleError ? 'agent-session-title-error' : null"
                 maxlength="200" @keydown.esc.prevent="cancelTitle">
          <button type="submit" class="agent-title-form__submit" :disabled="titleSaving">
            {{ titleSaving ? '保存中…' : (titleMode === 'create' ? '创建' : '保存') }}
          </button>
        </div>
        <p v-if="titleError" id="agent-session-title-error" role="alert">{{ titleError }}</p>
      </form>

      <section v-if="titleMode !== 'create' && (tasks.length || taskError)" class="agent-task-card" aria-labelledby="agent-task-heading">
        <div class="agent-task-card__header">
          <h3 id="agent-task-heading">
            <button type="button" class="agent-task-card__toggle" :aria-expanded="taskPanelOpen ? 'true' : 'false'"
                    aria-controls="agent-task-details" @click="taskPanelOpen = !taskPanelOpen">
              <i :class="taskPanelOpen ? 'el-icon-arrow-down' : 'el-icon-arrow-right'" aria-hidden="true" />
              任务清单
              <span v-if="taskError" class="agent-task-card__summary">读取失败</span>
              <span v-else-if="currentTaskProgress" class="agent-task-card__summary">
                {{ currentTaskProgress.completed }} / {{ currentTaskProgress.total }} 步已完成
              </span>
            </button>
          </h3>
          <button v-if="taskId" class="agent-task-card__continue" type="button"
                  :disabled="composerBusy || canInput || !!pendingSubmission" @click="continueTask">
            <i class="el-icon-video-play" aria-hidden="true" />
            继续此任务
          </button>
        </div>
        <div v-show="taskPanelOpen" id="agent-task-details">
          <div v-if="tasks.length" class="agent-task-card__controls">
            <div class="agent-task-card__field">
              <label for="agent-task-select">任务</label>
              <select id="agent-task-select" :value="taskId" @change="selectTask($event.target.value)">
                <option v-for="task in tasks" :key="task.task_id" :value="task.task_id">{{ task.title }}</option>
              </select>
            </div>
            <div v-if="taskDetail" class="agent-task-card__field">
              <label for="agent-task-version">清单版本</label>
              <select id="agent-task-version" v-model.number="taskVersion">
                <option :value="0">第 {{ taskDetail.current_version }} 版 · 当前</option>
                <option v-for="version in taskDetail.versions.slice(1)" :key="version.version" :value="version.version">
                  第 {{ version.version }} 版
                </option>
              </select>
            </div>
          </div>
          <div v-if="taskError" class="agent-task-card__error">
            <p role="alert">{{ taskError }}</p>
            <button type="button" @click="refreshTasks()">重试</button>
          </div>
          <template v-if="taskProgress">
            <div class="agent-task-card__progress">
              <progress :value="taskProgress.completed" :max="taskProgress.total" aria-label="任务步骤完成进度" />
              <span>已完成 {{ taskProgress.completed }} / {{ taskProgress.total }} 步</span>
            </div>
            <ol class="agent-task-card__steps" role="list">
              <li v-for="step in visibleTaskVersion.steps" :key="step.step_id"
                  class="agent-task-card__step" :class="'agent-task-card__step--' + step.status">
                <span class="agent-task-card__marker" aria-hidden="true">
                  <i v-if="step.status === 'completed'" class="el-icon-check" />
                </span>
                <span class="agent-task-card__description">{{ step.description }}</span>
                <span class="agent-task-card__status">{{ taskStatusText[step.status] }}</span>
              </li>
            </ol>
          </template>
        </div>
      </section>

      <div ref="messages" class="agent-panel__messages" aria-live="polite">
        <div class="agent-message agent-message--assistant">
          我可以查询产品、分仓库存、生产工单和设备分类。启用库存操作后，还可预览已准备的物料入库单和生产领料单。
        </div>
        <button
          v-for="example in examples"
          :key="example"
          type="button"
          class="agent-example"
          :disabled="loading"
          @click="submitExample(example)"
        >{{ example }}</button>

        <div v-for="message in displayedMessages" :key="message.id" :class="['agent-entry', `agent-entry--${message.role}`]">
          <div class="agent-message">{{ message.text }}</div>
          <div
            v-for="(result, index) in message.results || []"
            :key="message.resultIds && message.resultIds[index] || `${message.id}-${index}`"
          >
            <div v-if="message.resultPresentation[index]" class="agent-option-summary">
              <button type="button" :aria-expanded="message.resultPresentation[index].open ? 'true' : 'false'" :aria-controls="message.resultPresentation[index].id" @click="toggleOptions(message.resultPresentation[index])">
                {{ message.resultPresentation[index].title }} · {{ message.resultPresentation[index].open ? '收起' : '展开完整内容' }}
              </button>
              <p>{{ message.resultPresentation[index].summary }}<span v-if="message.resultPresentation[index].queriedAt"> · 查询时间 {{ message.resultPresentation[index].queriedAt }}</span></p>
            </div>
            <div v-show="!message.resultPresentation[index] || message.resultPresentation[index].open" :id="message.resultPresentation[index] ? message.resultPresentation[index].id : null">
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
              <agent-dv-machinery-type-card
                v-else-if="isDvMachineryTypeResult(result)"
                :result="result"
                :confirming="confirmingDvPreviewId === result.preview_id"
                :completed="completedDvPreviewIds.includes(result.preview_id)"
                @confirm="confirmDvPreview"
              />
              <agent-dv-machinery-card
                v-else-if="isDvMachineryResult(result)"
                :result="result"
                :confirming="confirmingMachineryPreviewId === result.preview_id"
                :completed="completedDvMachineryPreviewIds.includes(result.preview_id)"
                @confirm="confirmDvMachineryPreview"
              />
              <agent-dv-subject-card
                v-else-if="isDvSubjectResult(result)"
                :result="result"
                :confirming="confirmingSubjectPreviewId === result.preview_id"
                :completed="completedDvSubjectPreviewIds.includes(result.preview_id)"
                @confirm="confirmDvSubjectPreview"
              />
              <agent-tm-tool-type-card
                v-else-if="isTmToolTypeResult(result)"
                :result="result"
                :confirming="confirmingTmToolTypePreviewId === result.preview_id"
                :completed="completedTmToolTypePreviewIds.includes(result.preview_id)"
                @confirm="confirmTmToolTypePreview"
              />
              <agent-tm-tool-card
                v-else-if="isTmToolResult(result)"
                :result="result"
                :confirming="confirmingTmToolPreviewId === result.preview_id"
                :completed="completedTmToolPreviewIds.includes(result.preview_id)"
                @confirm="confirmTmToolPreview"
              />
              <agent-dv-check-plan-card
                v-else-if="isDvCheckPlanResult(result)"
                :result="result"
                :confirming="confirmingCheckPlanPreviewId === result.preview_id"
                :completed="completedDvCheckPlanPreviewIds.includes(result.preview_id)"
                @confirm="confirmDvCheckPlanPreview"
              />
              <agent-dv-repair-card
                v-else-if="isDvRepairResult(result)"
                :result="result"
                :confirming="confirmingRepairPreviewId === result.preview_id"
                :completed="completedDvRepairPreviewIds.includes(result.preview_id)"
                @confirm="confirmDvRepairPreview"
              />
              <agent-result-card v-else :result="result" />
            </div>
          </div>
        </div>
        <div v-if="pendingQuestion" class="agent-entry agent-entry--user">
          <div class="agent-message">{{ pendingQuestion }}</div>
        </div>
        <div v-for="input in displayedInputs" :key="input.request_id" class="agent-entry agent-entry--user">
          <div class="agent-message">{{ input.message }}</div>
          <div class="agent-input-status">
            {{ input.mode === 'steer' ? '调整方向' : '追加问题' }} · {{ inputStatusText(input) }}
            <button v-if="['not_consumed', 'rejected'].includes(input.status)" type="button" :disabled="composerBusy || canInput" @click="resendInput(input)">作为新问题发送</button>
            <button v-if="input.status === 'unconfirmed'" type="button" @click="retryInput">用同一请求重试</button>
          </div>
        </div>
        <div v-if="progress.messages.length || Object.keys(progress.tools).length" class="agent-progress" aria-label="本轮临时进度">
          <div v-for="message in progress.messages" :key="message.id">
            <div v-if="message.text" class="agent-message">{{ message.text }}</div>
            <div v-if="message.text" class="agent-progress__status">{{ message.status === 'incomplete' || streamError || (activeRun && isTerminalRun(activeRun.status) && message.status === 'streaming') ? '部分文本 · 未完成' : '临时文本 · 尚未结算' }}</div>
          </div>
          <div v-for="(tool, id) in progress.tools" :key="id" class="agent-progress__tool">
            {{ tool.name }}：{{ tool.message || (tool.status === 'running' ? '执行中' : tool.status) }}
          </div>
        </div>
        <div v-if="activeRun || loading" class="agent-message agent-message--assistant agent-message--loading" role="status">
          {{ loading ? '正在提交问题…' : runStatusText(activeRun) }}
          <button v-if="streamError && activeRun && !isTerminalRun(activeRun.status)" type="button" @click="followRun">重新连接</button>
          <button v-if="canCancel" type="button" :disabled="cancelLoading" @click="stopRun">
            {{ cancelLoading ? '停止请求中…' : '停止后续执行' }}
          </button>
        </div>
        <div v-if="activeRun" class="agent-run-usage" role="status">
          <span>{{ usageText(activeRun.usage) }}</span>
          <span v-if="activeRun.budget && activeRun.budget.max_total_tokens != null">总 token 停止阈值：{{ activeRun.budget.max_total_tokens }}</span>
          <details v-if="activeRun.usage && activeRun.usage.source !== 'legacy_unknown'" class="agent-run-usage__details">
            <summary>用量详情</summary>
            <div>已知输入 {{ activeRun.usage.known_input_tokens }} · 输出 {{ activeRun.usage.known_output_tokens }} token</div>
            <div>其中缓存输入 {{ activeRun.usage.known_cached_input_tokens }} token（已包含在输入中）</div>
            <div v-if="activeRun.usage.unknown_request_count">{{ activeRun.usage.unknown_request_count }} 次模型调用用量暂缺</div>
          </details>
        </div>
        <div v-if="contextStatusText(contextStatus)" class="agent-context-status" role="status">
          <details>
            <summary>上下文状态</summary>
            <div>{{ contextPressureText(contextStatus) }}</div>
            <div>{{ contextStatusText(contextStatus) }}</div>
          </details>
        </div>
        <p v-if="confirmingPreviewId || confirmingInventoryId" class="agent-business-status" role="status">独立业务确认仍在核验结果；停止聊天运行不会撤销已发出的确认。</p>
        <div v-if="pendingSubmission" class="agent-message agent-message--assistant" role="status">
          问题提交回执未确认。<button type="button" :disabled="loading" @click="retryPendingRun">用同一请求重试</button>
        </div>
        <div v-if="panelError" class="agent-message agent-message--error" role="alert">{{ panelError }}</div>
      </div>

      <form class="agent-panel__composer" @submit.prevent="send">
        <label class="sr-only" for="agent-query-input">输入查询问题或运行中追加内容</label>
        <textarea
          id="agent-query-input"
          ref="input"
          v-model="draft"
          rows="2"
          maxlength="2000"
          placeholder="例如：查询产品 A 的库存"
          :disabled="composerBusy"
          @keydown.enter.exact.prevent="send"
        />
        <select v-if="canInput" v-model="inputMode" aria-label="运行中输入方式">
          <option value="follow_up">追加问题</option>
          <option value="steer">调整方向</option>
        </select>
        <button type="submit" :disabled="composerBusy || !draft.trim()">
          <i class="el-icon-s-promotion" aria-hidden="true" />
          <span>{{ canInput ? '提交' : '发送' }}</span>
        </button>
      </form>
      <p v-if="canInput" class="agent-panel__hint">{{ inputMode === 'steer' ? '下一个可执行的步骤会考虑你的新指示。' : '当前工作准备结束且仍有执行额度时处理。' }}输入不会撤销已执行的操作。</p>
    </section>
  </div>
</template>

<script>
import { agentSession, createAgentSession, renameAgentSession, listAgentSessions, getAgentSession, getAgentSessionStats, listAgentMessages,
  listAgentTasks, getAgentTask, getAgentRun, confirmWorkorderDraft, confirmInventoryOperation, getInventoryOperation,
  confirmDvMachineryType, confirmDvMachinery, confirmDvSubject, confirmDvCheckPlan, confirmDvRepair,
  confirmTmToolType, confirmTmTool } from '@/api/agent'
import AgentResultCard from './ResultCard.vue'
import AgentInventoryCard from './InventoryCard.vue'
import AgentWorkorderDraftCard from './WorkorderDraftCard.vue'
import AgentDvMachineryTypeCard from './DvMachineryTypeCard.vue'
import AgentDvMachineryCard from './DvMachineryCard.vue'
import AgentDvSubjectCard from './DvSubjectCard.vue'
import AgentTmToolTypeCard from './TmToolTypeCard.vue'
import AgentTmToolCard from './TmToolCard.vue'
import AgentDvCheckPlanCard from './DvCheckPlanCard.vue'
import AgentDvRepairCard from './DvRepairCard.vue'
import { isWorkorderAssistantResult } from './workorderPresentation'
import { isTerminalRun, pageContext, presentMessages, optionCardPresentation, progressFromEvent, runStatusText, usageText,
  contextStatusText, contextPressureText,
  visibleInputs, inputStatusText } from './sessionPresentation'

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

export function clampPanelSize(size, viewport) {
  const maxWidth = Math.max(1, viewport.width - 48)
  const maxHeight = Math.max(1, viewport.height - 48)
  return {
    width: Math.round(Math.min(maxWidth, Math.max(Math.min(360, maxWidth), size.width))),
    height: Math.round(Math.min(maxHeight, Math.max(Math.min(420, maxHeight), size.height)))
  }
}

export default {
  name: 'AgentAssistant',
  components: { AgentResultCard, AgentInventoryCard, AgentWorkorderDraftCard, AgentDvMachineryTypeCard, AgentDvMachineryCard, AgentDvSubjectCard, AgentTmToolTypeCard, AgentTmToolCard, AgentDvCheckPlanCard, AgentDvRepairCard },
  data() {
    return {
      open: false,
      panelSize: { width: 390, height: 620 },
      viewport: { width: 1024, height: 768 },
      resizeStart: null,
      titleMode: '',
      titleDraft: '',
      titleError: '',
      titleSaving: false,
      titleRequest: 0,
      loading: false,
      sessionLoading: false,
      draft: '',
      sessions: [],
      sessionId: '',
      tasks: [],
      taskId: '',
      taskDetail: null,
      taskVersion: 0,
      taskError: '',
      taskPanelOpen: false,
      taskRefresh: 0,
      taskDetailRefresh: 0,
      taskStatusText: { pending: '待开始', in_progress: '进行中', completed: '已完成' },
      activeRunId: '',
      activeRun: null,
      sessionStats: null,
      pendingQuestion: '',
      pendingSubmission: null,
      inputMode: 'follow_up',
      runInputs: [],
      pastInputs: [],
      inputAttempt: null,
      cancelRequestId: '',
      cancelLoading: false,
      businessMessages: [],
      progress: { messages: [], tools: {}},
      panelError: '',
      streamError: false,
      epoch: 0,
      unsubscribeStore: null,
      confirmingPreviewId: '',
      confirmingInventoryId: '',
      confirmingDvPreviewId: '',
      completedInventoryIds: [],
      completedPreviewIds: [],
      completedDvPreviewIds: [],
      completedDvMachineryPreviewIds: [],
      confirmingMachineryPreviewId: '',
      confirmingSubjectPreviewId: '',
      completedDvSubjectPreviewIds: [],
      confirmingTmToolTypePreviewId: '',
      completedTmToolTypePreviewIds: [],
      confirmingTmToolPreviewId: '',
      completedTmToolPreviewIds: [],
      confirmingCheckPlanPreviewId: '',
      completedDvCheckPlanPreviewIds: [],
      confirmingRepairPreviewId: '',
      completedDvRepairPreviewIds: [],
      messages: [],
      expandedOptions: {},
      examples: ['查询编码为 A-01 的产品', '查询 A-01 的库存', '为产品 A 创建 100 件、指定日期的工单草稿']
    }
  },
  computed: {
    visibleTaskVersion() {
      if (!this.taskDetail) return null
      return this.taskDetail.versions.find(item =>
        item.version === (this.taskVersion || this.taskDetail.current_version))
    },
    taskProgress() {
      const steps = this.visibleTaskVersion && this.visibleTaskVersion.steps
      if (!steps || !steps.length) return null
      return { completed: steps.filter(step => step.status === 'completed').length, total: steps.length }
    },
    currentTaskProgress() {
      const version = this.taskDetail && this.taskDetail.versions[0]
      const steps = version && version.steps
      if (!steps || !steps.length) return null
      return { completed: steps.filter(step => step.status === 'completed').length, total: steps.length }
    },
    panelStyle() {
      if (this.viewport.width <= 600) return {}
      const size = clampPanelSize(this.panelSize, this.viewport)
      return { width: `${size.width}px`, height: `${size.height}px` }
    },
    displayedMessages() { return optionCardPresentation([...this.messages, ...this.businessMessages], this.expandedOptions) },
    contextStatus() {
      const context = this.sessionStats && this.sessionStats.context
      if (!context || !context.configured) return null
      const runContext = this.activeRun && this.activeRun.context
      return runContext ? { ...context, spill_count: runContext.spill_count } : context
    },
    displayedInputs() {
      const inputs = [...this.pastInputs, ...this.runInputs]
      if (this.inputAttempt && !inputs.some(item => item.request_id === this.inputAttempt.requestId)) {
        inputs.push({ ...this.inputAttempt, request_id: this.inputAttempt.requestId,
          status: this.inputAttempt.status || 'unconfirmed' })
      }
      return visibleInputs(inputs, this.messages, this.activeRunId)
    },
    canInput() { return this.activeRun && this.activeRun.status === 'running' },
    canCancel() { return this.activeRun && ['queued', 'running'].includes(this.activeRun.status) },
    composerBusy() {
      return this.loading || this.sessionLoading || this.cancelLoading ||
        (this.activeRun && ['queued', 'cancelling'].includes(this.activeRun.status))
    },
    busy() {
      return this.loading || this.sessionLoading || (this.activeRun && !isTerminalRun(this.activeRun.status)) ||
        Boolean(this.confirmingPreviewId) || Boolean(this.confirmingInventoryId) ||
        Boolean(this.confirmingDvPreviewId)
        || Boolean(this.confirmingTmToolTypePreviewId)
    }
  },
  mounted() {
    this.updateViewport()
    try {
      const saved = JSON.parse(window.localStorage.getItem('ktg-agent-panel-size'))
      if (saved && Number.isFinite(saved.width) && saved.width > 0 && Number.isFinite(saved.height) && saved.height > 0) {
        this.panelSize = saved
      }
    } catch (_) { /* Storage may be unavailable. */ }
    window.addEventListener('resize', this.updateViewport)
    this.unsubscribeStore = this.$store.subscribe(mutation => {
      if (mutation.type === 'SET_TOKEN' || mutation.type === 'SET_ID') {
        this.resetIdentity()
        this.bootstrap()
      }
    })
    this.bootstrap()
  },
  beforeDestroy() {
    this.finishResize()
    this.cancelTitle()
    window.removeEventListener('resize', this.updateViewport)
    this.epoch++
    agentSession.close()
    if (this.unsubscribeStore) this.unsubscribeStore()
  },
  methods: {
    updateViewport() {
      this.finishResize()
      this.viewport = { width: window.innerWidth, height: window.innerHeight }
    },
    persistSize() {
      try { window.localStorage.setItem('ktg-agent-panel-size', JSON.stringify(this.panelSize)) } catch (_) { /* Optional preference. */ }
    },
    startResize(event) {
      if (event.button !== 0 || this.viewport.width <= 600) return
      this.finishResize()
      this.resizeStart = { x: event.clientX, y: event.clientY, ...clampPanelSize(this.panelSize, this.viewport) }
      window.addEventListener('pointermove', this.moveResize)
      window.addEventListener('pointerup', this.finishResize)
      window.addEventListener('pointercancel', this.finishResize)
      window.addEventListener('blur', this.finishResize)
    },
    moveResize(event) {
      if (!this.resizeStart) return
      const start = this.resizeStart
      this.panelSize = clampPanelSize({ width: start.width + start.x - event.clientX,
        height: start.height + start.y - event.clientY }, this.viewport)
    },
    finishResize() {
      if (this.resizeStart) this.persistSize()
      this.resizeStart = null
      window.removeEventListener('pointermove', this.moveResize)
      window.removeEventListener('pointerup', this.finishResize)
      window.removeEventListener('pointercancel', this.finishResize)
      window.removeEventListener('blur', this.finishResize)
    },
    resizeKey(event) {
      const changes = { ArrowLeft: [20, 0], ArrowRight: [-20, 0], ArrowUp: [0, 20], ArrowDown: [0, -20] }
      if (!changes[event.key]) return
      event.preventDefault()
      const size = clampPanelSize(this.panelSize, this.viewport)
      const delta = changes[event.key]
      this.panelSize = clampPanelSize({ width: size.width + delta[0], height: size.height + delta[1] }, this.viewport)
      this.persistSize()
    },
    editTitle(mode) {
      this.cancelTitle()
      this.titleMode = mode
      const session = this.sessions.find(item => item.session_id === this.sessionId)
      this.titleDraft = mode === 'rename' && session ? session.title || '' : ''
      this.$nextTick(() => this.$refs.titleInput && this.$refs.titleInput.focus())
    },
    cancelTitle() {
      this.titleRequest++
      this.titleMode = ''
      this.titleDraft = ''
      this.titleError = ''
      this.titleSaving = false
    },
    async saveTitle() {
      if (this.titleSaving || !this.titleMode) return
      const title = this.titleDraft.trim()
      const mode = this.titleMode
      if ((mode === 'rename' && !title) || [...title].length > 200) {
        this.titleError = '会话名称须为1至200个字符。'
        return
      }
      const request = ++this.titleRequest
      const epoch = this.epoch
      const sessionId = this.sessionId
      this.titleSaving = true
      this.titleError = ''
      try {
        if (mode === 'create') {
          await this.newSession(title || undefined)
          if (request === this.titleRequest) this.cancelTitle()
          return
        }
        const session = await renameAgentSession(sessionId, title)
        if (request !== this.titleRequest || epoch !== this.epoch || sessionId !== this.sessionId) return
        this.sessions = this.sessions.map(item => item.session_id === sessionId ? { ...item, ...session } : item)
        this.cancelTitle()
      } catch (error) {
        if (request === this.titleRequest && (mode === 'create' || epoch === this.epoch)) this.titleError = this.errorText(error, '会话名称保存失败，请重试。')
      } finally {
        if (request === this.titleRequest) this.titleSaving = false
      }
    },
    toggleOptions(card) {
      this.$set(this.expandedOptions, card.id, !card.open)
      this.saveLocalState()
    },
    isTerminalRun, inputStatusText,
    runStatusText, usageText, contextStatusText, contextPressureText,
    localKey() {
      return `ktg-agent-view:${this.$store.state.user.id}:${this.sessionId}`
    },
    saveLocalState() {
      if (!this.sessionId || !window.sessionStorage) return
      window.sessionStorage.setItem(this.localKey(), JSON.stringify({
        expandedOptions: this.expandedOptions,
        businessMessages: this.businessMessages,
        completedPreviewIds: this.completedPreviewIds,
        completedInventoryIds: this.completedInventoryIds,
        completedDvPreviewIds: this.completedDvPreviewIds,
        completedDvMachineryPreviewIds: this.completedDvMachineryPreviewIds,
        completedDvSubjectPreviewIds: this.completedDvSubjectPreviewIds,
        completedTmToolTypePreviewIds: this.completedTmToolTypePreviewIds,
        completedDvCheckPlanPreviewIds: this.completedDvCheckPlanPreviewIds,
        completedDvRepairPreviewIds: this.completedDvRepairPreviewIds,
        pastInputs: this.pastInputs,
        pendingSubmission: this.pendingSubmission,
        inputAttempt: this.inputAttempt,
        cancelRequestId: this.cancelRequestId
      }))
    },
    restoreLocalState() {
      if (!window.sessionStorage) return
      const saved = window.sessionStorage.getItem(this.localKey())
      if (!saved) return
      const state = JSON.parse(saved)
      this.expandedOptions = state.expandedOptions || {}
      this.businessMessages = state.businessMessages || []
      this.completedPreviewIds = state.completedPreviewIds || []
      this.completedInventoryIds = state.completedInventoryIds || []
      this.completedDvPreviewIds = state.completedDvPreviewIds || []
      this.completedDvMachineryPreviewIds = state.completedDvMachineryPreviewIds || []
      this.completedDvSubjectPreviewIds = state.completedDvSubjectPreviewIds || []
      this.completedTmToolTypePreviewIds = state.completedTmToolTypePreviewIds || []
      this.completedDvCheckPlanPreviewIds = state.completedDvCheckPlanPreviewIds || []
      this.completedDvRepairPreviewIds = state.completedDvRepairPreviewIds || []
      this.pastInputs = state.pastInputs || []
      this.pendingSubmission = state.pendingSubmission || null
      this.inputAttempt = state.inputAttempt || null
      this.cancelRequestId = state.cancelRequestId || ''
      if (this.pendingSubmission) this.draft = this.pendingSubmission.message
      if (this.inputAttempt) this.draft = this.inputAttempt.message
    },
    resetIdentity() {
      this.cancelTitle()
      this.expandedOptions = {}
      this.sessionStats = null
      this.epoch++
      agentSession.checkIdentity()
      agentSession.close()
      this.sessions = []
      this.sessionId = ''
      this.resetTasks()
      this.activeRunId = ''
      this.activeRun = null
      this.pendingQuestion = ''
      this.pendingSubmission = null
      this.runInputs = []
      this.pastInputs = []
      this.inputAttempt = null
      this.cancelRequestId = ''
      this.cancelLoading = false
      this.businessMessages = []
      this.progress = { messages: [], tools: {}}
      this.messages = []
      this.draft = ''
      this.panelError = ''
      this.streamError = false
      this.loading = false
      this.sessionLoading = false
      this.completedInventoryIds = []
      this.completedPreviewIds = []
      this.completedDvPreviewIds = []
      this.completedDvMachineryPreviewIds = []
      this.completedDvSubjectPreviewIds = []
      this.completedTmToolTypePreviewIds = []
      this.completedDvCheckPlanPreviewIds = []
      this.completedDvRepairPreviewIds = []
      this.confirmingInventoryId = ''
      this.confirmingPreviewId = ''
      this.confirmingDvPreviewId = ''
      this.confirmingMachineryPreviewId = ''
      this.confirmingSubjectPreviewId = ''
      this.confirmingTmToolTypePreviewId = ''
      this.confirmingCheckPlanPreviewId = ''
      this.confirmingRepairPreviewId = ''
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
      if (sessionId !== this.sessionId) this.resetTasks()
      this.cancelTitle()
      this.expandedOptions = {}
      agentSession.close()
      this.sessionLoading = true
      this.panelError = ''
      this.streamError = false
      this.pendingQuestion = ''
      this.pendingSubmission = null
      this.runInputs = []
      this.pastInputs = []
      this.inputAttempt = null
      this.cancelRequestId = ''
      this.cancelLoading = false
      this.businessMessages = []
      this.progress = { messages: [], tools: {}}
      this.activeRun = null
      this.sessionStats = null
      this.activeRunId = ''
      this.messages = []
      try {
        const session = await getAgentSession(sessionId)
        if (epoch !== this.epoch) return
        if (!this.sessions.some(item => item.session_id === sessionId)) this.sessions.unshift(session)
        else this.sessions = this.sessions.map(item => item.session_id === sessionId ? { ...item, ...session } : item)
        if (!agentSession.getState() || agentSession.getState().sessionId !== sessionId) agentSession.setSession(sessionId)
        this.sessionId = sessionId
        this.refreshTasks(epoch)
        this.restoreLocalState()
        await this.refreshStats(epoch)
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
          this.runInputs = await agentSession.listInputs(runId) || []
          if (epoch !== this.epoch) return
          this.reconcileInputAttempt(runId, this.runInputs)
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
    async refreshStats(epoch = this.epoch) {
      const sessionId = this.sessionId
      if (!sessionId) return
      try {
        const stats = await getAgentSessionStats(sessionId)
        if (epoch === this.epoch && sessionId === this.sessionId) this.sessionStats = stats
      } catch (error) {
        if (epoch === this.epoch && sessionId === this.sessionId) this.sessionStats = null
      }
    },
    resetTasks() {
      this.tasks = []
      this.taskId = ''
      this.taskDetail = null
      this.taskVersion = 0
      this.taskError = ''
      this.taskPanelOpen = false
    },
    async refreshTasks(epoch = this.epoch) {
      const sessionId = this.sessionId
      if (!sessionId) return
      const refresh = ++this.taskRefresh
      try {
        const { items } = await listAgentTasks(sessionId)
        if (epoch !== this.epoch || refresh !== this.taskRefresh) return
        this.tasks = items
        this.taskError = ''
        const task = items.find(item => item.task_id === this.taskId) || items[0]
        if (task) await this.selectTask(task.task_id)
        else this.resetTasks()
      } catch (error) {
        if (epoch === this.epoch && refresh === this.taskRefresh) {
          this.taskError = this.errorText(error, '任务列表读取失败。')
        }
      }
    },
    async selectTask(taskId) {
      const epoch = this.epoch
      const refresh = ++this.taskDetailRefresh
      if (this.taskId !== taskId) {
        this.taskDetail = null
        this.taskVersion = 0
      }
      this.taskId = taskId
      try {
        const detail = await getAgentTask(this.sessionId, taskId)
        if (epoch !== this.epoch || refresh !== this.taskDetailRefresh) return
        this.taskDetail = detail
        this.taskError = ''
      } catch (error) {
        if (epoch === this.epoch && refresh === this.taskDetailRefresh) {
          this.taskError = this.errorText(error, '任务清单读取失败。')
        }
      }
    },
    continueTask() {
      if (!this.taskId || this.canInput || this.composerBusy || this.pendingSubmission) return
      if (!this.draft.trim()) this.draft = '继续此任务'
      return this.send(this.taskId)
    },
    selectSession(sessionId) {
      if (sessionId && sessionId !== this.sessionId) this.loadSession(sessionId)
    },
    async newSession(title) {
      const epoch = ++this.epoch
      agentSession.close()
      this.sessionLoading = true
      try {
        const session = await createAgentSession(title)
        if (epoch !== this.epoch) return
        this.sessions.unshift(session)
        agentSession.setSession(session.session_id)
        await this.loadSession(session.session_id, epoch)
        this.$nextTick(() => this.$refs.input && this.$refs.input.focus())
      } catch (error) {
        if (epoch === this.epoch) this.panelError = this.errorText(error, '新建会话失败。')
        throw error
      } finally {
        if (epoch === this.epoch) this.sessionLoading = false
      }
    },
    async followRun(epoch = this.epoch) {
      if (!this.activeRunId) return
      this.streamError = false
      try {
        await agentSession.subscribeRun({
          onConnectionError: () => {
            if (epoch !== this.epoch) return
            this.streamError = true
            this.panelError = '进度连接中断，正在自动重连；本轮不会重新提交。'
          },
          onConnectionRestored: () => {
            if (epoch !== this.epoch) return
            this.streamError = false
            this.panelError = ''
            this.refreshStats(epoch)
          },
          onEvent: event => {
            if (epoch !== this.epoch || event.run_id !== this.activeRunId) return
            if (['input_queued', 'input_consumed', 'input_not_consumed'].includes(event.type)) this.refreshInputs(epoch)
            if (event.type === 'agent_start' && this.activeRun.status === 'queued') {
              this.activeRun = { ...this.activeRun, status: 'running' }
            }
            if (event.type === 'tool_execution_end' && event.payload.status === 'succeeded' &&
                ['create_task', 'update_task'].includes(event.payload.tool_name)) {
              const taskId = event.payload.result && event.payload.result.task_id
              if (event.payload.tool_name === 'create_task' && taskId) this.selectTask(taskId)
              this.refreshTasks(epoch)
            }
            if (event.type === 'cancel_requested') this.activeRun = { ...this.activeRun, status: 'cancelling' }
            if (['model_request_started', 'model_usage_reported', 'model_request_settled'].includes(event.type)) {
              const runId = this.activeRunId
              getAgentRun(runId).then(run => {
                if (epoch === this.epoch && runId === this.activeRunId &&
                    (!this.activeRun.last_seq || run.last_seq >= this.activeRun.last_seq)) this.activeRun = run
              }).catch(error => {
                if (epoch === this.epoch) this.panelError = this.errorText(error, '用量状态读取失败。')
              })
            }
            if (event.type !== 'run_settled') this.progress = progressFromEvent(this.progress, event)
          },
          onSnapshot: snapshot => {
            if (epoch !== this.epoch || snapshot.run.run_id !== this.activeRunId) return
            this.activeRun = snapshot.run
            this.refreshStats(epoch)
            this.messages = presentMessages(snapshot.messages.items)
            this.runInputs = snapshot.inputs
            this.reconcileInputAttempt(snapshot.run.run_id, this.runInputs)
            if (isTerminalRun(snapshot.run.status)) {
              this.refreshTasks(epoch)
              this.pendingQuestion = ''
              if (snapshot.run.status === 'completed' || snapshot.run.status === 'needs_input') {
                this.progress = { messages: [], tools: {}}
              } else {
                const savedTexts = new Set(this.messages.filter(message => message.role === 'assistant').map(message => message.text))
                this.progress = { ...this.progress, messages: this.progress.messages
                  .filter(message => message.text && !savedTexts.has(message.text))
                  .map(message => ({ ...message, status: 'incomplete' })) }
              }
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
      if (data && data.error && data.error.message) return data.error.message
      if (!error || !error.response || error.response.status >= 500) return fallback
      return error.message || fallback
    },
    openPanel() {
      this.open = true
      this.$nextTick(() => this.$refs.input && this.$refs.input.focus())
    },
    closePanel() {
      this.finishResize()
      this.open = false
    },
    submitExample(example) {
      this.draft = example
      this.send()
    },
    reconcileInputAttempt(runId, inputs) {
      const attempt = this.inputAttempt
      if (!attempt || attempt.runId !== runId ||
          !inputs.some(item => item.request_id === attempt.requestId)) return
      this.inputAttempt = null
      if (this.draft === attempt.message) this.draft = ''
      this.saveLocalState()
    },
    async refreshInputs(epoch = this.epoch) {
      const runId = this.activeRunId
      if (!runId) return
      try {
        const inputs = await agentSession.listInputs(runId)
        if (epoch === this.epoch && runId === this.activeRunId && inputs) {
          this.runInputs = inputs
          this.reconcileInputAttempt(runId, inputs)
        }
      } catch (error) {
        if (epoch === this.epoch) this.panelError = this.errorText(error, '输入状态读取失败，请稍后重试。')
      }
    },
    async stopRun() {
      if (!this.canCancel || this.cancelLoading) return
      const epoch = this.epoch
      const runId = this.activeRunId
      this.cancelRequestId = this.cancelRequestId || requestId()
      this.saveLocalState()
      this.cancelLoading = true
      this.panelError = ''
      try {
        await agentSession.cancelRun(this.cancelRequestId)
        if (epoch !== this.epoch || runId !== this.activeRunId) return
        const run = await getAgentRun(runId)
        if (epoch === this.epoch && runId === this.activeRunId) this.activeRun = run
      } catch (error) {
        if (epoch === this.epoch) {
          this.panelError = this.errorText(error, '停止请求回执未确认；可用同一请求再次点击。')
          try {
            const run = await getAgentRun(runId)
            if (epoch === this.epoch && runId === this.activeRunId) this.activeRun = run
          } catch (_) { /* Keep the last known server state. */ }
        }
      } finally {
        if (epoch === this.epoch) this.cancelLoading = false
      }
    },
    async retryInput() {
      if (!this.inputAttempt || ['rejected', 'conflict'].includes(this.inputAttempt.status)) return
      await this.sendInput(true)
    },
    async retryPendingRun() {
      if (!this.pendingSubmission || this.loading) return
      const epoch = this.epoch
      const submission = this.pendingSubmission
      this.loading = true
      try {
        const accepted = await agentSession.createRun(submission)
        if (epoch !== this.epoch || !accepted) return
        this.pendingSubmission = null
        this.saveLocalState()
        this.activeRunId = accepted.run_id
        const run = await getAgentRun(accepted.run_id)
        if (epoch === this.epoch) {
          this.activeRun = run
          this.draft = ''
          if (isTerminalRun(this.activeRun.status)) await this.loadSession(this.sessionId, epoch)
          else this.followRun(epoch)
        }
      } catch (error) {
        if (epoch === this.epoch) this.panelError = this.errorText(error, '提交状态未确认；请继续使用同一请求重试。')
      } finally {
        if (epoch === this.epoch) this.loading = false
      }
    },
    async sendInput(retry = false) {
      const text = retry ? this.inputAttempt.message : this.draft.trim()
      if (!text || (!this.canInput && !retry) || this.loading || this.sessionLoading) return
      if (this.inputAttempt && !retry &&
          (this.inputAttempt.message !== text || this.inputAttempt.mode !== this.inputMode)) {
        this.panelError = this.inputAttempt.status === 'rejected'
          ? '上一条输入已被本轮拒绝，请查看运行状态后决定是否作为新问题发送。'
          : '上一条输入的回执未确认，请先按原内容重试。'
        return
      }
      if (this.inputAttempt && ['rejected', 'conflict'].includes(this.inputAttempt.status)) {
        this.panelError = this.inputAttempt.status === 'rejected'
          ? '上一条输入已被本轮拒绝，请主动选择作为新问题发送。'
          : '请求 ID 与已提交内容冲突，请查看输入状态。'
        return
      }
      const epoch = this.epoch
      const runId = this.activeRunId
      const attempt = retry ? this.inputAttempt : this.inputAttempt && this.inputAttempt.message === text &&
        this.inputAttempt.mode === this.inputMode && this.inputAttempt.runId === runId
        ? this.inputAttempt : { runId, requestId: requestId(), mode: this.inputMode, message: text }
      this.inputAttempt = attempt
      this.saveLocalState()
      this.loading = true
      this.panelError = ''
      try {
        const result = await agentSession.addInput(attempt)
        if (epoch !== this.epoch || runId !== this.activeRunId || !result) return
        this.runInputs = [...this.runInputs.filter(item => item.request_id !== attempt.requestId),
          { request_id: attempt.requestId, mode: attempt.mode, message: text, status: result.input_status }]
        this.inputAttempt = null
        if (this.draft === attempt.message) this.draft = ''
        this.saveLocalState()
        await this.refreshInputs(epoch)
      } catch (error) {
        if (epoch !== this.epoch) return
        const code = error && error.response && error.response.data && error.response.data.error && error.response.data.error.code
        if (error.response && error.response.status === 409 && ['RUN_NOT_RUNNING', 'RUN_NOT_ACTIVE'].includes(code)) {
          this.inputAttempt = { ...attempt, status: 'rejected', reason: code }
          this.saveLocalState()
          this.panelError = '本轮当前不接收输入，内容已保留。请查看运行状态后决定是否作为新问题发送。'
        } else if (code === 'REQUEST_ID_CONFLICT') {
          this.inputAttempt = { ...attempt, status: 'conflict', reason: code }
          this.saveLocalState()
          this.panelError = this.errorText(error, '请求 ID 与已提交内容冲突，请查看输入状态。')
        } else {
          await this.refreshInputs(epoch)
          if (this.inputAttempt) this.panelError = this.errorText(error, '输入回执未确认；再次提交相同内容会沿用同一请求 ID。')
        }
        try {
          const run = await getAgentRun(runId)
          if (epoch === this.epoch && runId === this.activeRunId) this.activeRun = run
        } catch (_) { /* The original error remains visible. */ }
      } finally {
        if (epoch === this.epoch) this.loading = false
      }
    },
    resendInput(input) {
      if (!['not_consumed', 'rejected'].includes(input.status) || !this.sessionId || this.composerBusy || this.canInput) return
      if (input.status === 'rejected' && this.inputAttempt && this.inputAttempt.requestId === input.request_id) {
        this.pastInputs = [...this.pastInputs, { request_id: input.request_id, runId: input.runId,
          mode: input.mode, message: input.message, status: 'rejected', reason: input.reason }]
        this.inputAttempt = null
      }
      this.draft = input.message
      this.pendingSubmission = null
      this.saveLocalState()
      this.send()
    },
    async send(taskId) {
      const text = this.draft.trim()
      if (this.pendingSubmission) return this.retryPendingRun()
      if (this.canInput) return this.sendInput()
      if (!text || this.composerBusy) return
      if (this.inputAttempt) {
        this.panelError = this.inputAttempt.status === 'rejected'
          ? '上一条输入已被本轮拒绝，请主动选择作为新问题发送。'
          : '上一条输入的回执未确认，请先用同一请求重试。'
        return
      }
      if (this.activeRun && isTerminalRun(this.activeRun.status)) {
        const old = this.runInputs.filter(item => item.status === 'not_consumed')
        this.pastInputs = [...this.pastInputs, ...old.filter(item =>
          !this.pastInputs.some(saved => saved.request_id === item.request_id))]
        this.saveLocalState()
      }
      this.runInputs = []
      this.draft = ''
      this.loading = true
      this.activeRun = null
      this.activeRunId = ''
      this.progress = { messages: [], tools: {}}
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
          : { sessionId: this.sessionId, requestId: requestId(), message: text, pageContext: pageContext(this.$route),
            ...(typeof taskId === 'string' ? { taskId } : {}) }
        this.pendingSubmission = submission
        this.saveLocalState()
        const run = await agentSession.createRun(submission)
        if (epoch !== this.epoch || !run) return
        this.pendingSubmission = null
        this.saveLocalState()
        this.activeRunId = run.run_id
        this.activeRun = { run_id: run.run_id, status: run.status }
        this.followRun(epoch)
      } catch (error) {
        if (epoch === this.epoch) {
          this.panelError = this.errorText(error, '提交状态未确认；再次发送相同问题会沿用同一请求。')
          this.draft = text
          this.pendingQuestion = ''
          const code = error && error.response && error.response.data && error.response.data.error && error.response.data.error.code
          if (code === 'TASK_SELECTION_REQUIRED') {
            this.pendingSubmission = null
            this.saveLocalState()
            this.panelError = '此会话有多个未完成任务。请在任务列表中选择任务，然后点击「继续此任务」。'
            this.refreshTasks(epoch)
          }
          if (code === 'RUN_BUSY') {
            this.pendingSubmission = null
            this.saveLocalState()
            try {
              const session = await getAgentSession(this.sessionId)
              if (epoch === this.epoch && session.active_run_id) {
                agentSession.setRun(session.active_run_id)
                this.activeRunId = session.active_run_id
                const run = await getAgentRun(session.active_run_id)
                if (epoch === this.epoch) {
                  this.activeRun = run
                  if (!isTerminalRun(run.status)) this.followRun(epoch)
                }
              }
            } catch (_) { /* Keep the original conflict visible. */ }
          }
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
    isDvMachineryTypeResult(result) {
      return result && ['dv_machinerytype_tree', 'dv_machinerytype_detail',
        'dv_machinerytype_preview', 'dv_machinerytype_create_result',
        'dv_machinerytype_update_result', 'dv_machinerytype_result'].includes(result.kind)
    },
    isDvMachineryResult(result) {
      return result && ['dv_machinery_list', 'dv_machinery_detail', 'md_machinery_workshop_options',
        'dv_machinery_preview', 'dv_machinery_create_result', 'dv_machinery_update_result',
        'dv_machinery_delete_result', 'dv_machinery_result'].includes(result.kind)
    },
    isDvCheckPlanResult(result) {
      return result && ['dv_checkplan_list', 'dv_checkplan_detail', 'dv_checkplan_options',
        'dv_checkplan_links', 'dv_checkplan_machinery_candidates', 'dv_checkplan_subject_candidates',
        'dv_checkplan_preview', 'dv_checkplan_result'].includes(result.kind)
    },
    isDvRepairResult(result) {
      return result && ['dv_repair_list', 'dv_repair_detail', 'dv_repair_lines', 'dv_repair_options',
        'dv_repair_machinery_candidates', 'dv_repair_subject_candidates', 'dv_repair_preview', 'dv_repair_result'].includes(result.kind)
    },
    isDvSubjectResult(result) {
      return result && ['dv_subject_list', 'dv_subject_detail',
        'dv_subject_preview', 'dv_subject_create_result', 'dv_subject_update_result',
        'dv_subject_delete_result', 'dv_subject_result'].includes(result.kind)
    },
    isTmToolTypeResult(result) {
      return result && ['tm_tooltype_list', 'tm_tooltype_detail', 'tm_tooltype_options', 'tm_tooltype_preview',
        'tm_tooltype_create_result', 'tm_tooltype_update_result', 'tm_tooltype_delete_result', 'tm_tooltype_result'].includes(result.kind)
    },
    isTmToolResult(result) {
      return result && ['tm_tool_list','tm_tool_detail','tm_tool_options','tm_tool_preview',
        'tm_tool_create_result','tm_tool_update_result','tm_tool_delete_result','tm_tool_result'].includes(result.kind)
    },
    showInventoryResult(result) {
      const id = `confirm-inventory-${result.operation_id}`
      const entry = { id, role: 'assistant', text: '', results: [result] }
      const index = this.businessMessages.findIndex(item => item.id === id)
      if (index >= 0) this.businessMessages.splice(index, 1, entry)
      else this.businessMessages.push(entry)
      this.saveLocalState()
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
      this.showInventoryResult({ kind: 'inventory_result', operation_id: operationId, outcome: 'unknown',
        label: '确认请求已发出，仍在核验结果。请勿再次提交。' })
      this.saveLocalState()
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
    async confirmDvPreview(previewId) {
      if (!previewId || this.confirmingDvPreviewId || this.completedDvPreviewIds.includes(previewId)) return
      const epoch = this.epoch
      this.confirmingDvPreviewId = previewId
      this.completedDvPreviewIds.push(previewId)
      const entryId = `confirm-dv-${previewId}`
      this.businessMessages.push({ id: entryId, role: 'assistant', text: '设备分类确认请求已发出，仍在核验结果。请勿再次提交。' })
      this.saveLocalState()
      try {
        const response = await confirmDvMachineryType(previewId)
        if (epoch !== this.epoch) return
        const result = response.result
        this.businessMessages.splice(this.businessMessages.findIndex(item => item.id === entryId), 1, {
          id: entryId,
          role: 'assistant',
          text: result.outcome === 'succeeded' ? '设备分类已写入并完成回读核验。' : (result.label || '设备分类操作未完成。'),
          results: [result]
        })
        this.saveLocalState()
        if (result.outcome === 'succeeded') {
          window.dispatchEvent(new CustomEvent('ktg:dv-machinerytype-changed', { detail: result }))
        }
      } catch (error) {
        if (epoch !== this.epoch) return
        const data = error && error.response && error.response.data
        const message = data && data.error && data.error.message
        this.businessMessages.splice(this.businessMessages.findIndex(item => item.id === entryId), 1, {
          id: entryId,
          role: 'assistant',
          text: message || '确认请求失败。系统不会自动重新提交，请先核对设备类型列表。'
        })
        this.saveLocalState()
      } finally {
        if (epoch === this.epoch) {
          this.confirmingDvPreviewId = ''
          this.scrollToBottom()
        }
      }
    },
    async confirmDvMachineryPreview(previewId) {
      if (!previewId || this.confirmingMachineryPreviewId || this.completedDvMachineryPreviewIds.includes(previewId)) return
      const epoch = this.epoch
      this.confirmingMachineryPreviewId = previewId
      this.completedDvMachineryPreviewIds.push(previewId)
      const entryId = `confirm-machinery-${previewId}`
      this.businessMessages.push({ id: entryId, role: 'assistant', text: '设备台账确认请求已发出，仍在核验结果。请勿再次提交。' })
      this.saveLocalState()
      try {
        const response = await confirmDvMachinery(previewId)
        if (epoch !== this.epoch) return
        const result = response.result
        const index = this.businessMessages.findIndex(item => item.id === entryId)
        if (index >= 0) {
          this.businessMessages.splice(index, 1, {
            id: entryId,
            role: 'assistant',
            text: result.outcome === 'succeeded' ? '设备台账已写入并完成回读核验。' : (result.label || '设备台账操作未完成。'),
            results: [result]
          })
        }
        this.saveLocalState()
        if (result.outcome === 'succeeded') {
          window.dispatchEvent(new CustomEvent('ktg:dv-machinery-changed', { detail: result }))
        }
      } catch (error) {
        if (epoch !== this.epoch) return
        const data = error && error.response && error.response.data
        const message = data && data.error && data.error.message
        const index = this.businessMessages.findIndex(item => item.id === entryId)
        if (index >= 0) {
          this.businessMessages.splice(index, 1, {
            id: entryId, role: 'assistant',
            text: message || '确认请求失败。系统不会自动重新提交，请先核对设备台账。'
          })
        }
        this.saveLocalState()
      } finally {
        if (epoch === this.epoch) {
          this.confirmingMachineryPreviewId = ''
          this.scrollToBottom()
        }
      }
    },
    async confirmDvCheckPlanPreview(previewId) {
      if (!previewId || this.confirmingCheckPlanPreviewId || this.completedDvCheckPlanPreviewIds.includes(previewId)) return
      const epoch = this.epoch
      this.confirmingCheckPlanPreviewId = previewId
      this.completedDvCheckPlanPreviewIds.push(previewId)
      const entryId = `confirm-checkplan-${previewId}`
      this.businessMessages.push({ id: entryId, role: 'assistant', text: '计划确认请求已发出，仍在核验结果。请勿再次提交。' })
      this.saveLocalState()
      try {
        const response = await confirmDvCheckPlan(previewId)
        if (epoch !== this.epoch) return
        const result = response && response.result
        const verified = result && result.outcome === 'succeeded' && result.verified === true
        const index = this.businessMessages.findIndex(item => item.id === entryId)
        if (index >= 0) this.businessMessages.splice(index, 1, {
          id: entryId, role: 'assistant',
          text: verified ? '点检保养计划操作已完成回读核验。' : (result && result.outcome === 'unknown' ? '结果尚未确认，请先核对。系统不会自动重新提交。' : (result && result.outcome === 'succeeded' && result.verified !== true ? '结果未核实，请先核对MES中的计划。' : result && result.label || '计划操作未能确认成功，请重新预览后再试。')),
          results: result ? [result] : []
        })
        this.saveLocalState()
        if (verified) window.dispatchEvent(new CustomEvent('ktg:dv-checkplan-changed', {
          detail: { result, plan_id: result.plan_id || result.plan && result.plan.planId || null, action: result.action }
        }))
      } catch (error) {
        if (epoch !== this.epoch) return
        const data = error && error.response && error.response.data
        const message = data && data.error && data.error.message
        const index = this.businessMessages.findIndex(item => item.id === entryId)
        if (index >= 0) this.businessMessages.splice(index, 1, { id: entryId, role: 'assistant',
          text: message || '确认结果尚未确认，请先核对计划。系统不会自动重新提交。' })
        this.saveLocalState()
      } finally {
        if (epoch === this.epoch) { this.confirmingCheckPlanPreviewId = ''; this.scrollToBottom() }
      }
    },
    async confirmDvRepairPreview(previewId) {
      if (!previewId || this.confirmingRepairPreviewId || this.completedDvRepairPreviewIds.includes(previewId)) return
      const epoch = this.epoch
      this.confirmingRepairPreviewId = previewId
      this.completedDvRepairPreviewIds.push(previewId)
      const entryId = `confirm-repair-${previewId}`
      this.businessMessages.push({ id: entryId, role: 'assistant', text: '维修确认请求已发出，仍在核验结果。请勿再次提交。' })
      this.saveLocalState()
      try {
        const response = await confirmDvRepair(previewId)
        if (epoch !== this.epoch) return
        const result = response && response.result
        const verified = result && result.outcome === 'succeeded' && result.verified === true
        const index = this.businessMessages.findIndex(item => item.id === entryId)
        if (index >= 0) this.businessMessages.splice(index, 1, {
          id: entryId, role: 'assistant',
          text: verified ? '维修操作已完成回读核验。' : (result && result.outcome === 'unknown' ?
            '结果未知，请先核对MES中的维修单。系统不会自动重新提交。' :
            result && result.outcome === 'succeeded' ? '结果未核实，请先核对MES中的维修单。' :
            result && result.label || '操作未能确认成功，请重新预览后再试。'),
          results: result ? [result] : []
        })
        this.saveLocalState()
        if (verified) window.dispatchEvent(new CustomEvent('ktg:dv-repair-changed', { detail: result }))
      } catch (error) {
        if (epoch !== this.epoch) return
        const data = error && error.response && error.response.data
        const message = data && data.error && data.error.message
        const index = this.businessMessages.findIndex(item => item.id === entryId)
        if (index >= 0) this.businessMessages.splice(index, 1, { id: entryId, role: 'assistant',
          text: message || '确认结果未知，请先核对维修单。系统不会自动重新提交。' })
        this.saveLocalState()
      } finally {
        if (epoch === this.epoch) { this.confirmingRepairPreviewId = ''; this.scrollToBottom() }
      }
    },
    async confirmDvSubjectPreview(previewId) {
      if (!previewId || this.confirmingSubjectPreviewId || this.completedDvSubjectPreviewIds.includes(previewId)) return
      const epoch = this.epoch
      this.confirmingSubjectPreviewId = previewId
      this.completedDvSubjectPreviewIds.push(previewId)
      const entryId = `confirm-subject-${previewId}`
      this.businessMessages.push({ id: entryId, role: 'assistant', text: '点检保养项目确认请求已发出，仍在核验结果。请勿再次提交。' })
      this.saveLocalState()
      try {
        const response = await confirmDvSubject(previewId)
        if (epoch !== this.epoch) return
        const result = response.result
        const index = this.businessMessages.findIndex(item => item.id === entryId)
        if (index >= 0) {
          this.businessMessages.splice(index, 1, {
            id: entryId,
            role: 'assistant',
            text: result.outcome === 'succeeded' ? '点检保养项目已写入并完成回读核验。' : (result.label || '点检保养项目操作未完成。'),
            results: [result]
          })
        }
        this.saveLocalState()
        if (result.outcome === 'succeeded') {
          window.dispatchEvent(new CustomEvent('ktg:dv-subject-changed', { detail: result }))
        }
      } catch (error) {
        if (epoch !== this.epoch) return
        const data = error && error.response && error.response.data
        const message = data && data.error && data.error.message
        const index = this.businessMessages.findIndex(item => item.id === entryId)
        if (index >= 0) {
          this.businessMessages.splice(index, 1, {
            id: entryId, role: 'assistant',
            text: message || '确认请求失败。系统不会自动重新提交，请先核对点检保养项目。'
          })
        }
        this.saveLocalState()
      } finally {
        if (epoch === this.epoch) {
          this.confirmingSubjectPreviewId = ''
          this.scrollToBottom()
        }
      }
    },
    async confirmTmToolPreview(previewId) {
      if (!previewId || this.confirmingTmToolPreviewId || this.completedTmToolPreviewIds.includes(previewId)) return
      const epoch = this.epoch
      this.confirmingTmToolPreviewId = previewId
      this.completedTmToolPreviewIds.push(previewId)
      const entryId = `confirm-tm-tool-${previewId}`
      this.businessMessages.push({ id: entryId, role: 'assistant', text: '工装夹具台账确认请求已发出，仍在核验结果。请勿再次提交。' })
      this.saveLocalState()
      try {
        const response = await confirmTmTool(previewId)
        if (epoch !== this.epoch) return
        const result = response && response.result
        const verified = result && result.outcome === 'succeeded' && result.verified === true
        const index = this.businessMessages.findIndex(item => item.id === entryId)
        if (index >= 0) this.businessMessages.splice(index, 1, {
          id: entryId, role: 'assistant',
          text: verified ? '工装夹具台账操作已完成回读核验。' :
            result && result.outcome === 'unknown' ? '操作结果未知，请先核对MES；系统不会自动重新提交。' :
            result && result.label || '工装夹具台账操作未能确认成功，请重新预览。',
          results: result ? [result] : []
        })
        this.saveLocalState()
        if (verified) {
          const item = result[({ create: 'verified_record', update: 'verified_record', delete: 'deleted' })[result.action]] || {}
          window.dispatchEvent(new CustomEvent('ktg:tm-tool-changed', {
            detail: { result, action: result.action, tool_id: item.toolId || null }
          }))
        }
      } catch (error) {
        if (epoch !== this.epoch) return
        const data = error && error.response && error.response.data
        const message = data && data.error && data.error.message
        const index = this.businessMessages.findIndex(item => item.id === entryId)
        if (index >= 0) this.businessMessages.splice(index, 1, { id: entryId, role: 'assistant',
          text: message || '确认结果尚未核实。系统不会自动重新提交，请先核对工装夹具。' })
        this.saveLocalState()
      } finally {
        if (epoch === this.epoch) { this.confirmingTmToolPreviewId = ''; this.scrollToBottom() }
      }
    },
    async confirmTmToolTypePreview(previewId) {
      if (!previewId || this.confirmingTmToolTypePreviewId || this.completedTmToolTypePreviewIds.includes(previewId)) return
      const epoch = this.epoch
      this.confirmingTmToolTypePreviewId = previewId
      this.completedTmToolTypePreviewIds.push(previewId)
      const entryId = `confirm-tm-tooltype-${previewId}`
      this.businessMessages.push({ id: entryId, role: 'assistant', text: '工装夹具类型确认请求已发出，仍在核验结果。请勿再次提交。' })
      this.saveLocalState()
      try {
        const response = await confirmTmToolType(previewId)
        if (epoch !== this.epoch) return
        const result = response && response.result
        const verified = result && result.outcome === 'succeeded' && result.verified === true
        const index = this.businessMessages.findIndex(item => item.id === entryId)
        if (index >= 0) this.businessMessages.splice(index, 1, {
          id: entryId, role: 'assistant',
          text: verified ? '工装夹具类型操作已完成回读核验。' :
            result && result.outcome === 'unknown' ? '操作结果未知，请先核对MES；系统不会自动重新提交。' :
            result && result.label || '工装夹具类型操作未能确认成功，请重新预览。',
          results: result ? [result] : []
        })
        this.saveLocalState()
        if (verified) {
          const item = result[({ create: 'created', update: 'updated', delete: 'deleted' })[result.action]] || {}
          window.dispatchEvent(new CustomEvent('ktg:tm-tooltype-changed', {
            detail: { result, action: result.action, tool_type_id: item.toolTypeId || null }
          }))
        }
      } catch (error) {
        if (epoch !== this.epoch) return
        const data = error && error.response && error.response.data
        const message = data && data.error && data.error.message
        const index = this.businessMessages.findIndex(item => item.id === entryId)
        if (index >= 0) this.businessMessages.splice(index, 1, { id: entryId, role: 'assistant',
          text: message || '确认结果尚未核实。系统不会自动重新提交，请先核对工装夹具类型。' })
        this.saveLocalState()
      } finally {
        if (epoch === this.epoch) { this.confirmingTmToolTypePreviewId = ''; this.scrollToBottom() }
      }
    },
    async confirmPreview(previewId) {
      if (!previewId || this.confirmingPreviewId || this.completedPreviewIds.includes(previewId)) return
      const epoch = this.epoch
      this.confirmingPreviewId = previewId
      this.completedPreviewIds.push(previewId)
      const entryId = `confirm-preview-${previewId}`
      this.businessMessages.push({ id: entryId, role: 'assistant', text: '工单确认请求已发出，仍在核验结果。请勿再次提交。' })
      this.saveLocalState()
      try {
        const response = await confirmWorkorderDraft(previewId)
        if (epoch !== this.epoch) return
        const result = response.result
        this.businessMessages.splice(this.businessMessages.findIndex(item => item.id === entryId), 1, {
          id: entryId,
          role: 'assistant',
          text: result.outcome === 'succeeded' ? '工单草稿已写入并完成回读核验。' : (result.label || '工单操作未完成。'),
          results: [result]
        })
        this.saveLocalState()
        if (result.outcome === 'succeeded') {
          window.dispatchEvent(new CustomEvent('ktg:workorder-changed', { detail: result }))
        }
      } catch (error) {
        if (epoch !== this.epoch) return
        const data = error && error.response && error.response.data
        const message = data && data.error && data.error.message
        this.businessMessages.splice(this.businessMessages.findIndex(item => item.id === entryId), 1, {
          id: entryId,
          role: 'assistant',
          text: message || '确认请求失败。系统不会自动重新提交，请先核对工单列表。'
        })
        this.saveLocalState()
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
  box-sizing: border-box;
  width: 390px;
  height: min(620px, calc(100vh - 48px));
  overflow: hidden;
  border: 1px solid #dcdfe6;
  border-radius: 8px;
  background: #f5f7fa;
  box-shadow: 0 12px 36px rgba(31, 45, 61, 0.22);
}

.agent-panel__resize { position: absolute; top: 0; left: 0; width: 24px; height: 24px; border: 0; background: transparent; color: #606266; cursor: nwse-resize; touch-action: none; }
.agent-panel__title-form {
  flex-shrink: 0;
  display: grid;
  gap: 8px;
  margin: 10px 12px 0;
  padding: 10px 12px 12px;
  border: 1px solid #dfe6ef;
  border-radius: 10px;
  background: #fff;
}
.agent-title-form__header,
.agent-title-form__fields {
  display: flex;
  align-items: center;
  gap: 8px;
}
.agent-title-form__header { justify-content: space-between; }
.agent-title-form__header label { color: #303946; font-size: 13px; font-weight: 600; }
.agent-title-form__close {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  padding: 0;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: #606b7a;
  cursor: pointer;
}
.agent-title-form__close:hover:not(:disabled) { background: #edf2f8; }
.agent-title-form__fields input {
  box-sizing: border-box;
  flex: 1 1 0;
  min-width: 0;
  height: 36px;
  padding: 0 10px;
  border: 1px solid #d5deea;
  border-radius: 6px;
  background: #f8fafc;
  color: #303946;
  font: inherit;
  font-size: 13px;
}
.agent-title-form__fields input::placeholder { color: #657388; }
.agent-title-form__fields input:focus {
  border-color: #337ecc;
  outline: 2px solid #dcecff;
  outline-offset: 1px;
  background: #fff;
}
.agent-title-form__fields input[aria-invalid="true"] { border-color: #bc4545; }
.agent-title-form__submit {
  flex-shrink: 0;
  min-width: 62px;
  height: 36px;
  padding: 0 12px;
  border: 0;
  border-radius: 6px;
  background: #2368ad;
  color: #fff;
  font: inherit;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
}
.agent-title-form__submit:hover:not(:disabled) { background: #195792; }
.agent-title-form__submit:disabled,
.agent-title-form__close:disabled { opacity: 0.6; cursor: not-allowed; }
.agent-panel__title-form p { margin: 0; color: #a22d2d; font-size: 12px; }
.agent-panel__header {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 16px 14px 26px;
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
  flex-shrink: 0;
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
  min-height: 0;
  flex: 1;
  overflow-y: auto;
  padding: 16px;
}

.agent-entry { display: flex; flex-direction: column; margin-top: 12px; }
.agent-entry--user { align-items: flex-end; }
.agent-entry--assistant { align-items: stretch; }
.agent-option-summary { margin-top: 10px; padding: 10px 12px; border: 1px solid #dfe4ed; border-radius: 6px; background: #fff; color: #303133; }
.agent-option-summary button { padding: 0; border: 0; background: transparent; color: var(--current-color, #409eff); cursor: pointer; text-align: left; min-height: 32px; }
.agent-option-summary button:focus-visible { outline: 2px solid var(--current-color, #409eff); outline-offset: 2px; }
.agent-option-summary p { margin: 4px 0 0; font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }

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
.agent-input-status { margin-top: 4px; color: #606266; font-size: 12px; text-align: right; }
.agent-input-status button { border: 0; background: transparent; color: #337ecc; cursor: pointer; }
.agent-business-status { margin: 8px 0; color: #606266; font-size: 12px; }
.agent-panel__hint { margin: 0; padding: 0 12px 8px; background: #fff; color: #606266; font-size: 12px; }
.agent-progress { margin-top: 12px; }
.agent-progress__status { margin: 3px 0 8px; color: #606266; font-size: 12px; }
.agent-progress__tool { margin-top: 6px; padding: 7px 10px; border-left: 3px solid #a0cfff; background: #fff; color: #606266; font-size: 12px; overflow-wrap: anywhere; }
.agent-run-usage { display: flex; flex-wrap: wrap; gap: 4px 12px; margin-top: 8px; color: #606266; font-size: 12px; }
.agent-run-usage__details { width: 100%; }
.agent-run-usage__details summary { color: #337ecc; cursor: pointer; }
.agent-context-status { margin-top: 8px; color: #606266; font-size: 12px; }
.agent-context-status summary { color: #337ecc; cursor: pointer; }

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
  flex-shrink: 0;
  display: flex;
  gap: 8px;
  padding: 12px;
  border-top: 1px solid #e4e7ed;
  background: #fff;
}

.agent-panel__composer textarea {
  min-width: 0;
  flex: 1;
  resize: none;
  padding: 9px 10px;
  border: 1px solid #dcdfe6;
  border-radius: 4px;
  color: #303133;
  font: inherit;
}
.agent-panel__composer select { max-width: 98px; border: 1px solid #dcdfe6; border-radius: 4px; background: #fff; }

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
  .agent-panel__resize { display: none; }
  .agent-panel { inset: 0; width: 100%; height: 100%; border: 0; border-radius: 0; }
}
.agent-task-card {
  flex: 0 0 auto;
  margin: 8px 12px 0;
  padding: 5px 8px;
  border: 1px solid #dfe4ed;
  border-radius: 6px;
  background: #fff;
}

.agent-task-card__header,
.agent-task-card__progress,
.agent-task-card__error {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.agent-task-card__header h3 {
  min-width: 0;
  margin: 0;
  color: #303133;
  font-size: 14px;
  font-weight: 600;
}

.agent-task-card button {
  flex-shrink: 0;
  min-height: 30px;
  padding: 0 8px;
  border: 1px solid #c6e2ff;
  border-radius: 4px;
  background: #ecf5ff;
  color: #175a9c;
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}

.agent-task-card button:hover:not(:disabled) {
  border-color: #8cc5ff;
  background: #d9ecff;
}

.agent-task-card button:disabled {
  border-color: #dfe4ed;
  background: #f5f7fa;
  color: #606266;
  cursor: not-allowed;
}

.agent-task-card button.agent-task-card__toggle {
  display: flex;
  align-items: center;
  gap: 5px;
  min-height: 32px;
  padding: 0 4px;
  border-color: transparent;
  background: transparent;
  color: #303133;
  white-space: nowrap;
}
.agent-task-card button.agent-task-card__toggle:hover {
  background: #f0f5fb;
}
.agent-task-card__summary { color: #606266; font-weight: 400; }
.agent-task-card #agent-task-details { padding: 0 4px 5px; }

.agent-task-card__continue i { margin-right: 2px; }

.agent-task-card__controls {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
  margin-top: 8px;
}

.agent-task-card__field {
  display: grid;
  min-width: 0;
  gap: 4px;
}

.agent-task-card__field label,
.agent-task-card__progress span {
  color: #606266;
  font-size: 12px;
}

.agent-task-card__field select {
  width: 100%;
  min-width: 0;
  height: 32px;
  padding: 0 6px;
  border: 1px solid #dcdfe6;
  border-radius: 4px;
  background: #fff;
  color: #303133;
  font: inherit;
  font-size: 12px;
  text-overflow: ellipsis;
}

.agent-task-card__field select:focus-visible {
  outline: 2px solid #337ecc;
  outline-offset: 1px;
}

.agent-task-card__progress { margin: 10px 0 6px; }
.agent-task-card__progress span { flex-shrink: 0; font-variant-numeric: tabular-nums; }
.agent-task-card__progress progress {
  flex: 1;
  min-width: 0;
  height: 5px;
  appearance: none;
  border: 0;
  border-radius: 4px;
  overflow: hidden;
  background: #e8edf5;
  accent-color: #337ecc;
}
.agent-task-card__progress progress::-webkit-progress-bar { background: #e8edf5; }
.agent-task-card__progress progress::-webkit-progress-value { background: #337ecc; }
.agent-task-card__progress progress::-moz-progress-bar { background: #337ecc; }

.agent-task-card__steps {
  max-height: clamp(60px, 22vh, 140px);
  margin: 0;
  padding: 0;
  overflow: auto;
  list-style: none;
}

.agent-task-card__step {
  position: relative;
  display: grid;
  grid-template-columns: 16px minmax(0, 1fr) auto;
  align-items: start;
  gap: 8px;
  padding: 5px 4px;
  border-radius: 4px;
  color: #303133;
  font-size: 12px;
  line-height: 1.5;
}

.agent-task-card__step:not(:last-child)::before {
  position: absolute;
  top: 20px;
  bottom: -7px;
  left: 11px;
  width: 2px;
  background: #dfe4ed;
  content: '';
}

.agent-task-card__marker {
  z-index: 1;
  display: grid;
  width: 16px;
  height: 16px;
  margin-top: 1px;
  place-items: center;
  border: 1px solid #a8abb2;
  border-radius: 50%;
  background: #fff;
  font-size: 10px;
}

.agent-task-card__description { overflow-wrap: anywhere; }
.agent-task-card__status {
  padding: 0 5px;
  border-radius: 4px;
  background: #f0f2f5;
  color: #606266;
  font-size: 11px;
  line-height: 18px;
  white-space: nowrap;
}

.agent-task-card__step--completed .agent-task-card__marker {
  border-color: #218052;
  background: #218052;
  color: #fff;
}
.agent-task-card__step--completed .agent-task-card__status { background: #eaf6ef; color: #1c6944; }
.agent-task-card__step--in_progress { background: #f0f7ff; }
.agent-task-card__step--in_progress .agent-task-card__marker {
  border: 4px solid #337ecc;
  background: #fff;
}
.agent-task-card__step--in_progress .agent-task-card__status { background: #dcecff; color: #175a9c; }

.agent-task-card__error {
  margin-top: 8px;
  padding: 4px 8px;
  border-radius: 4px;
  background: #fef0f0;
  color: #a83434;
  font-size: 12px;
}
.agent-task-card__error p { margin: 0; overflow-wrap: anywhere; }
.agent-task-card__error button { border-color: transparent; background: transparent; color: inherit; }
</style>

