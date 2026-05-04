<template>
  <div class="ai-config-page">
    <n-space vertical :size="16">
      <n-alert type="warning" :bordered="false">
        这里只保留两类配置：文字 API 和图片 API。可按需填写 URL、模型名与 API Key。阿里 OCR 建议图片模型填写 `qwen-vl-ocr-latest`。
      </n-alert>

      <n-spin :show="loading">
        <div class="config-grid">
          <n-card title="文字 API" size="small">
            <n-form label-placement="top">
              <n-form-item label="URL">
                <n-input
                  v-model:value="form.textUrl"
                  placeholder="例如：https://api.deepseek.com/chat/completions"
                />
              </n-form-item>
              <n-form-item label="模型">
                <n-input
                  v-model:value="form.textModel"
                  placeholder="例如：deepseek-chat / qwen-plus"
                />
              </n-form-item>
              <n-form-item label="API Key">
                <n-input
                  v-model:value="form.textApiKey"
                  type="password"
                  show-password-on="click"
                  placeholder="输入文字 API Key"
                />
              </n-form-item>
            </n-form>
            <n-space>
              <n-button type="primary" :loading="savingText" @click="saveTextConfig">
                保存
              </n-button>
              <n-button :loading="testingText" @click="testTextConfig">
                测试
              </n-button>
            </n-space>
          </n-card>

          <n-card title="图片 API" size="small">
            <n-form label-placement="top">
              <n-form-item label="URL">
                <n-input
                  v-model:value="form.imageUrl"
                  placeholder="例如：https://api.openai.com/v1/chat/completions"
                />
              </n-form-item>
              <n-form-item label="模型">
                <n-input
                  v-model:value="form.imageModel"
                  placeholder="例如：qwen-vl-ocr-latest / qwen-vl-plus"
                />
              </n-form-item>
              <n-form-item label="API Key">
                <n-input
                  v-model:value="form.imageApiKey"
                  type="password"
                  show-password-on="click"
                  placeholder="输入图片 API Key"
                />
              </n-form-item>
            </n-form>
            <n-space>
              <n-button type="primary" :loading="savingImage" @click="saveImageConfig">
                保存
              </n-button>
              <n-button :loading="testingImage" @click="testImageConfig">
                测试
              </n-button>
            </n-space>
          </n-card>
        </div>

        <n-card title="测试结果" size="small">
          <n-empty v-if="!testResult" description="点击测试后在这里显示结果" />
          <template v-else>
            <n-alert
              :type="testResult.success ? 'success' : 'warning'"
              :bordered="false"
              class="result-alert"
            >
              {{ testResult.success ? (testResult.message || '测试成功') : (testResult.error || '测试失败') }}
            </n-alert>

            <div v-if="lastTestType === 'ai_image'" class="result-summary">
              <div class="summary-row">
                <span class="summary-label">Provider</span>
                <span class="summary-value">{{ testResult.provider || '-' }}</span>
              </div>
              <div class="summary-row">
                <span class="summary-label">Model</span>
                <span class="summary-value">{{ testResult.model || '-' }}</span>
              </div>
              <div class="summary-row">
                <span class="summary-label">视觉能力</span>
                <span class="summary-value">{{ boolText(testResult.vision_supported) }}</span>
              </div>
              <div class="summary-row">
                <span class="summary-label">本地图优先命中</span>
                <span class="summary-value">{{ boolText(testResult.used_local_image) }}</span>
              </div>
              <div class="summary-row">
                <span class="summary-label">图片读取成功</span>
                <span class="summary-value">{{ boolText(testResult.image_fetch_succeeded) }}</span>
              </div>
              <div class="summary-row summary-row-full">
                <span class="summary-label">诊断</span>
                <span class="summary-value">{{ imageErrorText(testResult) }}</span>
              </div>
            </div>

            <pre class="test-output">{{ testOutput }}</pre>
          </template>
        </n-card>
      </n-spin>
    </n-space>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useMessage } from 'naive-ui'
import http, { unwrapApiData } from '@/api'

const message = useMessage()
const loading = ref(false)
const savingText = ref(false)
const savingImage = ref(false)
const testingText = ref(false)
const testingImage = ref(false)
const testOutput = ref('')
const testResult = ref<any | null>(null)
const lastTestType = ref<'ai_text' | 'ai_image' | ''>('')

const form = ref({
  textUrl: '',
  textModel: '',
  textApiKey: '',
  imageUrl: '',
  imageModel: '',
  imageApiKey: '',
})

function boolText(value: unknown): string {
  return value ? '是' : '否'
}

function imageErrorText(result: any): string {
  return result?.result?.image_understanding_error || result?.error || '无'
}

async function runConfigTest(type: 'ai_text' | 'ai_image') {
  const requestBody = type === 'ai_image'
    ? { type, image_url: 'https://httpbin.org/image/png' }
    : { type }
  const { data } = await http.post('/config/test', requestBody)
  const result = unwrapApiData<any>(data) || {}
  lastTestType.value = type
  testResult.value = result
  testOutput.value = JSON.stringify(result, null, 2)
  return result
}

