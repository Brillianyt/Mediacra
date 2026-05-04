<template>
  <div class="task-center-page">
    <n-space vertical :size="16">
      <n-card size="small" class="hero-card">
        <n-space justify="space-between" align="center" wrap>
          <div>
            <div class="hero-eyebrow">STRUCTURED MISSION CONTROL</div>
            <div class="hero-title">结构化任务中心</div>
            <n-text depth="3">集中查看历史任务、失败明细与重试入口，不再和结构化结果表混在一起。</n-text>
          </div>
          <n-space align="center" wrap>
            <n-select
              v-model:value="sourceTable"
              :options="tableOptions"
              clearable
              filterable
              placeholder="来源表"
              style="width: 220px"
            />
            <n-select
              v-model:value="statusFilter"
              :options="statusOptions"
              clearable
              placeholder="任务状态"
              style="width: 160px"
            />
            <n-button @click="reloadJobs" :loading="loadingJobs">刷新</n-button>
          </n-space>
        </n-space>
      </n-card>

      <div class="metric-grid">
        <n-card size="small" class="metric-card">
          <div class="metric-label">当前列表任务数</div>
          <div class="metric-value">{{ jobs.length }}</div>
        </n-card>
        <n-card size="small" class="metric-card">
          <div class="metric-label">运行中</div>
          <div class="metric-value">{{ runningCount }}</div>
        </n-card>
        <n-card size="small" class="metric-card">
          <div class="metric-label">失败任务</div>
          <div class="metric-value">{{ failedCount }}</div>
        </n-card>
        <n-card size="small" class="metric-card">
          <div class="metric-label">部分成功</div>
          <div class="metric-value">{{ partialSuccessCount }}</div>
        </n-card>
      </div>

      <div class="task-grid">
        <n-card size="small" title="任务列表">
          <n-data-table
            :columns="jobColumns"
            :data="jobs"
            size="small"
            :row-key="jobRowKey"
            :row-props="jobRowProps"
            :max-height="620"
            virtual-scroll
          />
          <div class="footer-actions">
            <n-button v-if="hasMore" @click="loadMore" :loading="loadingMore">加载更多</n-button>
            <n-text v-else depth="3">当前已加载全部任务</n-text>
          </div>
        </n-card>

        <n-space vertical :size="16">
          <n-card size="small" title="任务详情">
            <n-empty v-if="!selectedJob" description="从左侧选择一个任务查看详情" />
            <template v-else>
              <n-space justify="space-between" align="center" wrap>
                <n-space align="center" wrap>
                  <n-tag :type="jobTagType(selectedJob.status)">{{ jobStatusLabel(selectedJob.status) }}</n-tag>
                  <n-text depth="2">任务 #{{ selectedJob.id }}</n-text>
                  <n-text depth="3">{{ selectedJob.source_table || '-' }}</n-text>
                </n-space>
                <n-space>
                  <n-button size="small" quaternary @click="handleRefreshClick" :loading="detailLoading">刷新</n-button>
                  <n-button
                    v-if="canCancelSelectedJob"
                    size="small"
                    type="warning"
                    secondary
                    @click="cancelSelectedJob"
                    :loading="cancelling"
                  >
                    取消
                  </n-button>
                  <n-button
                    size="small"
                    type="primary"
                    secondary
                    @click="retrySelectedJob"
                    :loading="retrying"
                    :disabled="!canRetrySelectedJob"
                  >
                    重试
                  </n-button>
                </n-space>
              </n-space>

              <div class="detail-grid">
                <div class="detail-item">
                  <span class="detail-label">目标表</span>
                  <span class="detail-value">{{ selectedJob.target_table || '-' }}</span>
                </div>
                <div class="detail-item">
                  <span class="detail-label">触发方式</span>
                  <span class="detail-value">{{ selectedJob.trigger_type || '-' }}</span>
                </div>
                <div class="detail-item">
                  <span class="detail-label">当前阶段</span>
                  <span class="detail-value">{{ selectedJob.current_stage || 'queued' }}</span>
                </div>
                <div class="detail-item">
                  <span class="detail-label">创建时间</span>
                  <span class="detail-value">{{ formatDateTime(selectedJob.created_at) }}</span>
                </div>
                <div class="detail-item">
                  <span class="detail-label">处理进度</span>
                  <span class="detail-value">{{ selectedJob.processed_count || 0 }}/{{ selectedJob.total_count || 0 }}</span>
                </div>
                <div class="detail-item">
                  <span class="detail-label">结果摘要</span>
                  <span class="detail-value">
                    成功 {{ selectedJob.success_count || 0 }} / 失败 {{ selectedJob.failed_count || 0 }} / 跳过 {{ selectedJob.skipped_count || 0 }}
                  </span>
                </div>
              </div>

              <n-alert
                v-if="selectedJob.error_message"
                class="mt-4"
                type="error"
                :bordered="false"
                :show-icon="true"
              >
                {{ selectedJob.error_message }}
              </n-alert>
            </template>
          </n-card>

          <n-card size="small" title="失败明细">
            <n-empty v-if="!failedItems.length" description="当前任务没有失败项" />
            <n-space v-else vertical :size="10">
              <div
                v-for="item in failedItems"
                :key="item.id"
                class="failure-item"
              >
                <div class="failure-head">
                  <n-text strong>{{ item.source_title || `源记录 #${item.source_record_id}` }}</n-text>
                  <n-tag type="error" size="small">{{ jobStatusLabel(item.status) }}</n-tag>
                </div>
                <div class="failure-meta">
                  <span>记录 ID：{{ item.source_record_id }}</span>
                  <span>阶段：{{ item.current_stage || '-' }}</span>
                  <span>重试次数：{{ item.retry_count || 0 }}</span>
                </div>
                <div class="failure-reason">{{ item.error_message || item.result_payload?.reason || '未提供失败原因' }}</div>
              </div>
            </n-space>
          </n-card>

          <n-card size="small" title="最近处理项">
            <n-empty v-if="!jobItems.length" description="暂无任务项明细" />
            <n-space v-else vertical :size="8">
              <div v-for="item in jobItems.slice(0, 12)" :key="item.id" class="trace-item">
                <div class="trace-main">
                  <n-text>{{ item.source_title || `源记录 #${item.source_record_id}` }}</n-text>
                  <n-tag :type="jobTagType(item.status)" size="small">{{ jobStatusLabel(item.status) }}</n-tag>
                </div>
                <n-text depth="3">
                  {{ item.result_payload?.reason || item.error_message || item.quality_decision || item.current_stage || '-' }}
                </n-text>
              </div>
            </n-space>
          </n-card>
        </n-space>
      </div>
    </n-space>
  </div>
