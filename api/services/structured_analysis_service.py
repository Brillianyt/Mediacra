"""结构化分析执行单元。

将活动抽取、质量判断、图片理解结果格式化等逻辑从路由层下沉，
便于后续独立 worker 直接复用。
"""

import json
import os
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

from pydantic import BaseModel

from api.services.config_service import config_service


FIXED_ACTIVITY_TAGS = [
    "创业孵化",
    "行业大厂资源",
    "高校创新竞赛",
    "周末线下沙龙",
    "AI创意活动",
    "AI教学工坊",
]


class EventExtractRequest(BaseModel):
    title: Optional[str] = None
    link: Optional[str] = None
    published_at: Optional[str] = None
    content: str
    image_captions: Optional[str] = None
    image_urls: Optional[List[str]] = None
    custom_prompt: Optional[str] = None


class EventQualityRequest(BaseModel):
    title: Optional[str] = None
    link: Optional[str] = None
    published_at: Optional[str] = None
    content: str
    image_captions: Optional[str] = None
    extracted_events: Optional[List[dict]] = None
    custom_prompt: Optional[str] = None


class EventAnalyzeRequest(BaseModel):
    title: Optional[str] = None
    link: Optional[str] = None
    published_at: Optional[str] = None
    content: str
    image_captions: Optional[str] = None
    image_urls: Optional[List[str]] = None
    custom_extract_prompt: Optional[str] = None
    custom_quality_prompt: Optional[str] = None
    target_table: Optional[str] = "activities"
    auto_insert: Optional[bool] = True


class ImageUnderstandRequest(BaseModel):
    image_urls: Optional[List[str]] = None
    image_captions: Optional[str] = None
    custom_prompt: Optional[str] = None
    max_images: Optional[int] = 3
    fetch_and_embed: Optional[bool] = True


def bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def normalize_json_output(text: str) -> List[Any]:
    source = (text or "").strip()
    if not source:
        return []
    if source.startswith("```"):
        fenced = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", source, re.IGNORECASE)
        if fenced:
            source = fenced.group(1).strip()
    try:
        data = json.loads(source)
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            return data
    except Exception:
        pass

    candidate = source
    if source.startswith("{") and source.endswith("}"):
        try:
            return [json.loads(source)]
        except Exception:
            pass
    if not source.startswith("["):
        if source.count("}{") >= 1:
            candidate = "[" + source.replace("}{", "},{") + "]"
        elif source.count("}\n{") >= 1:
            candidate = "[" + re.sub(r"}\s*{\s*", "},{", source) + "]"
        elif source.count("}, {") >= 1 and not source.strip().startswith("["):
            candidate = "[" + source + "]"
    try:
        data = json.loads(candidate)
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            return data
    except Exception:
        return []
    return []


def normalize_image_understanding_output(
    data: Optional[dict],
    fallback_captions: Optional[str] = None,
) -> Dict[str, Any]:
    raw = data or {}
    description = raw.get("description")
    fallback_text = (fallback_captions or "").strip()
    if description is None and fallback_text:
        description = fallback_text
    elif isinstance(description, str):
        description = description.strip() or None
    result = {
        "image_type": str(raw.get("image_type") or "").strip() or None,
        "image_type_description": str(raw.get("image_type_description") or "").strip() or None,
        "has_valid_activity_info": bool_value(raw.get("has_valid_activity_info")),
        "title": str(raw.get("title") or "").strip() or None,
        "description": description,
    }
    if not result["image_type"] and fallback_text:
        result["image_type"] = "D类 - 其他"
    if not result["image_type_description"] and fallback_text:
        result["image_type_description"] = "已有图片文本摘要"
    return result


def format_image_understanding_text(
    image_understanding: Optional[Dict[str, Any]],
    fallback_captions: Optional[str] = None,
) -> str:
    normalized = normalize_image_understanding_output(image_understanding, fallback_captions)
    return json.dumps(normalized, ensure_ascii=False)


