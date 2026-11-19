<template>
  <main class="experience-entry" aria-live="polite">
    <h1>KTG MES</h1>
    <p>{{ error || '正在进入体验…' }}</p>
    <button v-if="error" type="button" @click="enter">重新连接</button>
  </main>
</template>

<script>
import { setToken } from '@/utils/auth'
export default {
  name: 'ExperienceEntry',
  data: () => ({ error: '' }),
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
        this.$router.replace('/index', () => {}, failure => {
          if (![2, 8, 16].includes(failure.type)) this.error = '页面加载失败，请重试。'
        })
      } catch (_) { this.error = '体验服务暂时无法连接，请重试。' }
    }
  }
}
</script>

<style scoped>
.experience-entry { min-height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center; color: #172334; background: #fff; }
.experience-entry h1 { font-size: 28px; }
.experience-entry p { color: #5a6e88; }
.experience-entry button { padding: 12px 24px; border: 0; border-radius: 8px; background: #245bea; color: white; cursor: pointer; }
</style>
