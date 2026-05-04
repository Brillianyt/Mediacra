# AI 配置与运行时收口执行计划

## 文档目的

这份文档用于固定当前 AI 配置整改方案，并作为后续持续推进的执行清单。

当前不再采用“前端兼容旧键、后端局部兼容”的思路，而是以“最小前端输入 + 后端统一解析 + 统一运行时调用”为主线收口。

## 总目标

- 前端 AI 配置页只收集最小必要字段
- 后端作为唯一配置真源，统一推断 provider 与默认 model
- 所有文字与图片调用统一走 `structured_runtime_service`
- 配置保存后可被真实任务链路直接使用
- 页面测试结果、接口返回值、真实任务执行结果保持一致

## 设计原则

- 最小输入：前端只提交 `URL + API Key`
- 单一真源：配置解析统一放在 `api/services/config_service.py`
- 单一运行时：模型调用统一放在 `api/services/structured_runtime_service.py`
- 显式诊断：失败时明确区分配置、网络、鉴权、模型能力、解析失败
- 渐进收口：旧函数可暂时保留名称，但内部必须转发到新运行时

## 分阶段计划

### 阶段 1：配置面收口

目标：

- 前端保存时只提交新配置键，不再发送任何旧键

执行项：

- AI 配置页仅提交 `TEXT_AI_*` 与 `IMAGE_UNDERSTANDING_*`
- 删除前端旧键回退分支
- 保证保存后配置接口可正确落盘并热重载

验收：

- 保存请求体中不再出现 `AI_PROVIDER`、`AI_MODEL`、`DEEPSEEK_*`、`OPENAI_*`、`ANTHROPIC_*`

当前状态：

- 已完成

### 阶段 2：后端调用统一

目标：

- 清理所有仍直接读取旧环境变量的文字/图片调用入口

执行项：

- 审计 `api/routers/ai.py`
- 审计 `api/services/structured_analysis_service.py`
- 审计后续 worker 与结构化任务路径
- 用 `resolve_text_ai_settings()` / `resolve_image_ai_settings()` 取代零散 `os.getenv(...)`
- 让旧函数内部统一转发到 `call_llm_for_text()` / `call_llm_multimodal()`

验收：

- 核心链路中不再存在直接依赖 `AI_PROVIDER`、`AI_MODEL`、`OPENAI_API_KEY`、`DEEPSEEK_API_KEY`、`ANTHROPIC_API_KEY` 的调用分叉

当前状态：

- 执行中
- 已完成 `api/routers/ai.py` 主调用入口收口
- 已完成 `api/services/structured_analysis_service.py` 的 provider 展示字段收口
- 仍需继续审计其余结构化任务与 worker 路径

### 阶段 3：默认推断与策略固化

目标：

- 用户只填写 `URL + API Key` 即可完成文字与图片模型调用

执行项：

- 基于 `base_url` 推断 provider
- 基于 provider 推断默认 model
- 明确文字与图片模型的默认策略
- 为不支持视觉的模型返回明确错误

验收：

- 不手填 provider/model 时，主流供应商配置也可完成测试与真实调用

当前状态：

- 部分完成
- 配置解析层已具备推断能力
- 仍需补齐链路级验证

### 阶段 4：保存即验证

目标：

- 配置保存后能立即得到“是否可用”的结论

执行项：

- 保存后支持文字轻量探针
- 保存后支持图片轻量探针
- 统一错误分类与界面提示
- 返回诊断字段，如 `provider`、`model`、`vision_supported`、`image_understanding_error`

验收：

- 用户无需猜测失败点，界面可直接说明是配置错误、鉴权失败、网络错误还是模型不支持

当前状态：

- 部分完成
- 已有测试接口
- AI 配置页已支持保存后自动验证

### 阶段 5：真实任务链路回归

目标：

- 确保测试接口和真实结构化任务使用同一套配置链路

执行项：

- 回归 `/config/test`
- 回归 `/ai/activity/extract`
- 回归 `/ai/activity/quality`
- 回归 `/ai/activity/analyze`
- 回归后台批量 AI 任务和结构化任务中心

验收：

- 同一配置在测试接口、手动分析、后台任务中表现一致

当前状态：

- 待执行

### 阶段 6：治理与收尾

目标：

- 将当前整改结果沉淀为长期可维护方案

执行项：

- 清理废弃旧逻辑
- 补最小回归测试
- 补开发文档与排障说明

验收：

- 后续再改 AI 配置时，不会再次出现“前端存新键、后端读旧键”的错位

当前状态：

- 待执行

## 当前执行进展

### 已完成

- 新增独立 `AI API 配置` 页面并完成前端构建
- 前端保存逻辑已删除旧键回退，只提交新配置键
- 配置保存时不再镜像写回旧键到 `.env`
- `api/routers/ai.py` 中基础文字/图片调用入口已统一转发到运行时服务
- `api/routers/ai.py` 中批量 AI 打分已改为优先走统一文字运行时
- `api/services/structured_analysis_service.py` 中 provider 展示字段已改为统一配置解析结果
- AI 配置页已支持保存成功后自动执行文字/图片探针验证

### 正在推进

- 继续审计剩余结构化任务、worker、诊断返回中的旧环境变量读取
- 清理“展示层用旧 provider，调用层用新配置”的残余错位

### 下一步

1. 完成阶段 2 剩余入口审计
2. 输出“已收口入口清单”和“剩余风险清单”
3. 开始阶段 4 的保存后探针验证设计

## 里程碑

- M1：前端只发新键并可成功保存
- M2：后端核心调用入口统一到运行时服务
- M3：`URL + API Key` 可驱动文字与图片真实调用
- M4：保存后自动验证并给出可诊断错误
- M5：真实结构化任务全链路回归通过
