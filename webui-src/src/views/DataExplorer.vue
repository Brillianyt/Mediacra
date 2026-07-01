<template>
  <div class="data-explorer">
    <n-card v-if="!initReady" title="数据浏览" size="small">
      <n-spin :show="true">
        <div class="h-24"></div>
      </n-spin>
    </n-card>

    <!-- Non-DB Mode (File Mode) -->
    <template v-else-if="!isDbMode">
      <n-card title="数据浏览 (文件模式)" size="small">
        <template #header-extra>
          <n-space>
            <n-tag size="small" type="default">存储: {{ saveMode || '-' }}</n-tag>
            <n-select v-model:value="platform" :options="platformOptions" placeholder="选择平台" style="width: 140px" @update:value="loadFiles" />
            <n-select v-model:value="fileType" :options="typeOptions" placeholder="文件类型" style="width: 120px" @update:value="loadFiles" />
            <n-button @click="loadFiles" size="small">刷新</n-button>
          </n-space>
        </template>
        <n-spin :show="loading">
          <n-empty v-if="!loading && !dataFiles.length" description="暂无数据，请先运行爬虫采集" />
          <n-data-table v-else :columns="fileColumns" :data="dataFiles" size="small" :row-key="(r: any) => r.path" />
        </n-spin>
      </n-card>
      <n-modal v-model:show="showPreview" :title="'预览: ' + previewFile" preset="card" style="width: 80vw; max-width: 900px">
        <n-spin :show="previewing">
          <div class="max-h-[60vh] overflow-auto">
            <n-data-table v-if="previewRows.length" :columns="previewColumns" :data="previewRows" size="small" :max-height="400" virtual-scroll />
            <pre v-else class="text-xs">{{ previewRaw }}</pre>
          </div>
        </n-spin>
      </n-modal>
    </template>

    <!-- DB Mode (AI Data Workbench) -->
    <template v-else>
      <n-card title="AI 智能数据看板" size="small" class="mb-4">
        <template #header-extra>
          <n-space align="center">
            <n-tag size="small" type="success">存储: {{ saveMode }}</n-tag>
            <n-select v-model:value="selectedTable" :options="tableOptions" placeholder="选择数据表" style="width: 180px" @update:value="onTableChange" />
          </n-space>
        </template>

        <!-- Advanced Filters -->
        <n-space class="mb-4" align="center" wrap>
          <n-input v-model:value="filters.keyword" placeholder="搜索关键词..." style="width: 180px" clearable @keyup.enter="reloadRecords" />
          <n-select v-model:value="filters.ai_status" :options="aiStatusOptions" placeholder="AI 状态" style="width: 120px" clearable @update:value="reloadRecords" />
          <n-select v-model:value="filters.is_spam" :options="spamOptions" placeholder="是否垃圾数据" style="width: 140px" clearable @update:value="reloadRecords" />
          <n-select v-model:value="filters.manual_review_status" :options="manualReviewOptions" placeholder="人工审核" style="width: 140px" clearable @update:value="reloadRecords" />
          <n-input-number v-model:value="filters.min_ai_score" placeholder="最低分" style="width: 100px" clearable @keyup.enter="reloadRecords" />
          <n-button type="primary" @click="reloadRecords" :loading="loading">查询</n-button>
        </n-space>

        <n-space class="mb-2" align="center" wrap>
          <n-select v-model:value="selectedPromptId" :options="promptOptions" placeholder="选择提示词版本" style="width: 220px" @update:value="onPresetSelect" />
          <n-input v-model:value="promptName" placeholder="版本名称" style="width: 200px" />
          <n-button @click="saveNewVersion" type="primary">保存为新版本</n-button>
          <n-button @click="updateCurrentVersion" type="default" :disabled="!selectedPromptId">更新当前版本</n-button>
          <n-button @click="deleteCurrentVersion" type="error" :disabled="!selectedPromptId">删除当前版本</n-button>
        </n-space>

        <n-space class="mb-2" vertical>
          <n-input v-model:value="customPrompt" type="textarea" placeholder="自定义提示词（可选）" :autosize="{ minRows: 3, maxRows: 6 }" />
        </n-space>

        <!-- AI Actions -->
        <n-space class="mb-4">
          <n-button type="info" @click="processAIBatch" :disabled="!checkedRowKeys.length" :loading="aiProcessing">
            ✨ AI 智能清洗/打标 ({{ checkedRowKeys.length }})
          </n-button>
          <n-button type="error" @click="deleteBatch" :disabled="!checkedRowKeys.length">
            🗑️ 批量删除 ({{ checkedRowKeys.length }})
          </n-button>
          <n-button @click="markAsSpamBatch" :disabled="!checkedRowKeys.length">
            标记为垃圾数据
          </n-button>
        </n-space>

        <!-- Data Table with Virtual Scroll -->
        <n-spin :show="loading && records.length === 0">
          <n-data-table
            :columns="recordColumns"
            :data="records"
            size="small"
            :row-key="rowKey"
            v-model:checked-row-keys="checkedRowKeys"
            :max-height="600"
            virtual-scroll
          />
          <div class="mt-4 flex justify-center">
            <n-button v-if="hasMore" @click="loadMore" :loading="loading">加载更多 (懒加载)</n-button>
            <n-text v-else-if="records.length > 0" depth="3">没有更多数据了</n-text>
            <n-empty v-else description="暂无匹配数据" />
          </div>
        </n-spin>
      </n-card>
      <n-modal v-model:show="showImageModal" preset="card" :title="imageModalTitle" style="width: 92vw; max-width: 1200px">
        <n-spin :show="imageLoading">
          <div class="image-preview-grid">
            <n-image-group>
              <n-image
                v-for="src in imageList"
                :key="src"
                :src="src"
                class="image-preview-item"
                :img-props="{ class: 'image-preview-inner' }"
                preview-disabled
              />
            </n-image-group>
          </div>
          <n-empty v-if="!imageLoading && imageList.length === 0" description="暂无本地图片" />
        </n-spin>
      </n-modal>
      <n-modal v-model:show="showTagEdit" preset="card" title="编辑 AI 标签" style="width: 520px">
        <n-space vertical :size="12">
          <n-text depth="3">用中文逗号或英文逗号分隔多个标签</n-text>
          <n-input v-model:value="tagEditText" type="textarea" :autosize="{ minRows: 3, maxRows: 6 }" placeholder="例如：黑客松, 活动预告, 开发者社区" />
          <n-space justify="end">
            <n-button @click="showTagEdit = false">取消</n-button>
            <n-button type="primary" @click="saveTagEdit">保存</n-button>
          </n-space>
        </n-space>
      </n-modal>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, h, onMounted, computed, watch } from 'vue'