def extract_fallback(
    title: Optional[str],
    link: Optional[str],
    content: str,
    published_at: Optional[str],
    image_captions: Optional[str],
) -> List[dict]:
    text = " ".join([title or "", content or "", image_captions or ""]).strip()
    matched_title = None
    for pattern in [
        r"《([^》]{2,50})》",
        r"【([^】]{2,50})】",
        r"([^\n]{2,30}(大会|峰会|论坛|沙龙|黑客松|路演|发布会|年会|训练营|见面会|招聘会|讲座|开放日))",
    ]:
        match = re.search(pattern, text)
        if match:
            matched_title = match.group(1)
            break
    date_pattern = r"(\d{4}[./-]\d{1,2}[./-]\d{1,2}(?:\s+\d{1,2}:\d{2})?)"
    matched_dates = re.findall(date_pattern, text)
    start_time = matched_dates[0] if matched_dates else None
    end_time = matched_dates[1] if len(matched_dates) >= 2 else None
    city = None
    city_list = [
        "北京", "上海", "广州", "深圳", "杭州", "南京", "成都", "武汉", "西安", "重庆",
        "苏州", "厦门", "天津", "合肥", "郑州", "长沙", "济南", "青岛", "东莞", "佛山", "宁波",
    ]
    for city_name in city_list:
        if city_name in text:
            city = city_name
            break
    address = None
    address_match = re.search(r"(?:地点|地址|会场)[:：]\s*([^\n，。]{2,60})", text)
    if address_match:
        address = address_match.group(1)
        if city and city not in address:
            address = f"{city}·{address}"
    host = None
    host_match = re.search(r"(?:主办方|主办|承办|举办)[:：]\s*([^\n，。]{2,40})", text)
    if host_match:
        host = host_match.group(1)
    return [{
        "title": matched_title or (title or None),
        "link": link or None,
        "description": None,
        "core_value": None,
        "start_time": start_time or None,
        "end_time": end_time or None,
        "city": city or None,
        "address": address or None,
        "host": host or None,
    }]


def infer_fixed_tags_from_text(text: str) -> List[str]:
    base = (text or "").strip()
    rules = [
        ("创业孵化", [r"创业", r"孵化", r"路演", r"投融资", r"创业者", r"创业营", r"加速器"]),
        ("行业大厂资源", [r"大厂", r"名企", r"企业参访", r"企业导师", r"内推", r"实习", r"校招", r"求职"]),
        ("高校创新竞赛", [r"高校", r"大学", r"校园", r"竞赛", r"大赛", r"黑客松", r"hackathon"]),
        ("周末线下沙龙", [r"周末", r"周六", r"周日", r"线下", r"沙龙", r"分享会", r"交流会", r" meetup "]),
        ("AI创意活动", [r"ai", r"人工智能", r"创意", r"生成式", r"aigc", r"作品展", r"创作"]),
        ("AI教学工坊", [r"工坊", r"工作坊", r"训练营", r"实操", r"教学", r"课程", r" workshop "]),
    ]
    text_lower = f" {base.lower()} "
    result: List[str] = []
    for label, patterns in rules:
        for pattern in patterns:
            target = text_lower if re.search(r"[A-Za-z]", pattern) else base
            if re.search(pattern, target, re.IGNORECASE):
                result.append(label)
                break
    return result[:3]


def normalize_fixed_tags(raw_tags: Any, fallback_text: str = "") -> List[str]:
    values: List[str] = []
    if isinstance(raw_tags, list):
        values = [str(item).strip() for item in raw_tags if str(item).strip()]
    elif isinstance(raw_tags, str):
        values = [item.strip() for item in re.split(r"[，,、/\n]+", raw_tags) if item.strip()]

    normalized: List[str] = []
    for tag in values:
        if tag in FIXED_ACTIVITY_TAGS and tag not in normalized:
            normalized.append(tag)

    if not normalized and fallback_text:
        normalized = infer_fixed_tags_from_text(fallback_text)
    return normalized[:3]


