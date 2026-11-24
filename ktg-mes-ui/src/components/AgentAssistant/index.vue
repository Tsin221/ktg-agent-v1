<template>
  <div class="agent-assistant">
    <button
      v-if="!open"
      ref="launcher"
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
      <div class="agent-panel__top">
        <header class="agent-panel__header">
          <div class="agent-panel__identity">
            <h2>KTG 智能助手</h2>
            <p :title="sessionTitle">{{ sessionTitle }}</p>
          </div>
          <div class="agent-panel__actions">
            <button
              ref="sessionToggle"
              type="button"
              class="agent-panel__session-toggle"
              :aria-expanded="sessionsOpen ? 'true' : 'false'"
              aria-controls="agent-session-controls"
              @click="sessionsOpen = !sessionsOpen"
            >
              会话 <i :class="sessionsOpen ? 'el-icon-arrow-up' : 'el-icon-arrow-down'" aria-hidden="true" />
            </button>
            <button class="agent-panel__minimize" type="button" aria-label="收起智能查询助手" @click="closePanel">
              <i class="el-icon-minus" aria-hidden="true" />
            </button>
          </div>
        </header>

        <div v-show="sessionsOpen" id="agent-session-controls" class="agent-panel__session-drawer">
          <div class="agent-panel__sessions">
            <label for="agent-session-select">切换</label>
            <select
              id="agent-session-select"
              :value="sessionId"
              :disabled="sessionLoading || !sessions.length"
              @change="selectSession($event.target.value)"
            >
              <option v-if="!sessionId" value="" disabled hidden>{{ sessions.length ? '未选中会话' : '暂无会话' }}</option>
              <option v-for="session in sessions" :key="session.session_id" :value="session.session_id">{{ session.title || '未命名会话' }}</option>
            </select>
            <button type="button" :disabled="sessionLoading || titleSaving" @click="editTitle('create')">新会话</button>
            <button type="button" :disabled="sessionLoading || titleSaving || !sessionId" @click="editTitle('rename')">重命名</button>
          </div>

          <form v-if="titleMode" class="agent-panel__title-form" @submit.prevent="saveTitle">
            <div class="agent-title-form__header">
              <label for="agent-session-title">{{ titleMode === 'create' ? '新建会话' : '重命名会话' }}</label>
              <button
                type="button"
                class="agent-title-form__close"
                aria-label="取消"
                title="取消"
                :disabled="titleSaving"
                @click="cancelTitle"
              >
                <i class="el-icon-close" aria-hidden="true" />
              </button>
            </div>
            <div class="agent-title-form__fields">
              <input
                id="agent-session-title"
                ref="titleInput"
                v-model="titleDraft"
                :disabled="titleSaving"
                :placeholder="titleMode === 'create' ? '会话名称（可选）' : '输入会话名称'"
                :aria-invalid="titleError ? 'true' : null"
                :aria-describedby="titleError ? 'agent-session-title-error' : null"
                maxlength="200"
                @keydown.esc.prevent="cancelTitle"
              >
              <button type="submit" class="agent-title-form__submit" :disabled="titleSaving">
                {{ titleSaving ? '保存中…' : (titleMode === 'create' ? '创建' : '保存') }}
              </button>
            </div>
            <p v-if="titleError" id="agent-session-title-error" role="alert">{{ titleError }}</p>
          </form>
        </div>
      </div>

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
        <p v-if="currentTaskStep" class="agent-task-card__current" role="status">
          进行中 · {{ currentTaskStep.description }}
        </p>
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
        <section v-if="showWelcome" class="agent-welcome" aria-label="开始对话">
          <h3>今天想查询什么？</h3>
          <p>目前可查询产品总数、分仓现存量、工单与生产进度、工单用料和缺口，以及质量异常和检验记录。</p>
          <div class="agent-welcome__examples">
            <button
              v-for="example in examples"
              :key="example"
              type="button"
              class="agent-example"
              :disabled="composerBusy"
              @click="submitExample(example)"
            >{{ example }}</button>
          </div>
        </section>

        <div v-for="message in displayedMessages" :key="message.id" :class="['agent-entry', `agent-entry--${message.role}`]">
          <div v-if="message.text && message.role === 'user'" class="agent-message">{{ message.text }}</div>
          <agent-answer-content v-else-if="message.text" class="agent-message" :text="message.textPreview || message.text" />
          <agent-answer-content v-if="message.followUp" class="agent-message agent-message--follow-up" :text="message.followUp" />
          <agent-answer-content v-if="message.textTables" class="agent-message" :text="message.textTables" />
          <div
            v-for="(result, index) in message.results || []"
            :key="message.resultIds && message.resultIds[index] || `${message.id}-${index}`"
          >
            <agent-result-card v-if="result" class="agent-result-surface" :result="result" />
          </div>
          <details v-if="message.hasFullText" class="agent-query-details">
            <summary>展开依据与明细</summary>
            <agent-answer-content class="agent-message" :text="message.textDetails" />
          </details>
          <p v-if="message.queryError" class="agent-message agent-message--error" role="alert">查询失败 · {{ message.queryError }}</p>
          <details
            v-if="message.queryDetails"
            class="agent-query-details"
            :open="openQueryDetails[message.id] || null"
            @toggle="$set(openQueryDetails, message.id, $event.target.open)"
          >
            <summary>
              查看查询明细 · {{ message.queryDetails.count }} 次查询<span v-if="message.queryDetails.failed" class="agent-query-error"> · {{ message.queryDetails.failed }} 次失败</span>
            </summary>
            <div v-if="openQueryDetails[message.id]" class="agent-query-details__body">
              <section v-for="group in message.queryDetails.groups" :key="group.id" class="agent-query-group">
                <h4>{{ group.title }}</h4>
                <div v-for="query in group.queries" :key="query.id" class="agent-query-record">
                  <component :is="query.attempts.length > 1 ? 'details' : 'div'">
                    <summary v-if="query.attempts.length > 1">{{ query.title }} · 重复查询 {{ query.attempts.length }} 次</summary>
                    <div v-for="attempt in query.attempts" :key="attempt.id" class="agent-query-attempt">
                      <p class="agent-query-attempt__meta">
                        <span v-if="attempt.time">查询时间 {{ attempt.time }}</span>
                        <span v-if="attempt.scope">{{ attempt.scope }}</span>
                      </p>
                      <p v-if="attempt.error" class="agent-query-error">{{ query.title }}失败 · {{ attempt.error }}</p>
                      <agent-result-card v-else-if="attempt.result" class="agent-result-surface" :result="attempt.result" />
                      <p v-else-if="attempt.resultShown" class="agent-query-attempt__meta">结果已在上方展示。</p>
                    </div>
                  </component>
                </div>
              </section>
            </div>
          </details>
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
        <div v-if="liveMessage" class="agent-progress" aria-label="本轮临时进度">
          <div v-if="liveMessage">
            <div class="agent-message">{{ liveMessage.text.length > 120 ? liveMessage.text.slice(0, 120) + '…' : liveMessage.text }}</div>
            <div class="agent-progress__status">{{ liveMessage.status === 'incomplete' || streamError || (activeRun && isTerminalRun(activeRun.status) && liveMessage.status === 'streaming') ? '部分文本 · 未完成' : '临时文本 · 尚未结算' }}</div>
          </div>
        </div>
      </div>

      <div
        v-if="activeRun || loading || contextStatusText(contextStatus) || pendingSubmission || panelError"
        class="agent-panel__status"
      >
        <div v-if="activeRun || loading" class="agent-message agent-message--assistant agent-message--loading" role="status">
          {{ loading ? '正在提交问题…' : toolActivity || runStatusText(activeRun) }}
          <button v-if="streamError && activeRun && !isTerminalRun(activeRun.status)" type="button" @click="followRun">重新连接</button>
          <button v-if="canCancel" type="button" :disabled="cancelLoading" @click="stopRun">
            {{ cancelLoading ? '停止请求中…' : '停止后续执行' }}
          </button>
        </div>
        <details v-if="activeRun || contextStatusText(contextStatus)" class="agent-run-details">
          <summary>运行详情</summary>
          <div v-if="activeRun" class="agent-run-usage">
            <strong>用量</strong>
            <div>{{ usageText(activeRun.usage) }}</div>
            <div v-if="activeRun.budget && activeRun.budget.max_total_tokens != null">总 token 停止阈值：{{ activeRun.budget.max_total_tokens }}</div>
            <template v-if="activeRun.usage && activeRun.usage.source !== 'legacy_unknown'">
              <div>已知输入 {{ activeRun.usage.known_input_tokens }} · 输出 {{ activeRun.usage.known_output_tokens }} token</div>
              <div>其中缓存输入 {{ activeRun.usage.known_cached_input_tokens }} token（已包含在输入中）</div>
              <div v-if="activeRun.usage.unknown_request_count">{{ activeRun.usage.unknown_request_count }} 次模型调用用量暂缺</div>
            </template>
          </div>
          <div v-if="contextStatusText(contextStatus)" class="agent-context-status">
            <strong>上下文状态</strong>
            <div>{{ contextPressureText(contextStatus) }}</div>
            <div>{{ contextStatusText(contextStatus) }}</div>
          </div>
        </details>
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
          placeholder="例如：冬季新品120件的用料够吗？"
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
    </section>
  </div>
