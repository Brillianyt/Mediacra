<template>
  <div class="structured-data-page">
    <n-card title="结构化数据" size="small">
      <template #header-extra>
        <n-space align="center">
          <n-tag size="small" type="success">展示内容: 本地结构化结果表</n-tag>
          <n-select v-model:value="selectedTable" :options="tableOptions" placeholder="选择来源表" style="width: 220px" @update:value="reloadRecords" />
          <n-select v-model:value="qualityMode" :options="qualityModeOptions" placeholder="选择展示范围" style="width: 180px" @update:value="reloadRecords" />
          <n-button type="primary" :loading="generating" @click="generateStructuredRecords">
            生成/刷新结构化
          </n-button>
          <n-button type="primary" secondary :disabled="selectedStructuredIds.length === 0" :loading="syncing" @click="syncSelectedRecords">
            远程数据库同步{{ selectedStructuredIds.length ? ` (${selectedStructuredIds.length})` : '' }}
          </n-button>
          <n-button secondary @click="openTaskCenter">
            打开任务中心
          </n-button>
        </n-space>
      </template>

      <n-space class="mb-4" align="center" wrap>
        <n-input v-model:value="keyword" placeholder="搜索标题/正文关键词..." style="width: 220px" clearable @keyup.enter="reloadRecords" />
        <n-button type="primary" @click="reloadRecords" :loading="loading">查询</n-button>
      </n-space>

      <n-text depth="3" class="mb-3 block">
        当前页面直接读取本地数据库中的结构化结果表。你现在看到的数量，取决于“全部结构化结果”还是“仅结构化质量分大于 60”的筛选，而不是源表里的 `ai_score`。
      </n-text>
      <n-text depth="3" class="mb-3 block">
        当前来源表共有 {{ counts.all }} 条结构化缓存，其中结构化高分 {{ counts.high_quality }} 条；当前模式展示 {{ counts.current }} 条。
      </n-text>
      <div v-if="currentJob" class="mb-4 rounded border border-slate-200 p-3">
        <n-space justify="space-between" align="center" wrap>
          <n-space align="center" wrap>
            <n-text depth="2">当前任务 #{{ currentJob.id }}</n-text>
            <n-tag :type="jobTagType(currentJob.status)" size="small">{{ jobStatusLabel(currentJob.status) }}</n-tag>
            <n-text depth="3">阶段：{{ currentJob.current_stage || 'queued' }}</n-text>
            <n-text depth="3">
              进度：{{ currentJob.processed_count || 0 }}/{{ currentJob.total_count || 0 }}
            </n-text>
            <n-text depth="3">
              成功 {{ currentJob.success_count || 0 }} / 失败 {{ currentJob.failed_count || 0 }} / 跳过 {{ currentJob.skipped_count || 0 }}
            </n-text>
          </n-space>
          <n-space align="center">
            <n-button size="small" quaternary @click="refreshCurrentJob" :loading="jobLoading">刷新任务</n-button>
            <n-button
              v-if="canCancelCurrentJob"
              size="small"
              type="warning"
              secondary
              @click="cancelCurrentJob"
              :loading="cancellingJob"
            >
              取消任务
            </n-button>
          </n-space>
        </n-space>
        <n-text v-if="currentJob.error_message" depth="3" class="mt-2 block">
          任务信息：{{ currentJob.error_message }}
        </n-text>
        <div v-if="currentJobItems.length" class="mt-3">
          <n-text depth="3" class="mb-2 block">最近处理记录</n-text>
          <n-space vertical :size="8">
            <div
              v-for="item in currentJobItems.slice(0, 5)"
              :key="item.id"
              class="flex items-center justify-between gap-3 rounded bg-slate-50 px-3 py-2"
            >
              <n-text style="max-width: 520px" ellipsis>{{ item.source_title || `源记录 #${item.source_record_id}` }}</n-text>
              <n-space align="center" size="small">
                <n-tag :type="jobTagType(item.status)" size="small">{{ jobStatusLabel(item.status) }}</n-tag>
                <n-text depth="3">{{ item.result_payload?.reason || item.error_message || '-' }}</n-text>
              </n-space>
            </div>
          </n-space>
        </div>
      </div>

      <n-spin :show="loading && records.length === 0">
        <n-data-table
          :columns="columns"
          :data="records"
          size="small"
          :row-key="rowKey"
          :checked-row-keys="checkedRowKeys"
          @update:checked-row-keys="handleCheckedRowKeys"
          :max-height="640"
          virtual-scroll
        />
        <div class="mt-4 flex justify-center">
          <n-button v-if="hasMore" @click="loadMore" :loading="loading">加载更多</n-button>
          <n-text v-else-if="records.length > 0" depth="3">没有更多结构化结果了</n-text>
          <n-empty v-else :description="qualityMode === 'high' ? '暂无满足高分条件的结构化数据' : '暂无已落库的结构化数据'" />
        </div>
      </n-spin>
    </n-card>
  </div>