import { useRoute } from 'vue-router'
import { NButton, NTag, NSpace, useMessage, NPopconfirm, NInput, NSelect, NInputNumber, NText, NEmpty, NDataTable, NCard, NModal, NSpin, NImage, NImageGroup } from 'naive-ui'
import type { DataTableColumn } from 'naive-ui'
import http, { unwrapApiData } from '@/api'

const message = useMessage()
const route = useRoute()
const loading = ref(false)
const aiProcessing = ref(false)
const initReady = ref(false)

// Common
const saveMode = ref('')
const isDbMode = computed(() => ['sqlite', 'db', 'postgres'].includes((saveMode.value || '').toLowerCase()))

// --- File Mode State ---
const platform = ref('')
const fileType = ref('')
const dataFiles = ref<any[]>([])
const showPreview = ref(false)
const previewing = ref(false)
const previewFile = ref('')
const previewType = ref('json')
const previewRaw = ref('')
const previewRows = ref<any[]>([])
const previewColumns = ref<DataTableColumn[]>([])

const platformOptions = [
  { label: '全部', value: '' },
  { label: '小红书', value: 'xhs' },
  { label: '抖音', value: 'dy' },
  { label: '微信', value: 'wechat' },
  { label: 'B站', value: 'bili' },
]
const typeOptions = [
  { label: '全部', value: '' },
  { label: 'JSON', value: 'json' },
  { label: 'CSV', value: 'csv' },
]

