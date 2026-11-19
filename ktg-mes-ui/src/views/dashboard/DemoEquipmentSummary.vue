<template>
  <el-row :gutter="20" class="demo-equipment-summary">
    <el-col v-for="item in counts" :key="item.label" :xs="12" :sm="6">
      <el-card shadow="never"><span>{{ item.label }}</span><strong>{{ item.value === null ? '—' : item.value }}</strong></el-card>
    </el-col>
  </el-row>
</template>
<script>
import { listMachinery } from '@/api/mes/dv/machinery'
export default {
  data: () => ({ counts: [
    { label: '设备总数', value: null }, { label: '生产中', status: 'WORKING', value: null },
    { label: '维修中', status: 'REPAIR', value: null }, { label: '停机', status: 'STOP', value: null }
  ] }),
  mounted() {
    this.counts.forEach(item => listMachinery({ pageNum: 1, pageSize: 1, ...(item.status ? { status: item.status } : {}) })
      .then(result => { item.value = result.total }).catch(() => { item.value = null }))
  }
}
</script>
<style scoped>
.demo-equipment-summary { margin: 18px 0 28px; }
.el-col { margin-bottom: 12px; }
span { color: #60718a; }
strong { display: block; font-size: 28px; margin-top: 14px; }
</style>