</template>

<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { NButton, NCard, NDataTable, NEmpty, NImage, NInput, NSpace, NSelect, NSpin, NTag, NText, useMessage } from 'naive-ui'
import type { DataTableColumn } from 'naive-ui'
import http, { unwrapApiData } from '@/api'

const message = useMessage()
const router = useRouter()
const loading = ref(false)
const syncing = ref(false)
const generating = ref(false)
const selectedTable = ref('')
const tableOptions = ref<Array<{ label: string; value: string }>>([])
const records = ref<any[]>([])
const hasMore = ref(false)
const keyword = ref('')
const qualityMode = ref<'all' | 'high'>('all')
const offset = ref(0)
const checkedRowKeys = ref<Array<string | number>>([])
const limit = 20
const generateLimit = 20
const counts = ref({ all: 0, high_quality: 0, current: 0 })
const currentJob = ref<any | null>(null)
const currentJobItems = ref<any[]>([])
const jobLoading = ref(false)
const cancellingJob = ref(false)
let pollTimer: number | null = null

const rowKey = (row: any) => row.id
const qualityModeOptions = [
  { label: '全部结构化结果', value: 'all' },
  { label: '仅结构化高分', value: 'high' }
]
const activeJobStatuses = ['pending', 'running', 'cancel_requested']
const canCancelCurrentJob = computed(() => !!currentJob.value && activeJobStatuses.includes(String(currentJob.value.status || '')))

function jobStatusLabel(status: string) {
  const map: Record<string, string> = {
    pending: '待执行',
    running: '执行中',
    success: '成功',
    partial_success: '部分成功',
    failed: '失败',
    cancelled: '已取消',
    cancel_requested: '取消中'
  }
  return map[status] || status || '未知'
}

function jobTagType(status: string): 'default' | 'primary' | 'info' | 'success' | 'warning' | 'error' {
  if (status === 'success') return 'success'
  if (status === 'partial_success') return 'warning'
  if (status === 'failed') return 'error'
  if (status === 'cancelled') return 'default'
  if (status === 'cancel_requested') return 'warning'
  if (status === 'running') return 'primary'
  return 'info'
}

function stopJobPolling() {
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer)
    pollTimer = null
  }
}

function scheduleJobPolling() {
  stopJobPolling()
  if (!currentJob.value || !activeJobStatuses.includes(String(currentJob.value.status || ''))) return
  pollTimer = window.setTimeout(() => {
    void refreshCurrentJobInternal(true)
  }, 1800)
}