def normalize_quality_output(data: Optional[dict]) -> dict:
    raw = data or {}
    decision = str(raw.get("decision", "")).strip()
    category = str(raw.get("category", "")).strip()
    score_raw = raw.get("score", None)
    score = None
    try:
        if score_raw is not None and str(score_raw).strip() != "":
            score = max(0, min(100, int(score_raw)))
    except Exception:
        score = None

    pass_terms = {"通过", "pass", "passed", "approve", "approved", "yes"}
    reject_terms = {"不通过", "reject", "rejected", "fail", "failed", "no"}
    decision_norm = decision.lower()

    if decision_norm in pass_terms:
        final_decision = "通过"
    elif decision_norm in reject_terms:
        final_decision = "不通过"
    elif category in {"优质", "缺失但值得溯源"}:
        final_decision = "通过"
    elif category in {"应丢弃", "无匹配类别"}:
        final_decision = "不通过"
    elif score is not None:
        final_decision = "通过" if score >= 60 else "不通过"
    else:
        final_decision = "需要人为打分"

    review_status = {
        "通过": "ai_passed",
        "不通过": "ai_rejected",
        "需要人为打分": "manual_required",
    }.get(final_decision, "manual_required")

    reason = str(raw.get("reason", "") or "").strip()
    reliable_fields = raw.get("reliable_fields") if isinstance(raw.get("reliable_fields"), list) else []
    missing_fields = raw.get("missing_fields") if isinstance(raw.get("missing_fields"), list) else []
    tags = normalize_fixed_tags(raw.get("tags"))

    return {
        "decision": final_decision,
        "review_status": review_status,
        "requires_manual_review": final_decision == "需要人为打分",
        "score": score,
        "reason": reason,
        "tags": tags,
        "reliable_fields": reliable_fields,
        "missing_fields": missing_fields,
        "raw": raw,
    }


def manual_required_quality(reason: str) -> dict:
    return {
        "decision": "需要人为打分",
        "review_status": "manual_required",
        "requires_manual_review": True,
        "score": None,
        "reason": reason,
        "tags": [],
        "reliable_fields": [],
        "missing_fields": [],
        "raw": {},
    }


def normalize_datetime_value(value: Optional[str]) -> Optional[str]:
    normalized = (value or "").strip()
    if not normalized:
        return None
    return normalized.replace("/", "-")


def extract_unique_explicit_year(source_text: str) -> Optional[str]:
    years = {
        match
        for match in re.findall(r"\b(20\d{2})\b", source_text or "")
        if 2020 <= int(match) <= 2035
    }
    if len(years) != 1:
        return None
    return next(iter(years))


def source_mentions_month_day(source_text: str, month: int, day: int) -> bool:
    tokens = [
        f"{month}.{day}",
        f"{month}-{day}",
        f"{month}/{day}",
        f"{month:02d}.{day:02d}",
        f"{month:02d}-{day:02d}",
        f"{month:02d}/{day:02d}",
        f"{month}月{day}日",
        f"{month}月{day}",
        f"{month:02d}月{day:02d}日",
        f"{month:02d}月{day:02d}",
    ]
    haystack = source_text or ""
    return any(token in haystack for token in tokens)


def align_datetime_year_from_source(value: Any, source_text: str) -> Optional[str]:
    normalized = normalize_datetime_value(value)
    if not normalized:
        return None
    matched = re.match(
        r"^(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})(?P<suffix>(?:[ T]\d{2}:\d{2}(?::\d{2})?)?)$",
        normalized,
    )
    if not matched:
        return normalized

    explicit_year = extract_unique_explicit_year(source_text)
    if not explicit_year or explicit_year == matched.group("year"):
        return normalized

    month = int(matched.group("month"))
    day = int(matched.group("day"))
    if not source_mentions_month_day(source_text, month, day):
        return normalized

    suffix = matched.group("suffix") or ""
    return f"{explicit_year}-{month:02d}-{day:02d}{suffix}"