async function loadConfig() {
  loading.value = true
  try {
    const { data } = await http.get('/config/groups', { params: { keys: 'ai_text,ai_image' } })
    const payload = unwrapApiData<any>(data) || {}
    const groups = payload.groups || []
    const textGroup = groups.find((group: any) => group.key === 'ai_text')
    const imageGroup = groups.find((group: any) => group.key === 'ai_image')
    const readValue = (group: any, key: string) =>
      group?.fields?.find((field: any) => field.key === key)?.value || ''

    form.value.textUrl = readValue(textGroup, 'TEXT_AI_BASE_URL')
    form.value.textModel = readValue(textGroup, 'TEXT_AI_MODEL')
    form.value.textApiKey = readValue(textGroup, 'TEXT_AI_API_KEY') === '****' ? '****' : readValue(textGroup, 'TEXT_AI_API_KEY')
    form.value.imageUrl = readValue(imageGroup, 'IMAGE_UNDERSTANDING_BASE_URL')
    form.value.imageModel = readValue(imageGroup, 'IMAGE_UNDERSTANDING_MODEL')
    form.value.imageApiKey = readValue(imageGroup, 'IMAGE_UNDERSTANDING_API_KEY') === '****' ? '****' : readValue(imageGroup, 'IMAGE_UNDERSTANDING_API_KEY')
  } catch (error: any) {
    message.error(error.message || '加载 AI 配置失败')
  } finally {
    loading.value = false
  }
}

async function saveTextConfig() {
  savingText.value = true
  try {
    const configs: Record<string, string> = {
      TEXT_AI_BASE_URL: form.value.textUrl.trim(),
      TEXT_AI_MODEL: form.value.textModel.trim(),
    }
    if (form.value.textApiKey !== '****') {
      configs.TEXT_AI_API_KEY = form.value.textApiKey.trim()
    }
    const { data } = await http.put('/config', { configs })
    await loadConfig()
    const result = await runConfigTest('ai_text')
    if (result.success) {
      message.success(`${data.message || '保存成功'}，自动验证通过`)
    } else {
      message.warning(`${data.message || '保存成功'}，但自动验证失败：${result.error || '请检查配置'}`)
    }
  } catch (error: any) {
    message.error(error.message || '保存失败')
  } finally {
    savingText.value = false
  }
}

async function saveImageConfig() {
  savingImage.value = true
  try {
    const configs: Record<string, string> = {
      IMAGE_UNDERSTANDING_BASE_URL: form.value.imageUrl.trim(),
      IMAGE_UNDERSTANDING_MODEL: form.value.imageModel.trim(),
      IMAGE_UNDERSTANDING_ENABLED: 'true',
    }
    if (form.value.imageApiKey !== '****') {
      configs.IMAGE_UNDERSTANDING_API_KEY = form.value.imageApiKey.trim()
    }
    const { data } = await http.put('/config', { configs })
    await loadConfig()
    const result = await runConfigTest('ai_image')
    if (result.success) {
      message.success(`${data.message || '保存成功'}，自动验证通过`)
    } else {
      message.warning(`${data.message || '保存成功'}，但自动验证失败：${result.error || '请检查配置'}`)
    }
  } catch (error: any) {
    message.error(error.message || '保存失败')
  } finally {
    savingImage.value = false
  }
}

async function testTextConfig() {
  testingText.value = true
  try {
    const result = await runConfigTest('ai_text')
    if (result.success) {
      message.success(result.message || '测试成功')
    } else {
      message.warning(result.error || '测试失败')
    }
  } catch (error: any) {
    message.error(error.message || '测试失败')
  } finally {
    testingText.value = false
  }
}

async function testImageConfig() {
  testingImage.value = true
  try {
    const result = await runConfigTest('ai_image')
    if (result.success) {
      message.success(result.message || '测试成功')
    } else {
      message.warning(result.error || '测试失败')
    }
  } catch (error: any) {
    message.error(error.message || '测试失败')
  } finally {
    testingImage.value = false
  }
}

onMounted(loadConfig)
</script>

<style scoped>
.ai-config-page {
  max-width: 960px;
}

.config-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.test-output {
  margin-top: 12px;
  margin: 0;
  max-height: 360px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 12px;
  line-height: 1.6;
}

.result-alert {
  margin-bottom: 12px;
}

.result-summary {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px 16px;
  padding: 12px;
  border-radius: 12px;
  background: #f8fafc;
}

.summary-row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  font-size: 13px;
}

.summary-row-full {
  grid-column: 1 / -1;
}

.summary-label {
  color: #64748b;
}

.summary-value {
  color: #0f172a;
  text-align: right;
  word-break: break-word;
}

@media (max-width: 900px) {
  .config-grid {
    grid-template-columns: 1fr;
  }

  .result-summary {
    grid-template-columns: 1fr;
  }

  .summary-row {
    flex-direction: column;
    gap: 4px;
  }

  .summary-value {
    text-align: left;
  }
}
</style>
