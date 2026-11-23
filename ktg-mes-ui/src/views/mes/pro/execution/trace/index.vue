<template>
  <div class="app-container execution-trace">
    <h1>双向追溯</h1>
    <p class="intro">根据实际投入、工序及单件质量记录追溯；不以 BOM、同工单或当前库存余额推断历史。</p>
    <el-tabs v-if="permitted" v-model="activeTab">
      <el-tab-pane label="成品 SN 追溯" name="unit">
        <unit-trace-panel ref="unit" @trace-material="openMaterial" />
      </el-tab-pane>
      <el-tab-pane label="原料批次追溯" name="material">
        <material-trace-panel ref="material" @trace-unit="openUnit" />
      </el-tab-pane>
    </el-tabs>
    <el-alert v-else title="当前账号没有双向追溯查询权限。" type="warning" :closable="false" show-icon />
  </div>
</template>

<script>
import UnitTracePanel from './UnitTracePanel.vue'
import MaterialTracePanel from './MaterialTracePanel.vue'
import { hasTracePermission } from './tracePresentation'

export default {
  name: 'ExecutionTrace',
  components: { UnitTracePanel, MaterialTracePanel },
  data: () => ({ activeTab: 'unit' }),
  computed: {
    permitted() {
      const permissions = this.$store.getters.permissions || []
      return ['query', 'trace'].every(action => hasTracePermission(permissions, 'mes:pro:execution:' + action))
    }
  },
  methods: {
    openUnit(snCode) {
      this.activeTab = 'unit'
      this.$nextTick(() => { if (this.$refs.unit) this.$refs.unit.search(snCode) })
    },
    openMaterial(itemId, batchCode) {
      this.activeTab = 'material'
      this.$nextTick(() => { if (this.$refs.material) this.$refs.material.open(itemId, batchCode) })
    }
  }
}
</script>

<style scoped>
.execution-trace h1 { font-size: 23px; margin: 0 0 8px; }
.intro { color: #606266; line-height: 1.6; margin-bottom: 20px; }
</style>