// --- DB Mode State ---
const selectedTable = ref('')
const tableOptions = ref<any[]>([])
const records = ref<any[]>([])
const checkedRowKeys = ref<Array<string | number>>([])
const hasMore = ref(false)
const offset = ref(0)
const limit = 50
const customPrompt = ref('')
const showImageModal = ref(false)
const imageModalTitle = ref('')
const imageLoading = ref(false)
const imageList = ref<string[]>([])
type PromptPreset = { id: number; name: string; content: string }
const promptPresets = ref<PromptPreset[]>([])
const selectedPromptId = ref<number | null>(null)
const promptName = ref('')
const promptOptions = computed(() => promptPresets.value.map(p => ({ label: p.name, value: p.id })))
const LS_KEY = 'mc_prompt_presets'
type MappingItem = { source_field: string; display_name: string; enabled: boolean; sort_order: number; display_type?: string }
const mappingItems = ref<MappingItem[]>([])
const mappingSchemes = ref<any[]>([])
const selectedMappingSchemeId = ref<number | null>(null)
function _inferPlatformAndType(table: string): { platform: string; data_type: string } | null {
  if (table === 'wechat_article') return { platform: 'wechat', data_type: 'article' }
  if (table === 'xhs_note') return { platform: 'xhs', data_type: 'note' }
  if (table === 'douyin_aweme') return { platform: 'dy', data_type: 'note' }
  if (table === 'bilibili_video') return { platform: 'bili', data_type: 'video' }
  if (table === 'weibo_note') return { platform: 'wb', data_type: 'note' }
  if (table === 'tieba_note') return { platform: 'tieba', data_type: 'note' }
  return null
}
async function loadMappingSchemes() {
  mappingItems.value = []
  mappingSchemes.value = []
  selectedMappingSchemeId.value = null
  const p = _inferPlatformAndType(selectedTable.value)
  if (!p) return
  try {
    const { data } = await http.get('/mapping/schemes', { params: { platform: p.platform, data_type: p.data_type, page: 1, size: 20 } })
    const payload = unwrapApiData<any>(data) || {}
    mappingSchemes.value = payload.items || []
    const def = mappingSchemes.value.find((x: any) => x.is_default) || mappingSchemes.value[0]
    if (def) {
      selectedMappingSchemeId.value = def.id
      await loadMappingSchemeDetail(def.id)
    } else {
      if (p.platform === 'wechat' && p.data_type === 'article') {
        mappingItems.value = [
          { source_field: 'article_id', display_name: '文章ID', enabled: true, sort_order: 1, display_type: 'text' },
          { source_field: 'title', display_name: '标题', enabled: true, sort_order: 2, display_type: 'text' },
          { source_field: 'digest', display_name: '内容摘要', enabled: true, sort_order: 3, display_type: 'text' },
          { source_field: 'account_nickname', display_name: '公众号名称', enabled: true, sort_order: 4, display_type: 'text' },
          { source_field: 'author_name', display_name: '作者', enabled: true, sort_order: 5, display_type: 'text' },
          { source_field: 'item_show_type', display_name: '类型', enabled: true, sort_order: 6, display_type: 'single_select' },
          { source_field: 'create_time_str', display_name: '发布时间', enabled: true, sort_order: 7, display_type: 'text' },
          { source_field: 'link', display_name: '文章链接', enabled: true, sort_order: 8, display_type: 'url' },
          { source_field: 'image_list', display_name: '图片', enabled: true, sort_order: 9, display_type: 'attachment' },
          { source_field: 'fakeid', display_name: '公众号ID', enabled: false, sort_order: 10, display_type: 'text' },
          { source_field: 'source_keyword', display_name: '来源关键词', enabled: false, sort_order: 11, display_type: 'text' },
        ]
      }
    }
  } catch {}
}
async function loadMappingSchemeDetail(id: number) {
  try {
    const { data } = await http.get(`/mapping/schemes/${id}`)
    const payload = unwrapApiData<any>(data) || {}
    const items = payload.items || []
    mappingItems.value = items.filter((it: any) => it.enabled).sort((a: any, b: any) => (a.sort_order || 0) - (b.sort_order || 0))
  } catch {
    mappingItems.value = []
  }
}
function loadPresets() {
  try {
    const raw = localStorage.getItem(LS_KEY)
    const arr = raw ? JSON.parse(raw) : []
    if (Array.isArray(arr) && arr.length > 0) {
      promptPresets.value = arr
    } else {
      const defaults: PromptPreset[] = [
        { id: Date.now(), name: '通用质量评估', content: '请对以下内容进行质量评估并返回JSON：仅返回纯JSON，不要包含解释或其他文字。tags 只能从“创业孵化、行业大厂资源、高校创新竞赛、周末线下沙龙、AI创意活动、AI教学工坊”中选择 1 到 3 个，不允许输出其他标签。字段: score(0-100整数), summary(不超过160字), tags(数组或逗号分隔字符串), is_spam(0或1)。\n' },
        { id: Date.now() + 1, name: '技术文章偏好', content: '从工程角度评估本文并仅返回JSON：字段同上，强调结构、术语、论证严谨度、结论可验证性，模板化表达降分。\n' },
        { id: Date.now() + 2, name: '市场推广识别', content: '识别推广/引流倾向，出现返利、扫码、私聊、加微信、代刷、买粉等行为视为垃圾；仅返回JSON，字段同上。\n' }
      ]
      promptPresets.value = defaults
      localStorage.setItem(LS_KEY, JSON.stringify(defaults))
    }
    const first = promptPresets.value[0]
    selectedPromptId.value = first?.id ?? null
    promptName.value = first?.name ?? ''
    customPrompt.value = first?.content ?? ''
  } catch {
    promptPresets.value = []
    selectedPromptId.value = null
    promptName.value = ''
    customPrompt.value = ''
  }
}
function savePresets() {
  localStorage.setItem(LS_KEY, JSON.stringify(promptPresets.value))
}
function onPresetSelect(val: number | null) {
  selectedPromptId.value = val
  const p = promptPresets.value.find(x => x.id === val)
  promptName.value = p?.name ?? ''
  customPrompt.value = p?.content ?? ''
}
function saveNewVersion() {
  const name = (promptName.value || '').trim()
  const content = (customPrompt.value || '').trim()
  if (!name || !content) return
  const id = Date.now()
  const preset = { id, name, content }
  promptPresets.value.push(preset)
  selectedPromptId.value = id
  savePresets()
}
function updateCurrentVersion() {
  if (!selectedPromptId.value) return
  const name = (promptName.value || '').trim()
  const content = (customPrompt.value || '').trim()
  if (!name || !content) return
  const idx = promptPresets.value.findIndex(x => x.id === selectedPromptId.value)
  if (idx >= 0) {
    promptPresets.value[idx] = { id: selectedPromptId.value, name, content }
    savePresets()
  }
}
function deleteCurrentVersion() {
  if (!selectedPromptId.value) return
  promptPresets.value = promptPresets.value.filter(x => x.id !== selectedPromptId.value)
  savePresets()
  const first = promptPresets.value[0]
  selectedPromptId.value = first?.id ?? null
  promptName.value = first?.name ?? ''
  customPrompt.value = first?.content ?? ''
}

