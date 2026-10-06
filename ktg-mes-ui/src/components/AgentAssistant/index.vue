<template>
  <div class="agent-assistant">
    <button
      v-if="!open"
      class="agent-launcher"
      type="button"
      aria-label="打开 KTG 智能查询助手"
      @click="openPanel"
    >
      <i class="el-icon-chat-dot-round" aria-hidden="true" />
      <span>智能查询</span>
    </button>

    <section v-else class="agent-panel" role="dialog" aria-label="KTG 智能查询助手">
      <header class="agent-panel__header">
        <div>
          <h2>KTG 智能查询</h2>
          <p>{{ currentUser ? `当前用户：${currentUser}` : '使用当前登录账号权限' }}</p>
        </div>
        <button ref="closeButton" type="button" aria-label="收起智能查询助手" @click="closePanel">
          <i class="el-icon-minus" aria-hidden="true" />
        </button>
      </header>

      <div ref="messages" class="agent-panel__messages" aria-live="polite">
        <div class="agent-message agent-message--assistant">
          我可以查询产品、分仓库存和生产工单。所有结果都按你当前账号的权限读取。
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
          <agent-result-card
            v-for="(result, index) in message.results || []"
            :key="`${message.id}-${index}`"
            :result="result"
          />
        </div>
        <div v-if="loading" class="agent-message agent-message--assistant agent-message--loading" role="status">
          正在按当前用户权限查询…
        </div>
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
          :disabled="loading"
          @keydown.enter.exact.prevent="send"
        />
        <button type="submit" :disabled="loading || !draft.trim()">
          <i class="el-icon-s-promotion" aria-hidden="true" />
          <span>发送</span>
        </button>
      </form>
    </section>
  </div>
</template>

<script>
import { queryAgent } from '@/api/agent'
import AgentResultCard from './ResultCard.vue'

export default {
  name: 'AgentAssistant',
  components: { AgentResultCard },
  data() {
    return {
      open: false,
      loading: false,
      draft: '',
      currentUser: '',
      messageId: 0,
      messages: [],
      examples: ['查询最近的产品', '查询 A-01 的库存', '查询最近的生产工单']
    }
  },
  methods: {
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
      if (!text || this.loading) return
      this.messages.push({ id: ++this.messageId, role: 'user', text })
      this.draft = ''
      this.loading = true
      this.scrollToBottom()
      try {
        const response = await queryAgent(text)
        this.currentUser = response.current_user || this.currentUser
        this.messages.push({
          id: ++this.messageId,
          role: 'assistant',
          text: response.answer,
          results: response.results || []
        })
      } catch (error) {
        const data = error && error.response && error.response.data
        const message = data && data.error && data.error.message
        this.messages.push({
          id: ++this.messageId,
          role: 'assistant',
          text: message || '查询失败，请确认 Agent 服务与模型配置后重试。'
        })
      } finally {
        this.loading = false
        this.scrollToBottom()
        this.$nextTick(() => this.$refs.input && this.$refs.input.focus())
      }
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

