# MediaCrawler — 多平台数据采集 WebUI 控制台

多平台（小红书 / 抖音 / B站 / 微博 / 快手 / 贴吧 / 知乎 / 微信）数据采集与可视化控制台。

## 本地启动

```bash
# 复制配置
cp .env.example .env

# 启动后端 API
uv run uvicorn api.main:app --host 0.0.0.0 --port 8080

# 浏览器打开 http://localhost:8080
```

Windows 一键启动：`.\start_webui_and_worker.ps1`

## 数据源

远程数据库（MemFire），账号密码：`18365104637` / `CRXcrx20020322`
URL：`https://d5s9nh8g91huch72ficg.baseapi.memfiredb.com`
私钥（勿删）：`eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIiwiZXhwIjozMzQ2MzExODc3LCJpYXQiOjE3Njk1MTE4NzcsImlzcyI6InN1cGFiYXNlIn0.B0B6eL-6JnydHQlCvjZP1eusY3U7RTJhmLk74ZiuV3o`

---

## 启动后配置

### 1. 微信采集

打开 [https://down.mptext.top](https://down.mptext.top)，左下角扫码登录 → 左侧 API 栏目查询密钥，填入 WebUI **配置管理 → 微信采集配置** 的 Auth Key 字段（有效期 4 天，过期需重新获取）。微信源 URL 填 `https://down.mptext.top`。

### 2. AI API

在 WebUI **AI API 配置** 页面填写兼容 OpenAI 风格的接口地址与密钥（DeepSeek / OpenAI 等），用于内容质量评分与筛选。

## 操作流程

完成上述配置后：

1. **订阅管理** → 选择微信平台，搜索公众号名称并添加，可在配置界面锁定爬取时间范围 → 批量爬取
2. **任务中心 / 日志监控 / 仪表盘** → 查看爬取进度（爬取未结束时相关栏目不显示数据，已知小缺陷）
3. **数据浏览** → 查看爬取结果，可使用 AI 一键打分或手动审批通过
4. **结构化数据** → 选择 `wechat_article`，勾选 AI 评分 ≥60 或手动通过的条目，点击**向远程数据库同步**，数据即写入 MemFire 数据库

## 活动数据在网站上的呈现

数据库中已同步的活动若缺少地点或标签而无法正常显示：
- **方式 A**：直接在数据库 `activities` 表的 `city_id` 字段选择城市
- **方式 B**：管理员登录网站活动管理页面直接修改（设置地点、修改封面、打两颗星设为热点活动等）