const columns = computed<DataTableColumn[]>(() => [
  { type: 'selection' },
  {
    title: '活动标题',
    key: 'title',
    width: 220,
    ellipsis: { tooltip: true },
    render(row: any) {
      return row.title || '-'
    }
  },
  {
    title: '链接',
    key: 'link',
    width: 220,
    ellipsis: { tooltip: true },
    render(row: any) {
      const url = row.link || ''
      if (!url) return '-'
      const text = url.length > 32 ? `${url.slice(0, 32)}...` : url
      return h('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }, text)
    }
  },
  {
    title: '活动简介',
    key: 'description',
    width: 260,
    ellipsis: { tooltip: true },
    render(row: any) {
      return row.description || '-'
    }
  },
  {
    title: '核心价值',
    key: 'core_value',
    width: 260,
    ellipsis: { tooltip: true },
    render(row: any) {
      return row.core_value || '-'
    }
  },
  {
    title: '开始时间',
    key: 'start_time',
    width: 140,
    render(row: any) {
      return row.start_time || '-'
    }
  },
  {
    title: '结束时间',
    key: 'end_time',
    width: 140,
    render(row: any) {
      return row.end_time || '-'
    }
  },
  {
    title: '城市',
    key: 'city',
    width: 96,
    render(row: any) {
      return row.city || '-'
    }
  },
  {
    title: '地址',
    key: 'address',
    width: 220,
    ellipsis: { tooltip: true },
    render(row: any) {
      return row.address || '-'
    }
  },
  {
    title: '主办方',
    key: 'host',
    width: 160,
    ellipsis: { tooltip: true },
    render(row: any) {
      return row.host || '-'
    }
  },
  {
    title: '图片',
    key: 'image',
    width: 140,
    render(row: any) {
      const src = row.image || ''
      if (!src) return '-'
      return h(NImage, {
        src,
        width: 80,
        height: 80,
        objectFit: 'cover'
      })
    }
  }
])

const selectedStructuredIds = computed<number[]>(() => {
  return checkedRowKeys.value
    .map((key) => Number(key))
    .filter((value) => Number.isFinite(value))
})

function handleCheckedRowKeys(keys: Array<string | number>) {
  checkedRowKeys.value = keys
}

function openTaskCenter() {
  router.push({ name: 'TaskCenter' })
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
    if (!selectedTable.value && tableOptions.value.length > 0) {
      selectedTable.value = items.find((item: any) => Number(item.count || 0) > 0)?.table || tableOptions.value[0].value
    }
  } catch {
    tableOptions.value = []
  }
}

async function fetchLatestJob() {
  if (!selectedTable.value) {
    currentJob.value = null
    stopJobPolling()
    return
  }
  jobLoading.value = true
  try {
    const { data } = await http.get('/ai/activity/materialize_jobs', {
      params: {
        source_table: selectedTable.value,
        limit: 1,
        offset: 0
      }
    })
    const payload = unwrapApiData<any>(data) || {}
    const jobs = payload.jobs || []
    currentJob.value = jobs[0] || null
    if (!currentJob.value) {
      currentJobItems.value = []
      stopJobPolling()
      return
    }
    await refreshCurrentJobInternal(true)
  } finally {
    jobLoading.value = false
  }
}

async function refreshCurrentJobInternal(silent: boolean) {
  if (!currentJob.value?.id) {
    if (!silent) {
      await fetchLatestJob()
    }
    return
  }
  if (!silent) jobLoading.value = true
  try {
    const { data } = await http.get(`/ai/activity/materialize_jobs/${currentJob.value.id}`, {
      params: { include_items: true }
    })
    const payload = unwrapApiData<any>(data) || {}
    const nextJob = payload.job || null
    const prevStatus = String(currentJob.value?.status || '')
    currentJob.value = nextJob
    currentJobItems.value = payload.items || []
    scheduleJobPolling()
    const nextStatus = String(nextJob?.status || '')
    const finalized = ['success', 'partial_success', 'failed', 'cancelled']
    if (finalized.includes(nextStatus) && nextStatus !== prevStatus) {
      if (nextStatus === 'success') {
        message.success('结构化任务已完成')
      } else if (nextStatus === 'partial_success') {
        message.warning('结构化任务已完成，但存在失败或跳过项')
      } else if (nextStatus === 'failed') {
        message.error(nextJob?.error_message || '结构化任务执行失败')
      } else if (nextStatus === 'cancelled') {
        message.warning('结构化任务已取消')
      }
      await reloadRecords()
    }
  } catch (error: any) {
    stopJobPolling()
    currentJobItems.value = []
    if (!silent) {
      message.error(error?.message || '获取任务状态失败')
    }
  } finally {
    if (!silent) jobLoading.value = false
  }
}