def align_event_years_from_source(items: List[dict], source_text: str) -> List[dict]:
    aligned: List[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        normalized_item = dict(item)
        for field in ("start_time", "end_time"):
            corrected = align_datetime_year_from_source(normalized_item.get(field), source_text)
            if corrected:
                normalized_item[field] = corrected
        aligned.append(normalized_item)
    return aligned


def pick_best_image_ref(extracted_image: Any, fallback_image: str) -> Optional[str]:
    extracted = str(extracted_image or "").strip()
    fallback = str(fallback_image or "").strip()
    if fallback.startswith("/media/") and not extracted.startswith("/media/"):
        return fallback
    return extracted or fallback or None


def build_activity_records(
    events: List[dict],
    source_title: Optional[str],
    source_link: Optional[str],
    source_content: str,
    image_urls: Optional[List[str]],
) -> List[dict]:
    normalized_title = (source_title or "").strip()
    normalized_link = (source_link or "").strip()
    clean_content = re.sub(r"\s+", " ", source_content or "").strip()
    cover_image_url = ""
    for item in image_urls or []:
        if item and str(item).strip():
            cover_image_url = str(item).strip()
            break

    records: List[dict] = []
    for item in events or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or normalized_title or "").strip()
        original_link = str(item.get("link") or item.get("Link") or normalized_link or "").strip()
        description = str(item.get("description") or "").strip()
        core_value = str(item.get("core_value") or "").strip()
        city = str(item.get("city") or "").strip()
        address = str(item.get("address") or "").strip()
        host = str(item.get("host") or "").strip()
        image = pick_best_image_ref(
            item.get("image") or item.get("image_url") or item.get("cover_image_url"),
            cover_image_url,
        ) or ""
        brief = description or clean_content[:280]
        highlights = [core_value] if core_value else []

        records.append({
            "title": title or None,
            "description": description or None,
            "start_time": normalize_datetime_value(item.get("start_time")),
            "end_time": normalize_datetime_value(item.get("end_time")),
            "location_name": address or city or None,
            "cover_image_url": image or None,
            "image_url": image or None,
            "image": image or None,
            "price": None,
            "currency": "CNY",
            "brief": brief or None,
            "original_link": original_link or None,
            "highlights": highlights,
            "category": "活动",
            "is_featured": False,
            "is_active": True,
            "organizer": host or None,
            "is_trending": False,
            "city": city or None,
            "host": host or None,
        })
    return records