const filters = ref({
  keyword: '',
  ai_status: null as number | null,
  is_spam: null as number | null,
  manual_review_status: null as string | null,
  min_ai_score: null as number | null
})

const aiStatusOptions = [
  { label: '未处理', value: 0 },
  { label: '处理中', value: 1 },
  { label: '已完成', value: 2 },
  { label: '失败', value: -1 }
]

const spamOptions = [
  { label: '正常数据', value: 0 },
  { label: '垃圾/广告', value: 1 }
]

const manualReviewOptions = [
  { label: '待人工标记', value: 'pending' },
  { label: '人工通过', value: 'passed' },
  { label: '人工不通过', value: 'rejected' }
]

const rowKey = (row: any) => row.id

const recordColumns = computed<DataTableColumn[]>(() => {
  const cols: DataTableColumn[] = [
    { type: 'selection' },
    { title: 'ID', key: 'id', width: 60 },
  ]
  if (mappingItems.value.length > 0) {
    const mappedCols: DataTableColumn[] = mappingItems.value.map((it) => {
      const key = it.source_field
      const title = it.display_name || key
      const base: any = { title, key, width: 140, ellipsis: { tooltip: true } }
      const type = (it.display_type || '').toLowerCase()
      if (type === 'url') {
        base.width = 180
        base.render = (row: any) => {
          const url = row[key] || ''
          if (!url) return '-'
          const text = (url.length > 28 ? (url.slice(0, 28) + '...') : url)
          return h('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }, text)
        }
      } else if (type === 'attachment') {
    base.width = selectedTable.value === 'wechat_article' && key === 'image_list' ? 160 : 90
    base.render = (row: any) => {
      const list = (row[key] || '').split(',').filter((x: string) => !!x.trim())
      if (selectedTable.value === 'wechat_article' && key === 'image_list') {
        const count = list.length
        return h(NSpace, { size: 6 }, {
          default: () => [
            h(NTag, { size: 'small', type: 'info' }, { default: () => `${count}` }),
            h(NButton, { size: 'tiny', onClick: () => previewImages(row) }, { default: () => '预览图片' })
          ]
        })
      }
      return String(list.length)
    }
      } else if (type === 'single_select') {
        base.width = 120
        base.render = (row: any) => {
          const val = row[key] || '-'
          return h(NTag, { size: 'small' }, { default: () => val })
        }
      }
      return base
    })
    cols.push(...mappedCols)
  }
  cols.push(
    {
      title: 'AI 状态',
      key: 'ai_status',
      width: 100,
      render(row: any) {
        if (row.ai_status === 2) return h(NTag, { type: 'success', size: 'small' }, { default: () => '已完成' })
        if (row.ai_status === 1) return h(NTag, { type: 'warning', size: 'small' }, { default: () => '处理中' })
        if (row.ai_status === -1) return h(NTag, { type: 'error', size: 'small' }, { default: () => '失败' })
        return h(NTag, { type: 'default', size: 'small' }, { default: () => '未处理' })
      }
    },
    {
      title: '人工审核',
      key: 'manual_review_status',
      width: 110,
      render(row: any) {
        if (row.manual_review_status === 'passed') return h(NTag, { type: 'success', size: 'small' }, { default: () => '人工通过' })
        if (row.manual_review_status === 'rejected') return h(NTag, { type: 'error', size: 'small' }, { default: () => '人工不通过' })
        return h(NTag, { type: 'default', size: 'small' }, { default: () => '待标记' })
      }
    },
    {
      title: '质量分',
      key: 'ai_score',
      width: 80,
      render(row: any) {
        if (row.ai_status !== 2) return '-'
        const type = row.ai_score >= 80 ? 'success' : (row.ai_score >= 50 ? 'warning' : 'error')
        return h(NTag, { type, size: 'small' }, { default: () => row.ai_score })
      }
    },
    {
      title: '性质',
      key: 'is_spam',
      width: 80,
      render(row: any) {
        if (row.is_spam === 1) return h(NTag, { type: 'error', size: 'small' }, { default: () => '垃圾广告' })
        return h(NTag, { type: 'info', size: 'small' }, { default: () => '正常' })
      }
    },
    { title: '标题 / 摘要', key: 'title', width: 250, ellipsis: { tooltip: true }, render(row: any) { return row.title || row.ai_summary || row.desc || '-' } },
    { title: 'AI 标签', key: 'ai_tags', width: 150, ellipsis: { tooltip: true } },
  )
  cols.push({
    title: '操作',
    key: 'actions',
    width: 360,
    render(row: any) {
      return h(NSpace, { size: 'small' }, {
        default: () => [
          h(
            NButton,
            { size: 'tiny', type: 'primary', onClick: () => processAISingle(row.id) },
            { default: () => 'AI打标签' }
          ),
          h(
            NButton,
            { size: 'tiny', type: 'success', ghost: row.manual_review_status !== 'passed', onClick: () => updateManualReview(row, 'passed') },
            { default: () => '人工通过' }
          ),
          h(
            NButton,
            { size: 'tiny', type: 'warning', ghost: row.manual_review_status !== 'rejected', onClick: () => updateManualReview(row, 'rejected') },
            { default: () => '人工不通过' }
          ),
          h(
            NButton,
            { size: 'tiny', onClick: () => openTagEditor(row) },
            { default: () => '编辑标签' }
          ),
          h(
            NPopconfirm,
            { onPositiveClick: () => deleteSingle(row.id) },
            {
              trigger: () => h(NButton, { size: 'tiny', type: 'error', ghost: true }, { default: () => '删除' }),
              default: () => '确认删除此条数据？'
            }
          )
        ]
      })
    }
  })
  return cols
})

async function previewImages(row: any) {
  imageModalTitle.value = (row.title || '图片预览') + ''
  imageList.value = []
  imageLoading.value = true
  showImageModal.value = true
  try {
    const nickname = row.account_nickname || ''
    const article_id = row.article_id || row.id || ''
    const { data } = await http.get('/data/images/wechat', { params: { nickname, article_id } })
    const payload = unwrapApiData<any>(data) || {}
    imageList.value = payload.items || []
  } catch (e: any) {
    imageList.value = []
  } finally {
    imageLoading.value = false
  }
}

function formatSize(bytes: number): string {
  if (!bytes) return '0 B'
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / 1048576).toFixed(1) + ' MB'
}