</template>

<script>
import { agentSession, createAgentSession, renameAgentSession, listAgentSessions, getAgentSession, getAgentSessionStats, listAgentMessages,
  listAgentTasks, getAgentTask, getAgentRun } from '@/api/agent'
import AgentResultCard from './ResultCard.vue'
import AgentAnswerContent from './AnswerContent.vue'
import { isTerminalRun, pageContext, presentMessages, compactResultPresentation, progressFromEvent, runStatusText, usageText,
  contextStatusText, contextPressureText, queryCardPresentation, toolProgressText,
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
  components: { AgentResultCard, AgentAnswerContent },
  data() {
    return {
      open: false,
      sessionsOpen: false,
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
      messages: [],
      openQueryDetails: {},
      examples: ['有哪些工单尚未完工？', '床头阅读灯为什么还没完工？', '冬季新品120件的用料够吗？', '查询 CELL-18650-2200 的分仓库存']
    }
  },
  computed: {
    sessionTitle() {
      const current = this.sessions.find(session => session.session_id === this.sessionId)
      return this.sessionId ? current && current.title || '未命名会话' : '暂无会话'
    },
    showWelcome() {
      return !this.sessionLoading && !this.loading && !this.activeRun && !this.liveMessage &&
        !this.pendingQuestion && !this.pendingSubmission && !this.displayedMessages.length && !this.displayedInputs.length
    },
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
    currentTaskStep() {
      const version = this.taskDetail && this.taskDetail.versions[0]
      if (!this.canInput || !version || version.run_id !== this.activeRunId) return null
      return version.steps.find(step => step.status === 'in_progress')
    },
    panelStyle() {
      if (this.viewport.width <= 600) return {}
      const size = clampPanelSize(this.panelSize, this.viewport)
      return { width: `${size.width}px`, height: `${size.height}px` }
    },
    displayedMessages() {
      return compactResultPresentation(queryCardPresentation([...this.messages, ...this.businessMessages]))
    },
    liveMessage() { return this.progress.messages.slice().reverse().find(message => message.text && !message.queryPreface) },
    toolActivity() { return this.canInput ? toolProgressText(this.progress.tools) : '' },
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
      return this.loading || this.sessionLoading || (this.activeRun && !isTerminalRun(this.activeRun.status))
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
    isTerminalRun, inputStatusText,
    runStatusText, usageText, contextStatusText, contextPressureText,
    localKey() {
      return `ktg-agent-view:${this.$store.state.user.id}:${this.sessionId}`
    },
    saveLocalState() {
      if (!this.sessionId || !window.sessionStorage) return
      window.sessionStorage.setItem(this.localKey(), JSON.stringify({
        businessMessages: this.businessMessages,
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
      this.businessMessages = state.businessMessages || []
      this.pastInputs = state.pastInputs || []
      this.pendingSubmission = state.pendingSubmission || null
      this.inputAttempt = state.inputAttempt || null
      this.cancelRequestId = state.cancelRequestId || ''
      if (this.pendingSubmission) this.draft = this.pendingSubmission.message
      if (this.inputAttempt) this.draft = this.inputAttempt.message
    },
    resetIdentity() {
      this.cancelTitle()
      this.openQueryDetails = {}
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
      this.openQueryDetails = {}
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
        if (this.activeRun && !isTerminalRun(this.activeRun.status)) this.scrollToBottom()
        else this.scrollToLatestAnswer()
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
      if (sessionId && sessionId !== this.sessionId) {
        this.sessionsOpen = false
        this.$refs.sessionToggle && this.$refs.sessionToggle.focus()
        this.loadSession(sessionId)
      }
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
        if (epoch !== this.epoch) return
        this.sessionsOpen = false
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
            if (isTerminalRun(snapshot.run.status)) this.scrollToLatestAnswer()
            else this.scrollToBottom()
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
      if (this.activeRun && !isTerminalRun(this.activeRun.status)) this.scrollToBottom()
      else this.scrollToLatestAnswer()
      this.$nextTick(() => this.$refs.input && this.$refs.input.focus())
    },
    closePanel() {
      this.finishResize()
      this.open = false
      this.sessionsOpen = false
      this.$nextTick(() => this.$refs.launcher && this.$refs.launcher.focus())
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
          if (this.activeRun && isTerminalRun(this.activeRun.status)) this.scrollToLatestAnswer()
          else this.scrollToBottom()
          if (this.open && !this.busy) this.$nextTick(() => this.$refs.input && this.$refs.input.focus())
        }
      }
    },
    scrollToBottom() {
      this.$nextTick(() => {
        const container = this.$refs.messages
        if (container) container.scrollTop = container.scrollHeight
      })
    },
    scrollToLatestAnswer() {
      this.$nextTick(() => {
        const container = this.$refs.messages
        if (!container) return
        const entries = container.querySelectorAll('.agent-entry--assistant')
        const latest = Array.from(entries).reverse().find(entry => entry.querySelector('.agent-answer-content'))
        if (!latest) { container.scrollTop = container.scrollHeight; return }
        container.scrollTop += latest.getBoundingClientRect().top - container.getBoundingClientRect().top - 8
      })
    }
  }
}
</script>

<style lang="scss" scoped>
.agent-assistant {
  --assistant-accent: var(--current-color, #1890ff);
  --assistant-action: #135da6;
  --assistant-text: #202b3a;
  --assistant-muted: #536275;
  --assistant-line: #e2e8f0;
  --assistant-surface: #f5f7fa;
}
@supports (color: color-mix(in srgb, blue, black)) {
  .agent-assistant { --assistant-action: color-mix(in srgb, var(--assistant-accent), #142235 44%); }
}

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
  background: var(--assistant-action);
  color: #fff;
  box-shadow: 0 6px 18px rgba(31, 45, 61, 0.2);
  cursor: pointer;
}

.agent-launcher:focus-visible,
.agent-panel :is(button, select, textarea, summary):focus-visible {
  outline: 2px solid var(--assistant-action);
  outline-offset: 2px;
}

.agent-panel {
  display: flex;
  flex-direction: column;
  box-sizing: border-box;
  width: 390px;
  height: min(620px, calc(100vh - 48px));
  overflow: hidden;
  border: 1px solid var(--assistant-line);
  border-radius: 16px;
  background: #fff;
  color: var(--assistant-text);
  box-shadow: 0 16px 48px rgba(24, 39, 60, 0.16), 0 2px 8px rgba(24, 39, 60, 0.05);
}

.agent-panel__resize { position: absolute; z-index: 2; top: 3px; left: 3px; width: 24px; height: 24px; border: 0; background: transparent; color: #606266; cursor: nwse-resize; touch-action: none; }
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
  gap: 12px;
  padding: 14px 14px 14px 29px;
  border-bottom: 1px solid var(--assistant-line);
}
.agent-panel__top { position: relative; z-index: 1; flex-shrink: 0; }
.agent-panel__session-drawer {
  position: absolute;
  top: 100%;
  right: 0;
  left: 0;
  max-height: min(48vh, 240px);
  overflow: auto;
  overscroll-behavior: contain;
  padding-bottom: 4px;
  border: solid var(--assistant-line);
  border-width: 0 0 1px;
  background: #fff;
  box-shadow: 0 10px 18px rgba(24, 39, 60, 0.1);
}
.agent-panel__session-drawer .agent-panel__title-form { margin-bottom: 8px; }
.agent-panel__identity { min-width: 0; }
.agent-panel__actions { display: flex; flex-shrink: 0; align-items: center; gap: 5px; }
.agent-panel__header h2 { margin: 0; font-size: 16px; font-weight: 700; line-height: 1.35; }
.agent-panel__header p {
  margin: 3px 0 0;
  overflow: hidden;
  font-size: 12px;
  line-height: 1.35;
  color: var(--assistant-muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.agent-panel__header button {
  min-height: 36px;
  padding: 0 10px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--assistant-muted);
  font: inherit;
  font-size: 13px;
  white-space: nowrap;
  cursor: pointer;
}
.agent-panel__header button:hover,
.agent-panel__header button[aria-expanded="true"] { background: var(--assistant-surface); color: var(--assistant-action); }
.agent-panel__minimize { min-width: 36px; }

.agent-panel__sessions {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  border-bottom: 1px solid var(--assistant-line);
  background: var(--assistant-surface);
  color: var(--assistant-muted);
  font-size: 12px;
}
.agent-panel__sessions select {
  flex: 1;
  min-width: 0;
  min-height: 34px;
  padding: 4px 6px;
  border: 1px solid var(--assistant-line);
  border-radius: 8px;
  background: #fff;
  color: var(--assistant-text);
  font: inherit;
}
.agent-panel__sessions button,
.agent-panel__status button {
  min-height: 32px;
  padding: 0 7px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--assistant-action);
  font: inherit;
  white-space: nowrap;
  cursor: pointer;
}
.agent-panel__sessions button:hover:not(:disabled),
.agent-panel__status button:hover:not(:disabled) { background: #eaf2fc; }
.agent-panel__sessions button:disabled,
.agent-panel__status button:disabled { opacity: 0.55; cursor: not-allowed; }

.agent-panel__messages {
  box-sizing: border-box;
  min-height: 48px;
  flex: 1;
  overflow: auto;
  overscroll-behavior: contain;
  padding: 16px clamp(14px, 4vw, 24px);
}
.agent-entry { display: flex; min-width: 0; flex-direction: column; }
.agent-entry + .agent-entry { margin-top: 18px; }
.agent-entry--user { align-items: flex-end; }
.agent-entry--assistant { align-items: stretch; }

.agent-panel .agent-result-surface {
  box-sizing: border-box;
  max-width: 100%;
  padding: 12px 14px;
  overflow-x: auto;
  border-radius: 10px;
  background: var(--assistant-surface);
  line-height: 1.55;
  overflow-wrap: anywhere;
}
.agent-result-surface ::v-deep button { --current-color: var(--assistant-action); }

.agent-query-details { margin-top: 8px; border: 1px solid #dfe4ed; border-radius: 6px; background: #fff; }
.agent-query-details summary { padding: 9px 10px; cursor: pointer; color: #303133; font-weight: 500; }
.agent-query-details summary:focus-visible { outline: 2px solid #409eff; outline-offset: -2px; }
.agent-query-details__body { padding: 0 10px 10px; }
.agent-query-group { padding: 8px 0 0; border-top: 1px solid #ebeef5; }
.agent-query-group + .agent-query-group { margin-top: 10px; }
.agent-query-group h4 { margin: 0; font-size: 13px; overflow-wrap: anywhere; }
.agent-query-record { margin-top: 6px; }
.agent-query-record summary { padding: 6px 0; font-size: 12px; }
.agent-query-attempt__meta { display: flex; flex-wrap: wrap; gap: 2px 10px; margin: 0; color: #606266; font-size: 12px; overflow-wrap: anywhere; }
.agent-query-attempt__meta:empty { display: none; }
.agent-query-attempt + .agent-query-attempt { margin-top: 10px; }
.agent-query-error { color: #b83232; overflow-wrap: anywhere; }

.agent-message {
  color: var(--assistant-text);
  font-size: 14px;
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.agent-entry--user .agent-message { max-width: 88%; padding: 9px 12px; border-radius: 12px 12px 3px 12px; background: #eaf2fc; }
.agent-message--follow-up { margin: 8px 0; color: var(--assistant-action); }
.agent-message--error { margin: 8px 0 0; padding: 7px 9px; border-radius: 8px; color: #922e38; background: #fff1f2; font-size: 12px; }
.agent-input-status { margin-top: 4px; color: var(--assistant-muted); font-size: 12px; text-align: right; }
.agent-input-status button { border: 0; background: transparent; color: var(--assistant-action); cursor: pointer; }
.agent-progress { margin-top: 14px; padding-left: 10px; border-left: 2px solid var(--assistant-accent); }
.agent-progress__status { margin-top: 3px; color: var(--assistant-muted); font-size: 12px; }
.agent-panel__status {
  flex: 0 1 auto;
  min-height: 0;
  max-height: 26%;
  overflow: auto;
  overscroll-behavior: contain;
  padding: 5px 14px;
  border-top: 1px solid var(--assistant-line);
  background: var(--assistant-surface);
}
.agent-panel__status .agent-message { font-size: 12px; white-space: normal; }
.agent-message--loading {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  column-gap: 8px;
  color: var(--assistant-muted);
}
.agent-run-details { color: var(--assistant-muted); font-size: 12px; line-height: 1.45; overflow-wrap: anywhere; }
.agent-run-details summary { padding: 4px 0; color: var(--assistant-action); cursor: pointer; }
.agent-run-usage,
.agent-context-status { margin: 4px 0 7px; }

.agent-welcome { max-width: 440px; margin: clamp(12px, 4vh, 32px) auto 0; }
.agent-welcome h3 { margin: 0 0 8px; font-size: 22px; font-weight: 700; line-height: 1.3; }
.agent-welcome p { margin: 0 0 16px; color: var(--assistant-muted); line-height: 1.65; }
.agent-welcome__examples { display: flex; flex-wrap: wrap; gap: 8px; }
.agent-example {
  min-height: 34px;
  padding: 6px 10px;
  border: 1px solid var(--assistant-line);
  border-radius: 8px;
  background: var(--assistant-surface);
  color: var(--assistant-action);
  font: inherit;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}
.agent-example:hover:not(:disabled) { background: #eaf2fc; }

.agent-panel__composer {
  flex-shrink: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 6px 8px;
  padding: 10px 14px 7px;
  border-top: 1px solid var(--assistant-line);
}
.agent-panel__composer textarea {
  grid-column: 1 / -1;
  width: 100%;
  box-sizing: border-box;
  resize: none;
  padding: 9px 10px;
  border: 1px solid var(--assistant-line);
  border-radius: 9px;
  background: var(--assistant-surface);
  color: var(--assistant-text);
  font: inherit;
  line-height: 1.35;
}
.agent-panel__composer select {
  justify-self: start;
  min-width: 0;
  padding: 0 5px;
  border: 1px solid var(--assistant-line);
  border-radius: 7px;
  background: #fff;
  color: var(--assistant-text);
  font: inherit;
  font-size: 12px;
}
.agent-panel__composer button {
  grid-column: 2;
  min-width: 68px;
  min-height: 34px;
  border: 0;
  border-radius: 8px;
  background: var(--assistant-action);
  color: #fff;
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}
.agent-panel__composer button:hover:not(:disabled) { background: #104f8e; }

.agent-panel__composer button:disabled,
.agent-example:disabled { opacity: 0.55; cursor: not-allowed; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }

@media (max-width: 600px) {
  .agent-launcher { right: 16px; bottom: 16px; }
  .agent-panel__resize { display: none; }
  .agent-panel { inset: 0; width: 100%; height: 100%; border: 0; border-radius: 0; }
  .agent-panel__header { padding-left: 14px; }
}
.agent-task-card {
  flex: 0 1 auto;
  min-height: 0;
  max-height: 24%;
  margin: 8px 12px 0;
  padding: 5px 8px;
  overflow: auto;
  overscroll-behavior: contain;
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
  color: var(--assistant-text);
  flex-wrap: wrap;
  text-align: left;
}
.agent-task-card button.agent-task-card__toggle:hover {
  background: #f0f5fb;
}
.agent-task-card__summary { color: #606266; font-weight: 400; }
.agent-task-card__current { max-height: 3.5em; margin: 5px 0 0; overflow: auto; color: var(--assistant-muted); font-size: 12px; overflow-wrap: anywhere; }
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
  margin: 0;
  padding: 0;
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