def build_extract_prompt(req: EventExtractRequest) -> str:
    base = (
        "你是一位活动信息结构化专家。请仔细阅读以下小红书帖子内容，从中提取与线下活动相关的信息，并严格按照以下要求输出：\n"
        "\n"
        "仅输出一个合法的 JSON 对象，不要任何解释、前缀或后缀。\n"
        "如果某项信息未在文本中明确提及，请对应字段填写 null。\n"
        "【重要】如果识别到多个活动，就输出多个json，逗号隔开即可\n"
        "【时间约束】如果标题、正文或图片文字里出现了明确的四位年份（如 2026），必须沿用这个年份；不得根据发布时间、当前日期或常识把它改写成别的年份。\n"
        "-----------------------\n"
        "{\n"
        "\"title\": \"字段说明：活动的正式名称或常用简称，应简洁明确，通常包含品牌、届次、主题等关键信息。示例：'模法黑客松 S2'、'2026全球AI开发者大会'。\",\n"
        "\"link\": \"笔记源链接, 例如 `https://mp.weixin.qq.com/s/l3X2GfDfluS2vkYz3sjUvw`\",\n"
        "\"description\": \"字段说明：对活动整体的概括性介绍，突出其性质、目的或特色。避免细节堆砌，聚焦核心定位。示例：'第二届OPC社区黑客松，强调以赛促产，目标是将想法推向现实，而非仅仅制作展示型Demo。'\",\n"
        "\"core_value\": \"字段说明：提炼活动最具吸引力的独特价值或差异化优势，如免费支持、资源对接、赛道方向、评审机制、成果转化路径等。应体现为什么值得参与。示例：'活动全程免费提供食宿和活动保险，降低了参与门槛。赛道方向包括AI+健康、AI+游戏等，评审标准注重创新性和市场潜力。'\",\n"
        "\"start_time\": \"字段说明：活动开始日期，严格采用 ISO 8601 格式 'YYYY-MM-DD'，如包含具体时刻，必须加上时间点，若无可不填。若活动跨多日，此处为第一天。示例：'2026-01-31 14:00' 或 '2026-01-31'。\",\n"
        "\"end_time\": \"字段说明：活动结束日期，同样使用 'YYYY-MM-DD' 格式。如包含具体时刻，必须加上时间点，若无可不填。若活动跨多日，此处为最后一天。示例：'2026-02-11 14:00' 或 '2026-02-11'。\",\n"
        "\"city\": \"字段说明：活动举办城市名称，仅填写市级行政区（无需省份或国家），使用中文标准地名。示例：'南京'、'深圳'、'成都'。\",\n"
        "\"address\": \"字段说明：活动详细地址，需包含具体场地名称，格式建议为‘城市·具体地点’，便于定位。可省略街道门牌号，但应具辨识度。示例：'南京·建邺数字江苏科创园'。\",\n"
        "\"host\": \"字段说明：主办方或主要承办机构名称，使用官方或通用称谓，避免缩写或模糊表述。若有多方主办，可列出最主要的一家。示例：'模法学院'、'中国人工智能学会'。\",\n"
        "\"image\": \"字段说明：活动图片链接，优先填写与活动最相关的帖子图片 URL；若无法明确对应，则优先填写帖子首张图片链接，若没有则为 null。\"\n"
        "}\n"
        "-----------------\n"
        "输出格式示例（仅作参考，不要照抄）：\n"
        "{\n"
        "    \"title\": \"模法黑客松 S2\",\n"
        "    \"link\": \"https://mp.weixin.qq.com/s/l3X2GfDfluS2vkYz3sjUvw\",\n"
        "    \"description\": \"第二届OPC社区黑客松，强调以赛促产，目标是将想法推向现实，而非仅仅制作展示型Demo。\",\n"
        "    \"core_value\": \"活动全程免费提供食宿和活动保险，降低了参与门槛。赛道方向包括AI+健康、AI+游戏等，评审标准注重创新性和市场潜力。\",\n"
        "    \"start_time\": \"2026-01-31\",\n"
        "    \"end_time\": \"2026-02-02\",\n"
        "    \"city\": \"南京\",\n"
        "    \"address\": \"南京·建邺数字江苏科创园\",\n"
        "    \"host\": \"模法学院\",\n"
        "    \"image\": \"https://example.com/poster.jpg\"\n"
        "},\n"
        "----------开始行动-------------\n"
        "待处理的帖子正文：\n"
        "{{笔记内容}}：\n"
        f"发布时间（对于隐含的时间信息有指导意义，例如“本月”等词汇是相对于发布时间而言的）: {req.published_at or ''}\n"
        f"笔记正文: {req.content}\n"
        f"笔记链接(对应link): {req.link or ''}\n"
        f"笔记图片链接候选(对应image): {json.dumps(req.image_urls or [], ensure_ascii=False)}\n"
        f"附上笔记图片理解（你只需要关注对活动有效的信息，其余均可忽视）: {req.image_captions or ''}\n"
    )
    if req.custom_prompt:
        base = req.custom_prompt + "\n" + base
    return base


def build_quality_prompt(req: EventQualityRequest) -> str:
    allowed_tags = "、".join(FIXED_ACTIVITY_TAGS)
    body = (
        "你将收到一段关于某个线下或线上活动的文本描述。请根据是否值得自动化入库进行审核，并且仅输出JSON。\n"
        "如果你有足够把握，请直接给出 decision=通过 或 decision=不通过；如果你无法稳定判断，请返回 decision=需要人为打分。\n"
        "如果内容命中“招聘 / 课程售卖 / 政策解读 / 行业文章”相关关键词，请在原有判断基础上额外扣分。\n"
        f"标签 tags 只能从这 6 个标签中选择 1 到 3 个，不允许输出其他标签：{allowed_tags}。\n"
        "返回字段: {\"decision\":\"通过|不通过|需要人为打分\",\"score\":0-100整数或null,\"reason\":\"简要说明\",\"tags\":[\"标签1\",\"标签2\"],\"reliable_fields\":[\"字段名\"...],\"missing_fields\":[\"字段名\"...]}\n"
        "结构化字段的目标是后续自动插库，因此请优先依据标题、时间、城市、地点、主办方等客观字段进行判断。\n"
        f"标题: {req.title or ''}\n"
        f"发布时间: {req.published_at or ''}\n"
        f"正文: {req.content}\n"
        f"图片理解: {req.image_captions or ''}\n"
    )
    if req.extracted_events:
        body += f"已抽取事件: {json.dumps(req.extracted_events, ensure_ascii=False)}\n"
    if req.custom_prompt:
        body = req.custom_prompt + "\n" + body
    return body