// --- Init ---
async function init() {
  initReady.value = false
  loading.value = false
  platform.value = ''
  fileType.value = ''
  dataFiles.value = []
  showPreview.value = false
  previewing.value = false
  previewFile.value = ''
  previewType.value = 'json'
  previewRaw.value = ''
  previewRows.value = []
  previewColumns.value = []
  selectedTable.value = ''
  tableOptions.value = []
  records.value = []
  checkedRowKeys.value = []
  hasMore.value = false
  offset.value = 0
  try {
    const { data } = await http.get('/config/validate')
    saveMode.value = (data.data?.save_data_option || '').toLowerCase()
  } catch {
    saveMode.value = ''
  }
  loadPresets()

  try {
    if (isDbMode.value) {
      await loadDbTables()
      await loadMappingSchemes()
    } else {
      await loadFiles()
    }
  } finally {
    initReady.value = true
  }
}

// --- File Mode Methods ---
async function loadFiles() {
  loading.value = true
  try {
    const params: any = {}
    if (platform.value) params.platform = platform.value
    if (fileType.value) params.file_type = fileType.value
    const { data } = await http.get('/data/files', { params })
    const payload = unwrapApiData<any>(data) || {}
    dataFiles.value = payload.files || []
  } catch (e: any) {
    message.error(e.message || '加载失败')
  } finally {
    loading.value = false
  }
}

