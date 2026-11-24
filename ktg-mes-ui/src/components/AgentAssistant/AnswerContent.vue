<template>
  <div class="agent-answer-content">
    <template v-for="(block, index) in blocks">
      <div v-if="block.type === 'table'" :key="index" class="agent-answer-table-wrap" role="region" aria-label="回复数据表" tabindex="0">
        <table class="agent-answer-table">
          <thead><tr><th v-for="(header, column) in block.headers" :key="column" scope="col">{{ plain(header) }}</th></tr></thead>
          <tbody><tr v-for="(row, rowIndex) in block.rows" :key="rowIndex">
            <td v-for="(cell, column) in row" :key="column">{{ plain(cell) }}</td>
          </tr></tbody>
        </table>
      </div>
      <div v-else :key="index" class="agent-answer-text"><span v-for="(part, partIndex) in inlineParts(block.text)" :key="partIndex"><strong v-if="part.bold">{{ part.text }}</strong><template v-else>{{ part.text }}</template></span></div>
    </template>
  </div>
</template>

<script>
import { answerBlocks } from './sessionPresentation'

export default {
  name: 'AgentAnswerContent',
  props: { text: { type: String, required: true } },
  computed: { blocks() { return answerBlocks(this.text) } },
  methods: {
    plain(value) { return value.replace(/\*\*([^*]+)\*\*/g, '$1') },
    inlineParts(value) {
      return value.split(/\*\*([^*]+)\*\*/g).map((text, index) => ({ text, bold: index % 2 === 1 }))
    }
  }
}
</script>

<style scoped>
.agent-answer-content > * + * { margin-top: 8px; }
.agent-answer-text { white-space: pre-wrap; overflow-wrap: anywhere; }
.agent-answer-table-wrap { max-width: 100%; overflow-x: auto; white-space: normal; border: 1px solid #dfe4ed; border-radius: 6px; }
.agent-answer-table { width: 100%; border-collapse: collapse; text-align: left; line-height: 1.5; font-size: 13px; }
.agent-answer-table th, .agent-answer-table td { padding: 8px 10px; min-width: 70px; border-bottom: 1px solid #dfe4ed; vertical-align: top; }
.agent-answer-table th { color: #303133; font-weight: 600; background: #f2f6fc; }
.agent-answer-table tr:last-child td { border-bottom: 0; }
</style>