async def extract_events(
    req: EventExtractRequest,
    *,
    call_llm_for_text: Callable[[str, str], Awaitable[Optional[str]]],
    provider_name: str,
) -> Dict[str, Any]:
    prompt = build_extract_prompt(req)
    text = await call_llm_for_text(prompt, "只返回纯JSON，不要包含其他文字")
    items = normalize_json_output(text) if text else []
    used_fallback = False
    if not items:
        items = extract_fallback(req.title, req.link, req.content, req.published_at, req.image_captions)
        used_fallback = True
    return {
        "items": items,
        "provider": provider_name,
        "used_fallback": used_fallback,
    }


async def judge_quality(
    req: EventQualityRequest,
    *,
    call_llm_for_text: Callable[[str, str], Awaitable[Optional[str]]],
    provider_name: str,
) -> Dict[str, Any]:
    prompt = build_quality_prompt(req)
    text = await call_llm_for_text(prompt, "只返回纯JSON，不要包含其他文字")
    if not text:
        quality = manual_required_quality("AI打分不可用，需要人为打分")
    else:
        try:
            quality = normalize_quality_output(json.loads(text))
        except Exception:
            quality = manual_required_quality("AI返回结果不可解析，需要人为打分")
    return {
        "quality": quality,
        "provider": provider_name,
    }


async def analyze_activity_request(
    req: EventAnalyzeRequest,
    *,
    understand_images: Callable[[ImageUnderstandRequest], Awaitable[Dict[str, Any]]],
    call_llm_for_text: Callable[[str, str], Awaitable[Optional[str]]],
    should_materialize_result: Callable[[Optional[Dict[str, Any]]], bool],
    insert_structured_records: Callable[[str, List[dict]], Awaitable[Any]],
) -> Dict[str, Any]:
    provider_name = config_service.resolve_text_ai_settings(dict(os.environ)).get("provider") or "openai"
    image_understanding = await understand_images(
        ImageUnderstandRequest(
            image_urls=req.image_urls,
            image_captions=req.image_captions,
        )
    )
    effective_image_captions = format_image_understanding_text(
        image_understanding,
        req.image_captions,
    )

    extract_result = await extract_events(
        EventExtractRequest(
            title=req.title,
            link=req.link,
            published_at=req.published_at,
            content=req.content,
            image_captions=effective_image_captions,
            image_urls=req.image_urls,
            custom_prompt=req.custom_extract_prompt,
        ),
        call_llm_for_text=call_llm_for_text,
        provider_name=provider_name,
    )
    items = extract_result["items"]
    used_fallback = bool(extract_result["used_fallback"])
    source_text_for_year_alignment = "\n".join(
        part for part in [req.title or "", req.content or "", effective_image_captions or ""] if part
    )
    items = align_event_years_from_source(items, source_text_for_year_alignment)

    structured_records = build_activity_records(
        items,
        req.title,
        req.link,
        req.content,
        req.image_urls,
    )

    quality_result = await judge_quality(
        EventQualityRequest(
            title=req.title,
            link=req.link,
            published_at=req.published_at,
            content=req.content,
            image_captions=effective_image_captions,
            extracted_events=items,
            custom_prompt=req.custom_quality_prompt,
        ),
        call_llm_for_text=call_llm_for_text,
        provider_name=provider_name,
    )
    quality = quality_result["quality"]

    insert_result = None
    if bool_value(req.auto_insert) and should_materialize_result(quality):
        insert_result = await insert_structured_records(
            req.target_table or "activities",
            structured_records,
        )

    return {
        "image_understanding": image_understanding,
        "image_captions": effective_image_captions,
        "events": items,
        "structured_records": structured_records,
        "quality": quality,
        "insert_result": insert_result,
        "provider": provider_name,
        "used_fallback": used_fallback,
    }