const fileColumns: DataTableColumn[] = [
  { title: '文件名', key: 'name', ellipsis: { tooltip: true } },
  { title: '平台', key: 'path', width: 80, render: (row: any) => h(NTag, { size: 'small', type: 'info' }, () => (row.path || '').split(/[/\\]/)[0] || '-') },
  { title: '类型', key: 'type', width: 70, render: (row: any) => h(NTag, { size: 'small' }, () => (row.type || '').toUpperCase()) },
  { title: '记录数', key: 'record_count', width: 80, render: (row: any) => row.record_count ?? '-' },
  { title: '大小', key: 'size', width: 90, render: (row: any) => formatSize(row.size) },
  { title: '修改时间', key: 'modified_at', width: 160, render: (row: any) => row.modified_at ? new Date(row.modified_at * 1000).toLocaleString() : '-' },
  {
    title: '操作', key: 'actions', width: 140,
    render: (row: any) => h(NSpace, { size: 'small' }, () => [
      h(NButton, { size: 'tiny', onClick: () => previewFileData(row) }, () => '预览')
    ])
  }
]

async function previewFileData(row: any) {
  previewFile.value = row.name
  previewType.value = row.type || 'json'
  previewRaw.value = ''
  previewRows.value = []
  previewColumns.value = []
  showPreview.value = true
  previewing.value = true
  try {
    const { data } = await http.get(`/data/files/${encodeURIComponent(row.path)}`, { params: { preview: true } })
    const content = unwrapApiData<any>(data)
    if (Array.isArray(content)) {
      previewRows.value = content.slice(0, 100)
      if (content.length > 0) previewColumns.value = Object.keys(content[0]).map(k => ({ title: k, key: k, width: 150, ellipsis: { tooltip: true } }))
    } else {
      previewRaw.value = typeof content === 'string' ? content : JSON.stringify(content, null, 2)
    }
  } catch (e: any) {
    previewRaw.value = '预览失败: ' + (e.message || '未知错误')
  } finally {
    previewing.value = false
  }
}

