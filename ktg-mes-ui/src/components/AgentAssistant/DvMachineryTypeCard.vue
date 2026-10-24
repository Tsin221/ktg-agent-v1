<template>
  <section class="dv-card" :class="`dv-card--${tone}`" :aria-label="title">
    <header class="dv-card__header">
      <strong>{{ title }}</strong>
      <span v-if="result.outcome" class="dv-card__status">{{ statusText }}</span>
      <span v-else-if="isTree && result.total !== undefined" class="dv-card__status">共 {{ result.total }} 个</span>
    </header>

    <template v-if="isTree">
      <div v-if="result.rows && result.rows.length" class="dv-card__rows">
        <div v-for="(row, index) in result.rows" :key="row.machineryTypeId || index" class="dv-card__row">
          <strong>{{ row.path || row.machineryTypeName }}</strong>
          <span>{{ row.machineryTypeCode || '-' }} · {{ enableText(row.enableFlag) }}</span>
        </div>
      </div>
      <p v-else class="dv-card__empty">没有符合条件的数据</p>
      <p class="dv-card__meta">
        {{ result.truncated ? `还有 ${result.total - result.returned} 个未显示，请缩小筛选范围` : '已显示全部结果' }}
      </p>
    </template>

    <template v-else-if="isDetail">
      <dl class="dv-card__facts">
        <div><dt>路径</dt><dd>{{ result.node.path }}</dd></div>
        <div><dt>编码</dt><dd>{{ result.node.machineryTypeCode }}（ID {{ result.node.machineryTypeId }}）</dd></div>
        <div><dt>父分类</dt><dd>{{ result.node.parentName || '无（根分类）' }}</dd></div>
        <div><dt>启用状态</dt><dd>{{ enableText(result.node.enableFlag) }}</dd></div>
        <div><dt>直接子分类</dt><dd>{{ childText }}</dd></div>
        <div><dt>子树内设备</dt><dd>{{ result.device.subtreeTotal }} 台{{ deviceText }}</dd></div>
        <div><dt>维修单引用</dt><dd>{{ result.repairTotal === null ? '未核验' : `${result.repairTotal} 张` }}</dd></div>
        <div><dt>删除</dt><dd>已停用</dd></div>
        <div><dt>父类型</dt><dd>不可修改</dd></div>
      </dl>
      <p v-if="result.checkPlanNote" class="dv-card__meta">{{ result.checkPlanNote }}</p>
    </template>

    <template v-else>
      <dl class="dv-card__facts">
        <div v-if="preview && result.after.parentPath"><dt>父分类</dt><dd>{{ result.after.parentPath }}</dd></div>
        <div v-if="preview && result.action === 'create'"><dt>名称</dt><dd>{{ result.after.machineryTypeName }}</dd></div>
        <div v-if="preview && result.action === 'update'"><dt>名称</dt><dd>{{ result.before.machineryTypeName }} → {{ result.after.machineryTypeName }}</dd></div>
        <div v-if="preview && result.action === 'update'"><dt>启用状态</dt><dd>{{ enableText(result.before.enableFlag) }} → {{ enableText(result.after.enableFlag) }}</dd></div>
        <div v-if="preview && result.action === 'create'"><dt>启用状态</dt><dd>{{ enableText(result.after.enableFlag) }}</dd></div>
        <div v-if="preview && result.after.remark"><dt>备注</dt><dd>{{ result.after.remark }}</dd></div>
        <div v-if="preview && result.action === 'create'"><dt>编码</dt><dd>确认时由系统生成</dd></div>
        <div v-if="result.kind === 'dv_machinerytype_create_result' && result.created">
          <dt>已新增</dt><dd>{{ result.created.machineryTypeName }}（{{ result.created.machineryTypeCode }}）</dd>
        </div>
        <div v-if="result.kind === 'dv_machinerytype_create_result' && result.created">
          <dt>路径</dt><dd>{{ result.created.path }}</dd>
        </div>
        <div v-if="result.kind === 'dv_machinerytype_update_result' && result.updated">
          <dt>已保存</dt><dd>{{ result.updated.machineryTypeName }}（{{ result.updated.machineryTypeCode }}）</dd>
        </div>
        <div v-if="result.kind === 'dv_machinerytype_update_result' && result.updated">
          <dt>路径</dt><dd>{{ result.updated.path }}</dd>
        </div>
      </dl>

      <p v-if="preview" class="dv-card__warning">确认后将使用当前登录账号写入，预览内容不可修改。</p>
      <p v-else-if="result.outcome === 'unknown'" class="dv-card__warning">
        系统不会自动重试，请到设备类型页面按名称核对后再操作。
      </p>
      <p v-if="preview && result.warnings && result.warnings.length" class="dv-card__warning">
        {{ result.warnings.join('；') }}
      </p>
    </template>

    <div v-if="preview" class="dv-card__actions">
      <button
        type="button"
        class="dv-card__primary"
        :disabled="confirming || completed"
        @click="$emit('confirm', result.preview_id)"
      >{{ confirming ? '正在核验…' : completed ? '已提交确认' : (result.action === 'update' ? '确认修改' : '确认新增') }}</button>
    </div>
  </section>