</template>

<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, ref } from 'vue'
import { NButton, NDataTable, NTag, NText, useMessage } from 'naive-ui'
import type { DataTableColumn } from 'naive-ui'
import http, { unwrapApiData } from '@/api'

const message = useMessage()
const loadingJobs = ref(false)
const loadingMore = ref(false)
const detailLoading = ref(false)
const cancelling = ref(false)
const retrying = ref(false)
const sourceTable = ref<string | null>(null)
const statusFilter = ref<string | null>(null)
const tableOptions = ref<Array<{ label: string; value: string }>>([])
const jobs = ref<any[]>([])
const selectedJob = ref<any | null>(null)
const jobItems = ref<any[]>([])
const offset = ref(0)
const limit = 20
const total = ref(0)
let pollTimer: number | null = null

const activeJobStatuses = ['pending', 'running', 'cancel_requested']
const statusOptions = [
  { label: '待执行', value: 'pending' },
  { label: '执行中', value: 'running' },
  { label: '成功', value: 'success' },
  { label: '部分成功', value: 'partial_success' },
  { label: '失败', value: 'failed' },
  { label: '已取消', value: 'cancelled' },
]

const jobRowKey = (row: any) => row.id
const hasMore = computed(() => jobs.value.length < total.value)
const failedItems = computed(() => jobItems.value.filter((item: any) => String(item.status || '') === 'failed'))
const runningCount = computed(() => jobs.value.filter((job: any) => activeJobStatuses.includes(String(job.status || ''))).length)
const failedCount = computed(() => jobs.value.filter((job: any) => String(job.status || '') === 'failed').length)
const partialSuccessCount = computed(() => jobs.value.filter((job: any) => String(job.status || '') === 'partial_success').length)
const canCancelSelectedJob = computed(() => !!selectedJob.value && activeJobStatuses.includes(String(selectedJob.value.status || '')))
const canRetrySelectedJob = computed(() => !!selectedJob.value && !activeJobStatuses.includes(String(selectedJob.value.status || '')) && Array.isArray(selectedJob.value.requested_ids) && selectedJob.value.requested_ids.length > 0)

function formatDateTime(value: string) {
  if (!value) return '-'
  const text = String(value).replace('T', ' ')
  return text.length > 19 ? text.slice(0, 19) : text
}