// --- DB Mode Methods ---
async function loadDbTables() {
  try {
    const { data } = await http.get('/data/db/tables')
    const payload = unwrapApiData<any>(data) || {}
    const items = payload.items || []
    tableOptions.value = items.map((x: any) => ({ label: `${x.table} (${x.count}条)`, value: x.table }))
    if (items.length > 0) {
      const preferredTable = typeof route.query.table === 'string' ? route.query.table : ''
      const firstNonEmpty = items.find((x: any) => Number(x.count || 0) > 0)?.table || items[0].table
      selectedTable.value = items.some((x: any) => x.table === preferredTable) ? preferredTable : firstNonEmpty
      if (typeof route.query.keyword === 'string' && route.query.keyword.trim()) {
        filters.value.keyword = route.query.keyword.trim()
      }
      await reloadRecords()
    }
  } catch (e: any) {
    message.error('加载表失败')
  }
}

function onTableChange() {
  reloadRecords()
  loadMappingSchemes()
}

async function reloadRecords() {
  records.value = []
  offset.value = 0
  hasMore.value = false
  checkedRowKeys.value = []
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
      table: selectedTable.value,
      limit,
      offset: offset.value
    }
    if (filters.value.keyword) params.keyword = filters.value.keyword
    if (filters.value.ai_status !== null) params.ai_status = filters.value.ai_status
    if (filters.value.is_spam !== null) params.is_spam = filters.value.is_spam
    if (filters.value.manual_review_status) params.manual_review_status = filters.value.manual_review_status
    if (filters.value.min_ai_score !== null) params.min_ai_score = filters.value.min_ai_score

    const { data } = await http.get('/data/db/records', { params })
    const payload = unwrapApiData<any>(data) || {}
    
    // Append for lazy loading
    if (payload.records && payload.records.length > 0) {
      records.value.push(...payload.records)
    }
    hasMore.value = payload.has_more || false
  } catch (e: any) {
    message.error(e.message || '加载数据失败')
  } finally {
    loading.value = false
  }
}

async function processAIBatch() {
  if (!checkedRowKeys.value.length || !selectedTable.value) return
  aiProcessing.value = true
  try {
    await http.post('/ai/process_batch', {
      table: selectedTable.value,
      ids: checkedRowKeys.value,
      custom_prompt: customPrompt.value || null
    })
    message.success('AI 清洗任务已提交后台处理')
    checkedRowKeys.value = []
    setTimeout(reloadRecords, 1000) // 延迟刷新以查看状态变更
  } catch (e: any) {
    message.error(e.message || '提交失败')
  } finally {
    aiProcessing.value = false
  }
}