</template>

<script>
export default {
  name: 'AgentDvMachineryTypeCard',
  props: {
    result: { type: Object, required: true },
    confirming: { type: Boolean, default: false },
    completed: { type: Boolean, default: false }
  },
  computed: {
    preview() { return this.result.kind === 'dv_machinerytype_preview' },
    isTree() { return this.result.kind === 'dv_machinerytype_tree' },
    isDetail() { return this.result.kind === 'dv_machinerytype_detail' },
    title() {
      if (this.isTree) return '设备分类查询结果'
      if (this.isDetail) return '设备分类详情'
      if (this.preview) return this.result.action === 'update' ? '设备分类修改预览' : '设备分类新增预览'
      return this.result.label || '设备分类操作结果'
    },
    tone() {
      if (this.preview) return 'preview'
      if (this.isTree || this.isDetail) return 'neutral'
      return this.result.outcome || 'neutral'
    },
    statusText() {
      if (this.result.outcome === 'succeeded') return '已完成'
      if (this.result.outcome === 'unknown') return '待核对'
      return '未完成'
    },
    childText() {
      const children = this.result.children || []
      if (!children.length) return '无'
      const names = children.slice(0, 10).map(child => child.machineryTypeName)
      return names.join('、') + (children.length > 10 ? ` 等 ${children.length} 个` : '')
    },
    deviceText() {
      const sample = (this.result.device && this.result.device.sample) || []
      if (!sample.length) return ''
      const names = sample.slice(0, 3).map(item => `${item.machineryCode || ''} ${item.machineryName || ''}`.trim())
      return '（样例：' + names.join('、') + '）'
    }
  },
  methods: {
    enableText(value) { return value === 'Y' ? '启用' : value === 'N' ? '停用' : '—' }
  }
}
</script>

<style lang="scss" scoped>
.dv-card { margin-top: 10px; padding: 12px; border: 1px solid #dfe4ed; border-left-width: 4px; border-radius: 6px; background: #fff; color: #303133; }
.dv-card--preview { border-left-color: #e6a23c; }
.dv-card--succeeded { border-left-color: #67c23a; }
.dv-card--failed, .dv-card--unknown { border-left-color: #f56c6c; }
.dv-card__header, .dv-card__facts div, .dv-card__actions { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.dv-card__header { font-size: 14px; }
.dv-card__status { color: #606266; font-size: 12px; }
.dv-card__facts { margin: 10px 0 0; font-size: 12px; }
.dv-card__facts div { padding: 4px 0; }
.dv-card__facts dt { color: #909399; }
.dv-card__facts dd { margin: 0; text-align: right; overflow-wrap: anywhere; }
.dv-card__rows { margin-top: 8px; }
.dv-card__row { display: flex; flex-direction: column; gap: 3px; padding: 8px 0; border-top: 1px solid #ebeef5; color: #606266; font-size: 12px; }
.dv-card__row strong { color: #303133; font-size: 13px; }
.dv-card__empty, .dv-card__meta { margin: 8px 0 0; color: #909399; font-size: 12px; }
.dv-card__warning { margin: 9px 0 0; color: #606266; font-size: 12px; line-height: 1.5; }
.dv-card__actions { justify-content: flex-start; margin-top: 10px; }
.dv-card__actions button { min-height: 34px; padding: 0 12px; border-radius: 4px; cursor: pointer; }
.dv-card__primary { border: 0; background: var(--current-color, #409eff); color: #fff; }
.dv-card__actions button:disabled { opacity: 0.55; cursor: not-allowed; }
</style>