function jobStatusLabel(status: string) {
  const map: Record<string, string> = {
    pending: '待执行',
    running: '执行中',
    success: '成功',
    partial_success: '部分成功',
    failed: '失败',
    cancelled: '已取消',
    cancel_requested: '取消中',
    saved: '已保存',
    skipped: '已跳过',
  }
  return map[status] || status || '未知'
}

function jobTagType(status: string): 'default' | 'primary' | 'info' | 'success' | 'warning' | 'error' {
  if (status === 'success' || status === 'saved') return 'success'
  if (status === 'partial_success' || status === 'cancel_requested') return 'warning'
  if (status === 'failed') return 'error'
  if (status === 'running') return 'primary'
  if (status === 'cancelled') return 'default'
  return 'info'
}

function stopPolling() {
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer)
    pollTimer = null
  }
}

function schedulePolling() {
  stopPolling()
  if (!selectedJob.value || !activeJobStatuses.includes(String(selectedJob.value.status || ''))) return
  pollTimer = window.setTimeout(() => {
    void refreshSelectedJob(true)
  }, 1800)
}

const jobColumns = computed<DataTableColumn[]>(() => [
  {
    title: '任务',
    key: 'id',
    width: 90,
    render(row: any) {
      return `#${row.id}`
    }
  },
  {
    title: '状态',
    key: 'status',
    width: 100,
    render(row: any) {
      return h(NTag, { type: jobTagType(row.status), size: 'small' }, { default: () => jobStatusLabel(row.status) })
    }
  },
  {
    title: '来源表',
    key: 'source_table',
    width: 150,
    ellipsis: { tooltip: true }
  },
  {
    title: '进度',
    key: 'progress',
    width: 110,
    render(row: any) {
      return `${row.processed_count || 0}/${row.total_count || 0}`
    }
  },
  {
    title: '结果',
    key: 'summary',
    width: 180,
    render(row: any) {
      return `成 ${row.success_count || 0} / 败 ${row.failed_count || 0} / 跳 ${row.skipped_count || 0}`
    }
  },
  {
    title: '创建时间',
    key: 'created_at',
    width: 170,
    render(row: any) {
      return formatDateTime(row.created_at)
    }
  }
])

function jobRowProps(row: any) {
  const isActive = Number(selectedJob.value?.id) === Number(row.id)
  return {
    style: {
      cursor: 'pointer',
      background: isActive ? 'rgba(15, 23, 42, 0.05)' : ''
    },
    onClick: () => {
      void selectJob(row)
    }
  }
}

async function loadTables() {
  try {
    const { data } = await http.get('/data/db/tables')
    const payload = unwrapApiData<any>(data) || {}
    const items = (payload.items || []).filter((item: any) => !String(item.table || '').startsWith('webui_'))
    tableOptions.value = items.map((item: any) => ({
      label: `${item.table} (${item.count}条)`,
      value: item.table
    }))
  } catch {
    tableOptions.value = []
  }
}

async function reloadJobs() {
  offset.value = 0
  jobs.value = []
  await fetchJobs(false)
}

async function loadMore() {
  if (!hasMore.value) return
  loadingMore.value = true
  offset.value += limit
  try {
    await fetchJobs(true)
  } finally {
    loadingMore.value = false
  }
}

async function fetchJobs(append: boolean) {
  if (!append) loadingJobs.value = true
  try {
    const { data } = await http.get('/ai/activity/materialize_jobs', {
      params: {
        source_table: sourceTable.value || undefined,
        status: statusFilter.value || undefined,
        limit,
        offset: offset.value
      }
    })
    const payload = unwrapApiData<any>(data) || {}
    const incoming = payload.jobs || []
    total.value = Number(payload.total || 0)
    jobs.value = append ? jobs.value.concat(incoming) : incoming

    const selectedId = Number(selectedJob.value?.id || 0)
    const matched = jobs.value.find((job: any) => Number(job.id) === selectedId)
    if (matched) {
      selectedJob.value = matched
      await refreshSelectedJob(true)
    } else if (jobs.value.length > 0) {
      await selectJob(jobs.value[0])
    } else {
      selectedJob.value = null
      jobItems.value = []
      stopPolling()
    }
  } catch (error: any) {
    message.error(error?.message || '加载任务列表失败')
  } finally {
    if (!append) loadingJobs.value = false
  }
}

async function selectJob(job: any) {
  selectedJob.value = job
  await refreshSelectedJob(true)
}

async function handleRefreshClick() {
  await refreshSelectedJob(false)
}

