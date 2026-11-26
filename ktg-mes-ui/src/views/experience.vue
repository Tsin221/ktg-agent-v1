<template>
  <main :class="agentReady ? 'experience-agent' : 'experience-entry'">
    <agent-assistant v-if="agentReady" :embedded="true" />
    <template v-else>
      <h1>{{ agentOnly ? 'KTG 智能助手' : 'KTG MES' }}</h1>
      <p aria-live="polite">{{ error || '正在进入体验…' }}</p>
      <button v-if="error" type="button" @click="enter">重新连接</button>
    </template>
  </main>
</template>

<script>
import { setToken } from '@/utils/auth'
import AgentAssistant from '@/components/AgentAssistant'
export default {
  name: 'ExperienceEntry',
  components: { AgentAssistant },
  data: () => ({ error: '', agentReady: false }),
  computed: { agentOnly() { return this.$route.query.view === 'agent' } },
  mounted() { this.enter() },
  methods: {
    async enter() {
      this.error = ''
      try {
        const response = await fetch('/demo-session', { method: 'POST', credentials: 'same-origin' })
        const result = await response.json()
        if (!response.ok || !result.token) throw new Error('体验服务暂时无法连接')
        setToken(result.token)
        this.$store.commit('SET_TOKEN', result.token)
        this.$store.commit('SET_ROLES', [])
        this.$store.commit('SET_PERMISSIONS', [])
        if (this.agentOnly) {
          await this.$store.dispatch('GetInfo')
          this.agentReady = true
          return
        }
        this.$router.replace('/index', () => {}, failure => {
          if (![2, 8, 16].includes(failure.type)) this.error = '页面加载失败，请重试。'
        })
      } catch (_) { this.error = '体验服务暂时无法连接，请重试。' }
    }
  }
}
</script>

<style scoped>
.experience-agent { width: 100%; height: 100vh; }
.experience-entry { min-height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center; color: #172334; background: #fff; }
.experience-entry h1 { font-size: 28px; }
.experience-entry p { color: #5a6e88; }
.experience-entry button { padding: 12px 24px; border: 0; border-radius: 8px; background: #245bea; color: white; cursor: pointer; }
</style>