async function refreshCurrentJob(_e?: MouseEvent) {
  await refreshCurrentJobInternal(false)
}

async function cancelCurrentJob() {
  if (!currentJob.value?.id) return
  cancellingJob.value = true
  try {
    const { data } = await http.post(`/ai/activity/materialize_jobs/${currentJob.value.id}/cancel`)
    const payload = unwrapApiData<any>(data) || {}
    currentJob.value = payload.job || currentJob.value
    message.warning(payload.message || '取消请求已提交')
    scheduleJobPolling()
  } catch (error: any) {
    message.error(error?.message || '取消任务失败')
  } finally {
    cancellingJob.value = false
  }
}

async function reloadRecords() {
  records.value = []
  offset.value = 0
  hasMore.value = false
  checkedRowKeys.value = []
  await fetchLatestJob()
  await fetchRecords()
}

async function loadMore() {
  if (!hasMore.value) return
  offset.value += limit
  await fetchRecords()
}

async function fetchRecords() {
  if (!selectedTable.value) return
  loading.value = true
  try {
    const params: any = {
      source_table: selectedTable.value,
      limit,
      offset: offset.value,
      high_quality_only: qualityMode.value === 'high'
    }
    if (keyword.value) params.keyword = keyword.value
    const { data } = await http.get('/ai/activity/structured_records', { params })
    const payload = unwrapApiData<any>(data) || {}
    const incoming = payload.items || payload.records || []
    records.value.push(...incoming)
    hasMore.value = payload.has_more || false
    counts.value = payload.counts || { all: 0, high_quality: 0, current: incoming.length }
  } finally {
    loading.value = false
  }
}

async function generateStructuredRecords() {
  if (!selectedTable.value) return
  generating.value = true
  try {
    const params: any = {
      table: selectedTable.value,
      limit: generateLimit,
      offset: 0,
      structured_only: true
    }
    if (keyword.value) params.keyword = keyword.value
    const sourceResp = await http.get('/data/db/records', { params })
    const sourcePayload = unwrapApiData<any>(sourceResp.data) || {}
    const sourceRows = sourcePayload.records || []
    const ids = sourceRows.map((row: any) => Number(row.id)).filter((id: number) => Number.isFinite(id))
    if (ids.length === 0) {
      message.info('当前筛选条件下没有可生成的候选数据')
      return
    }

    const { data } = await http.post('/ai/activity/materialize_from_db', {
      source_table: selectedTable.value,
      ids
    })
    const payload = unwrapApiData<any>(data) || {}
    currentJob.value = payload.job || null
    message.success(`已创建结构化任务 #${currentJob.value?.id ?? ''}`)
    scheduleJobPolling()
    await refreshCurrentJobInternal(true)
  } catch (error: any) {
    message.error(error?.message || '生成结构化结果失败')
  } finally {
    generating.value = false
  }
}

async function syncSelectedRecords() {
  if (selectedStructuredIds.value.length === 0) return
  syncing.value = true
  try {
    const { data } = await http.post('/ai/activity/sync_structured_results', {
      ids: selectedStructuredIds.value
    })
    const payload = unwrapApiData<any>(data) || {}
    const inserted = Number(payload.inserted || 0)
    message.success(`远程同步完成：写入 ${inserted} 条`)
  } catch (error: any) {
    message.error(error?.message || '远程同步失败')
  } finally {
    syncing.value = false
  }
}

onMounted(async () => {
  await loadTables()
  await reloadRecords()
})

onBeforeUnmount(() => {
  stopJobPolling()
})
</script>
