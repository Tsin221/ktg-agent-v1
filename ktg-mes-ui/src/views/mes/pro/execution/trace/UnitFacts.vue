<template>
  <div class="facts">
    <section aria-label="工单及固定工艺" class="fact-section">
      <el-descriptions title="所属工单及固定工艺" :column="2" border>
        <el-descriptions-item label="工单">{{ result.workorderCode }} · {{ result.workorderName || '—' }}（ID {{ result.workorderId }}）</el-descriptions-item>
        <el-descriptions-item label="工单状态">{{ orderLabel(result.orderStatus) }}</el-descriptions-item>
        <el-descriptions-item label="固定路线">{{ result.routeCode || '—' }} · {{ result.routeName || '—' }}（ID {{ result.routeId }}）</el-descriptions-item>
        <el-descriptions-item label="下达时间 / 人员 ID">{{ result.releasedAt || '—' }} / {{ result.releaseUserId || '—' }}</el-descriptions-item>
        <el-descriptions-item label="当前任务 ID">{{ result.currentTaskId || '—' }}</el-descriptions-item>
        <el-descriptions-item label="单件记录更新时间">{{ result.updatedAt || '—' }}</el-descriptions-item>
      </el-descriptions>
      <h3>工序任务与返工任务</h3>
      <p class="hint">普通工序为工单固定路线；返工任务仅展示本件已关联记录。</p>
      <p v-if="!(result.tasks || []).length" class="hint">暂无已采集工序任务。</p>
      <el-table v-else :data="result.tasks || []" row-key="taskId">
        <el-table-column prop="taskCode" label="任务编号" min-width="140" show-overflow-tooltip />
        <el-table-column label="工序" min-width="140"><template slot-scope="{ row }">{{ row.processName || stepLabel(row.stepKind) }}<span class="subline">{{ row.taskKind === 'REWORK' ? '返工' : '普通工序 ' + (row.normalStepNo || '') }} · ID {{ row.taskId }}</span></template></el-table-column>
        <el-table-column label="任务状态" width="110"><template slot-scope="{ row }">{{ taskLabel(row.status) }}</template></el-table-column>
        <el-table-column prop="assignedUserId" label="分派人员 ID" width="125" />
        <el-table-column label="实际开工 / 人员 ID" min-width="180"><template slot-scope="{ row }">{{ row.actualStartedAt || '—' }}<span class="subline">{{ row.startedBy || '—' }}</span></template></el-table-column>
        <el-table-column label="实际完成 / 人员 ID" min-width="180"><template slot-scope="{ row }">{{ row.actualEndedAt || '—' }}<span class="subline">{{ row.endedBy || '—' }}</span></template></el-table-column>
      </el-table>
    </section>

    <section aria-label="实际投入与报工" class="fact-section">
      <h3>装配报工与本件实际投入</h3>
      <el-descriptions v-if="result.assemblyFeedback" :column="2" border>
        <el-descriptions-item label="原报工单 ID"><el-button v-if="canReadFeedback" type="text" @click="$emit('document', 'feedback', result.assemblyFeedback.recordId)">{{ result.assemblyFeedback.recordId }} · 查看</el-button><span v-else>{{ result.assemblyFeedback.recordId }}</span></el-descriptions-item>
        <el-descriptions-item label="报工时间">{{ result.assemblyFeedback.feedbackTime || '—' }}</el-descriptions-item>
        <el-descriptions-item label="报工人">{{ result.assemblyFeedback.nickName || result.assemblyFeedback.userName || '—' }}</el-descriptions-item>
        <el-descriptions-item label="原报工单数量（整单）">{{ result.assemblyFeedback.quantityFeedback }}</el-descriptions-item>
      </el-descriptions>
      <p v-else class="hint">暂无本件关联的装配报工记录。</p>
      <p v-if="!(result.inputs || []).length" class="hint">暂无本件实际投入采集记录。</p>
      <el-table v-else :data="result.inputs || []" row-key="inputId">
        <el-table-column type="expand">
          <template slot-scope="{ row }">
            <div class="expanded">
              <p>投入记录 ID {{ row.inputId }} · 任务 ID {{ row.taskId }} · 报工 ID {{ row.feedbackId }} · 库存行 ID {{ row.materialStockId }} · 耗料单 ID {{ row.consumeRecordId }} · 耗料行 ID {{ row.consumeLineId }}</p>
              <p v-if="!(row.transactions || []).length" class="hint">无可见关联库存流水。</p>
              <el-table v-else :data="row.transactions || []" row-key="transactionId" size="mini">
                <el-table-column prop="transactionId" label="流水 ID" min-width="100" />
                <el-table-column prop="transactionType" label="流水类型" min-width="120" />
                <el-table-column prop="sourceDocCode" label="来源单号" min-width="140" />
                <el-table-column prop="transactionFlag" label="增减标记" width="100" />
                <el-table-column prop="transactionQuantity" label="整耗料行流水数量" width="155" />
                <el-table-column prop="transactionDate" label="流水时间" min-width="180" />
              </el-table>
              <p class="hint">库存流水可能汇总多个 SN 的实际投入；本件数量仅以上方投入记录为准。</p>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="原料" min-width="165"><template slot-scope="{ row }">{{ row.itemCode || '—' }} · {{ row.itemName || '—' }}<span class="subline">物料 ID {{ row.itemId }}</span></template></el-table-column>
        <el-table-column label="实际原料批次" min-width="160"><template slot-scope="{ row }"><el-button class="raw-code" type="text" @click="$emit('trace-material', row.itemId, row.sourceBatchCode)">{{ row.sourceBatchCode }}</el-button></template></el-table-column>
        <el-table-column label="本件投入量" min-width="120"><template slot-scope="{ row }">{{ row.quantity }} {{ row.unitOfMeasure }}</template></el-table-column>
        <el-table-column prop="usedBy" label="操作人 ID" width="105" />
        <el-table-column prop="usedAt" label="投入时间" min-width="180" />
      </el-table>
    </section>

    <section aria-label="历次质量检验" class="fact-section">
      <h3>历次质量检验</h3>
      <p v-if="!(result.inspections || []).length" class="hint">暂无本件已提交检验记录。</p>
      <article v-for="record in result.inspections || []" :key="record.inspectionId" class="inspection">
        <h4>第 {{ record.roundNo }} 轮检验 <el-tag size="small" :type="record.checkResult === 'ACCEPT' ? 'success' : record.checkResult === 'REJECT' ? 'danger' : 'info'">{{ qualityLabel(record.checkResult) }}</el-tag></h4>
        <p>检验记录 ID {{ record.inspectionId }} · 过程检验单 {{ record.ipqcCode || record.ipqcId }} · {{ record.inspectDate || '—' }} · 检验人 {{ record.inspector || '—' }}</p>
        <p>工序任务 ID {{ record.taskId }} · 合格产出行 ID {{ record.produceLineId || '无关联' }} · 来源处置 ID {{ record.sourceDispositionId || '无关联' }} <el-button v-if="canReadIpqc" type="text" @click="$emit('document', 'ipqc', record.ipqcId)">查看原检验单</el-button></p>
        <p v-if="!(record.lines || []).length" class="hint">暂无关联指标记录。</p>
        <el-table v-else :data="record.lines || []" row-key="lineId" size="mini">
          <el-table-column label="检验指标" min-width="150"><template slot-scope="{ row }">{{ row.indexName || row.indexCode || '—' }}<span class="subline">ID {{ row.indexId || '—' }}</span></template></el-table-column>
          <el-table-column label="实际结果" min-width="130"><template slot-scope="{ row }">{{ historicalValue(row) }}</template></el-table-column>
          <el-table-column label="检验要求" min-width="180"><template slot-scope="{ row }">{{ row.standerVal || '—' }} {{ row.unitOfMeasure || '' }}<span class="subline">{{ row.checkMethod || '' }}<template v-if="row.thresholdMin != null || row.thresholdMax != null"> · 范围 {{ row.thresholdMin }} 至 {{ row.thresholdMax }}</template></span></template></el-table-column>
          <el-table-column label="致命 / 严重 / 轻微" min-width="145"><template slot-scope="{ row }">{{ display(row.crQuantity) }} / {{ display(row.majQuantity) }} / {{ display(row.minQuantity) }}</template></el-table-column>
        </el-table>
        <p v-for="defect in record.defects || []" :key="defect.recordId">缺陷：{{ defect.defectName }} · 等级 {{ defect.defectLevel || '—' }} · 数量 {{ display(defect.defectQuantity) }} · 记录 ID {{ defect.recordId }}<template v-if="defect.remark"> · {{ defect.remark }}</template></p>
        <template v-if="record.produceLineId">
          <p>合格产出事实：产出单 ID {{ record.produceRecordId || '—' }} · 产出行 ID {{ record.produceLineId }} · 成品批次 {{ record.producedBatchCode || '—' }} · 产出数量 {{ display(record.quantityProduce) }}</p>
          <p v-if="!(record.produceTransactions || []).length" class="hint">无可见关联产出流水。</p>
          <el-table v-else :data="record.produceTransactions || []" row-key="transactionId" size="mini">
            <el-table-column prop="transactionId" label="产出流水 ID" min-width="115" />
            <el-table-column prop="transactionType" label="类型" min-width="120" />
            <el-table-column prop="transactionFlag" label="增减标记" width="100" />
            <el-table-column prop="transactionQuantity" label="流水数量" width="110" />
            <el-table-column prop="transactionDate" label="流水时间" min-width="180" />
          </el-table>
        </template>
      </article>
    </section>

    <section aria-label="处置与包装" class="fact-section">
      <h3>不合格处置历史</h3>
      <p v-if="!(result.dispositions || []).length" class="hint">暂无本件不合格处置记录。</p>
      <el-table v-else :data="result.dispositions || []" row-key="dispositionId">
        <el-table-column prop="dispositionId" label="处置 ID" width="100" />
        <el-table-column prop="inspectionId" label="检验记录 ID" min-width="115" />
        <el-table-column label="处置决定" width="100"><template slot-scope="{ row }">{{ { REWORK: '返工', SCRAP: '报废' }[row.decision] || row.decision }}</template></el-table-column>
        <el-table-column prop="reason" label="原因" min-width="180" />
        <el-table-column prop="reworkTaskId" label="返工任务 ID" min-width="115" />
        <el-table-column prop="decidedBy" label="决定人 ID" width="115" />
        <el-table-column prop="decidedAt" label="处置时间" min-width="180" />
      </el-table>
      <h3>包装结果</h3>
      <el-descriptions v-if="result.packing" :column="2" border>
        <el-descriptions-item label="包装任务 ID">{{ result.packing.taskId }}</el-descriptions-item>
        <el-descriptions-item label="包装时间">{{ result.packing.packedAt || '—' }}</el-descriptions-item>
        <el-descriptions-item label="包装人 ID">{{ result.packing.packedBy || '—' }}</el-descriptions-item>
        <el-descriptions-item label="装箱关联">{{ result.packing.packageCode || result.packing.packageId || '无箱号关联' }}<template v-if="result.packing.packageLineId">（装箱行 {{ result.packing.packageLineId }}）</template></el-descriptions-item>
      </el-descriptions>
      <p v-else class="hint">暂无本件包装事实记录。</p>
    </section>
  </div>