async function refreshSelectedJob(silent = false) {
  if (!selectedJob.value?.id) return
  if (!silent) detailLoading.value = true
  try {
    const { data } = await http.get(`/ai/activity/materialize_jobs/${selectedJob.value.id}`, {
      params: { include_items: true }
    })
    const payload = unwrapApiData<any>(data) || {}
    selectedJob.value = payload.job || selectedJob.value
    jobItems.value = payload.items || []
    const index = jobs.value.findIndex((job: any) => Number(job.id) === Number(selectedJob.value.id))
    if (index >= 0) {
      jobs.value[index] = selectedJob.value
    }
    schedulePolling()
  } catch (error: any) {
    stopPolling()
    if (!silent) {
      message.error(error?.message || '加载任务详情失败')
    }
  } finally {
    if (!silent) detailLoading.value = false
  }
}

async function cancelSelectedJob() {
  if (!selectedJob.value?.id) return
  cancelling.value = true
  try {
    const { data } = await http.post(`/ai/activity/materialize_jobs/${selectedJob.value.id}/cancel`)
    const payload = unwrapApiData<any>(data) || {}
    selectedJob.value = payload.job || selectedJob.value
    message.warning(payload.message || '取消请求已提交')
    await refreshSelectedJob(true)
    await reloadJobs()
  } catch (error: any) {
    message.error(error?.message || '取消任务失败')
  } finally {
    cancelling.value = false
  }
}

async function retrySelectedJob() {
  if (!selectedJob.value?.source_table || !selectedJob.value?.requested_ids?.length) return
  retrying.value = true
  try {
    const { data } = await http.post('/ai/activity/materialize_from_db', {
      source_table: selectedJob.value.source_table,
      ids: selectedJob.value.requested_ids,
      target_table: selectedJob.value.target_table || 'activities'
    })
    const payload = unwrapApiData<any>(data) || {}
    const nextJob = payload.job || null
    if (nextJob) {
      message.success(`已重试并创建任务 #${nextJob.id}`)
      selectedJob.value = nextJob
      await reloadJobs()
    }
  } catch (error: any) {
    message.error(error?.message || '重试任务失败')
  } finally {
    retrying.value = false
  }
}

onMounted(async () => {
  await loadTables()
  await reloadJobs()
})

onBeforeUnmount(() => {
  stopPolling()
})
</script>

<style scoped>
.task-center-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.hero-card {
  border: 1px solid rgba(148, 163, 184, 0.24);
  background:
    radial-gradient(circle at top left, rgba(96, 165, 250, 0.15), transparent 38%),
    radial-gradient(circle at right center, rgba(59, 130, 246, 0.12), transparent 30%),
    linear-gradient(135deg, rgba(255, 255, 255, 0.98), rgba(248, 250, 252, 0.95));
}

.hero-eyebrow {
  font-size: 11px;
  letter-spacing: 0.24em;
  color: #3b82f6;
  margin-bottom: 8px;
}

.hero-title {
  font-size: 28px;
  font-weight: 700;
  color: #0f172a;
  margin-bottom: 8px;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
}

.metric-card {
  border: 1px solid rgba(148, 163, 184, 0.18);
}

.metric-label {
  font-size: 12px;
  color: #64748b;
  margin-bottom: 8px;
}

.metric-value {
  font-size: 28px;
  line-height: 1;
  font-weight: 700;
  color: #0f172a;
}

.task-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.25fr) minmax(360px, 0.95fr);
  gap: 16px;
  align-items: start;
}

.footer-actions {
  display: flex;
  justify-content: center;
  margin-top: 16px;
}

.detail-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin-top: 16px;
}

.detail-item {
  padding: 12px 14px;
  border-radius: 12px;
  background: #f8fafc;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.detail-label {
  font-size: 12px;
  color: #64748b;
}

.detail-value {
  font-size: 14px;
  color: #0f172a;
  word-break: break-word;
}

.failure-item,
.trace-item {
  padding: 12px 14px;
  border-radius: 12px;
  border: 1px solid rgba(226, 232, 240, 0.95);
  background: rgba(248, 250, 252, 0.95);
}

.failure-head,
.trace-main {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.failure-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 8px;
  font-size: 12px;
  color: #64748b;
}

.failure-reason {
  margin-top: 8px;
  color: #991b1b;
  line-height: 1.6;
  word-break: break-word;
}

@media (max-width: 1200px) {
  .task-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 900px) {
  .metric-grid,
  .detail-grid {
    grid-template-columns: 1fr;
  }
}
</style>