async function processAISingle(id: number) {
  if (!selectedTable.value || !id) return
  aiProcessing.value = true
  try {
    await http.post('/ai/process_batch', {
      table: selectedTable.value,
      ids: [id],
      custom_prompt: customPrompt.value || null
    })
    message.success('已提交 AI 打标签任务')
    setTimeout(reloadRecords, 800)
  } catch (e: any) {
    message.error(e.message || '提交失败')
  } finally {
    aiProcessing.value = false
  }
}

async function markAsSpamBatch() {
  if (!checkedRowKeys.value.length || !selectedTable.value) return
  try {
    for (const id of checkedRowKeys.value) {
      await http.put(`/data/db/records/${selectedTable.value}/${id}`, {
        updates: { is_spam: 1 }
      })
    }
    message.success('已标记为垃圾数据')
    checkedRowKeys.value = []
    reloadRecords()
  } catch (e: any) {
    message.error('标记失败')
  }
}

async function updateManualReview(row: any, status: 'pending' | 'passed' | 'rejected') {
  if (!selectedTable.value || !row?.id) return
  try {
    const { data } = await http.put(`/data/db/records/${selectedTable.value}/${row.id}/manual_review`, {
      manual_review_status: status,
      review_note: row.review_note || ''
    })
    const payload = unwrapApiData<any>(data) || {}
    const idx = records.value.findIndex(r => r.id === row.id)
    if (idx >= 0) {
      records.value[idx].manual_review_status = payload.manual_review_status || status
      records.value[idx].review_note = payload.review_note || ''
      records.value[idx].manual_review_updated_at = payload.manual_review_updated_at || null
    }
    message.success(status === 'passed' ? '已标记为人工通过' : status === 'rejected' ? '已标记为人工不通过' : '已清除人工审核状态')
  } catch (e: any) {
    message.error(e.message || '人工审核状态更新失败')
  }
}

async function deleteSingle(id: number) {
  if (!selectedTable.value) return
  try {
    await http.delete(`/data/db/records/${selectedTable.value}/${id}`)
    message.success('删除成功')
    records.value = records.value.filter(r => r.id !== id)
  } catch (e: any) {
    message.error('删除失败')
  }
}

async function deleteBatch() {
  if (!checkedRowKeys.value.length || !selectedTable.value) return
  try {
    await http.post(`/data/db/records/${selectedTable.value}/batch_delete`, {
      ids: checkedRowKeys.value
    })
    message.success(`成功删除 ${checkedRowKeys.value.length} 条记录`)
    checkedRowKeys.value = []
    reloadRecords()
  } catch (e: any) {
    message.error('删除失败')
  }
}

// --- Tag Editor ---
const showTagEdit = ref(false)
const tagEditRowId = ref<number | null>(null)
const tagEditText = ref('')
function openTagEditor(row: any) {
  tagEditRowId.value = row.id
  tagEditText.value = (row.ai_tags || '').trim()
  showTagEdit.value = true
}
async function saveTagEdit() {
  if (!selectedTable.value || !tagEditRowId.value) return
  try {
    await http.put(`/data/db/records/${selectedTable.value}/${tagEditRowId.value}`, {
      updates: { ai_tags: tagEditText.value }
    })
    message.success('标签已更新')
    showTagEdit.value = false
    // 更新当前行显示
    const idx = records.value.findIndex(r => r.id === tagEditRowId.value)
    if (idx >= 0) records.value[idx].ai_tags = tagEditText.value
  } catch (e: any) {
    message.error('保存失败')
  }
}

onMounted(() => {
  init()
})

watch(
  () => route.fullPath,
  (newPath, oldPath) => {
    if (route.name === 'DataExplorer' && newPath !== oldPath) {
      init()
    }
  }
)
</script>

<style scoped>
.image-preview-grid {
  max-height: 72vh;
  overflow: auto;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  align-items: start;
  gap: 12px;
  padding-right: 4px;
}

:deep(.image-preview-item) {
  width: 100%;
}

:deep(.image-preview-inner) {
  display: block;
  width: 100%;
  height: auto;
  max-height: none;
  object-fit: contain;
  border-radius: 10px;
  background: #f8fafc;
}
</style>