</template>

<script>
import { taskLabel, stepLabel } from '../taskPresentation'
import { hasTracePermission } from './tracePresentation'

export default {
  name: 'UnitFacts',
  props: { result: { type: Object, required: true } },
  computed: {
    canReadIpqc() { return hasTracePermission(this.$store.getters.permissions || [], 'mes:qc:ipqc:query') },
    canReadFeedback() { return hasTracePermission(this.$store.getters.permissions || [], 'mes:pro:feedback:query') }
  },
  methods: {
    taskLabel, stepLabel,
    display(value) { return value == null ? '—' : value },
    orderLabel(value) { return { PREPARE: '草稿', CONFIRMED: '已下达', FINISHED: '已完结' }[value] || value || '—' },
    qualityLabel(value) { return { ACCEPT: '合格', REJECT: '不合格' }[value] || value || '未记录结论' },
    historicalValue(line) {
      if (!line.remark) return '未记录实际结果'
      try {
        const fact = JSON.parse(line.remark)
        if (fact.value != null) return fact.value
        if (typeof fact.passed === 'boolean') return fact.passed ? '符合' : '不符合'
        return line.remark
      } catch (_) { return line.remark }
    }
  }
}
</script>

<style scoped>
.fact-section { margin-top: 24px; padding-top: 16px; border-top: 1px solid #ebeef5; }
h3 { font-size: 16px; margin: 20px 0 12px; }
h4 { font-size: 14px; margin: 10px 0; }
.hint, .subline, .inspection p { color: #606266; line-height: 1.6; overflow-wrap: anywhere; }
.subline { display: block; font-size: 12px; }
.inspection { border: 1px solid #ebeef5; padding: 12px; margin-bottom: 12px; }
.expanded { padding: 0 16px; overflow-wrap: anywhere; }
.raw-code { white-space: pre-wrap; overflow-wrap: anywhere; text-align: left; }
</style>
