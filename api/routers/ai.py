import asyncio
import base64
import hashlib
import json
import mimetypes
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import re
import uuid
import html
from urllib.parse import urlparse
from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel
from api.schemas.common import ok
from api.services.structured_analysis_service import (
    analyze_activity_request as run_structured_analysis,
    format_image_understanding_text as format_structured_image_understanding_text,
    judge_quality as run_structured_quality,
    manual_required_quality as build_manual_required_quality,
    normalize_quality_output as normalize_structured_quality_output,
    extract_events as run_structured_extract,
)
from api.services.structured_materialize_helpers import (
    backfill_structured_row_images,
    build_source_analyze_payload,
    build_structured_cache_payloads,
    fetch_source_rows,
    load_manual_review_statuses,
    replace_structured_cache_rows,
)
from api.services.structured_materialize_service import (
    serialize_structured_job,
    serialize_structured_job_item,
    structured_materialize_service,
)
from api.services.structured_job_service import structured_job_service
from api.services.config_service import config_service
from api.services.structured_runtime_service import (
    analyze_activity_request_with_runtime,
    cache_image_locally,
    call_llm_for_text as runtime_call_llm_for_text,
    call_llm_multimodal as runtime_call_llm_multimodal,
    ensure_review_state_table,
    insert_structured_records,
    should_materialize_structured_result,
    understand_images_async,
)
from dotenv import load_dotenv
import httpx
load_dotenv()

router = APIRouter(prefix="/ai", tags=["ai"])

FIXED_ACTIVITY_TAGS = [
    "创业孵化",
    "行业大厂资源",
    "高校创新竞赛",
    "周末线下沙龙",
    "AI创意活动",
    "AI教学工坊",
]

STRUCTURED_ACTIVITY_SCORE_THRESHOLD = 60
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
STRUCTURED_IMAGE_CACHE_DIR = DATA_DIR / "structured_images"
IMAGE_UNDERSTANDING_PROMPT = """请分析用户提供的系列图片，并按以下步骤处理： 
 第一步：判断图片类型与信息价值 
 快速判断系列图属于以下哪种类型： 
 A. 活动宣传海报 / 招商邀请函 / 展会预告（含时间、地点、流程等结构化信息） 
 B. 个人头像 / 自拍 / 装饰性配图（无活动信息） 
 C. 商品展示 / 穀物摆拍 / 场景氛围图（可能含少量文字但非活动通知） 
 D. 其他 
 第二步：根据类型决定输出策略 
 如果是 A 类：请提取所有与活动相关的有效信息，保留原文措辞，不要改写、不要标准化格式。 
 如果是 B/C/D 类：只需在 image_type_description 中简要说明图片内容（如“一个戴墨镜的太空人头像”或“一盘抹茶蛋糕的摆拍”），其余字段留空或填 null。 
 第三步：严格按以下 JSON 格式输出结果（仅输出 JSON，无任何额外文字） 
 { 
   "image_type": "字符串，输出“类别 - 具体描述”，例如：C类 - 商品展示", 
   "image_type_description": "字符串，10字左右的简要描述，如'复古市集海报'或'女生自拍头像'", 
   "has_valid_activity_info": true / false, 
   "title": "字符串，主标题（若无则 null）", 
   "description": "与活动相关的有效信息（图片中可能有多条活动出现，请你自行划分好标点符号），保留原文措辞" 
 } 
 特别说明： 
 所有 _raw 字段必须忠实还原图片中的文字，不做任何格式转换、补全或推理。 
 不要试图从头像、背景、装饰元素中“脑补”活动信息。 
 如果图片模糊、文字无法识别，请将 has_valid_activity_info 设为 false，并在 image_type_description 中注明“文字模糊无法识别”等。 
 现在，请分析图片。"""


def _resolve_text_ai_settings() -> Dict[str, str]:
    return config_service.resolve_text_ai_settings(dict(os.environ))


def _resolve_image_ai_settings() -> Dict[str, str]:
    return config_service.resolve_image_ai_settings(dict(os.environ))

class AIProcessRequest(BaseModel):
    table: str
    ids: List[int]
    custom_prompt: Optional[str] = None

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

class MultiModalQualityRequest(BaseModel):
    title: Optional[str] = None
    link: Optional[str] = None
    published_at: Optional[str] = None
    content: str
    image_urls: Optional[List[str]] = None
    image_captions: Optional[str] = None
    custom_prompt: Optional[str] = None
    max_images: Optional[int] = 2
    fetch_and_embed: Optional[bool] = True

class ManualActivityReviewRequest(BaseModel):
    target_table: Optional[str] = "activities"
    decision: str
    structured_records: List[dict]


class ActivitySyncFromDbRequest(BaseModel):
    source_table: str
    ids: List[int]
    target_table: Optional[str] = "activities"
    custom_extract_prompt: Optional[str] = None
    custom_quality_prompt: Optional[str] = None


class StructuredResultSyncRequest(BaseModel):
    ids: List[int]
    target_table: Optional[str] = "activities"


async def _ensure_review_state_table() -> None:
    import database.webui_models  # noqa: F401
    from database.db_session import create_tables
    from sqlalchemy import text
    from sqlalchemy import inspect as sqlalchemy_inspect
    import config
    from database.db_session import get_async_engine

    await create_tables()
    engine = get_async_engine(config.SAVE_DATA_OPTION)
    if not engine:
        return

    def _get_columns(sync_conn) -> set:
        inspector = sqlalchemy_inspect(sync_conn)
        return {col["name"] for col in inspector.get_columns("webui_structured_activity_record")}

    async with engine.begin() as conn:
        try:
            columns = await conn.run_sync(_get_columns)
        except Exception:
            return
        if "image" not in columns:
            await conn.execute(text("ALTER TABLE webui_structured_activity_record ADD COLUMN image TEXT"))


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return ""

def _bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in ("1","true","yes","y","on")

def _call_llm_for_text(prompt: str, system: str = "仅输出JSON") -> Optional[str]:
    return runtime_call_llm_for_text(prompt, system)

def _download_images_as_base64(urls: List[str], limit: int = 2) -> List[dict]:
    out: List[dict] = []
    if not urls:
        return out
    use = [u for u in urls if u][:limit]
    try:
        with httpx.Client(timeout=20) as client:
            for u in use:
                try:
                    r = client.get(u)
                    if r.status_code >= 400:
                        continue
                    ct = r.headers.get("Content-Type", "").split(";")[0].strip() or "image/jpeg"
                    b64 = base64.b64encode(r.content).decode()
                    out.append({"media_type": ct, "data": b64})
                except Exception:
                    continue
    except Exception:
        return []
    return out

def _call_llm_multimodal(prompt: str, system: str, image_urls: List[str]) -> Optional[str]:
    return runtime_call_llm_multimodal(prompt, system, image_urls)

def _normalize_json_output(text: str):
    s = (text or "").strip()
    if not s:
        return []
    try:
        data = json.loads(s)
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            return data
    except Exception:
        pass
    ss = s
    if s.startswith("{") and s.endswith("}"):
        try:
            return [json.loads(s)]
        except Exception:
            pass
    if not s.startswith("["):
        if s.count("}{")>=1:
            ss = "[" + s.replace("}{","},{") + "]"
        elif s.count("}\n{")>=1:
            ss = "[" + re.sub(r"}\s*{\s*", "},{", s) + "]"
        elif s.count("}, {")>=1 and not s.strip().startswith("["):
            ss = "[" + s + "]"
    try:
        data = json.loads(ss)
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            return data
    except Exception:
        return []


def _normalize_image_understanding_output(data: Optional[dict], fallback_captions: Optional[str] = None) -> Dict[str, Any]:
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
        "has_valid_activity_info": _bool(raw.get("has_valid_activity_info")),
        "title": str(raw.get("title") or "").strip() or None,
        "description": description,
    }
    if not result["image_type"] and fallback_text:
        result["image_type"] = "D类 - 其他"
    if not result["image_type_description"] and fallback_text:
        result["image_type_description"] = "已有图片文本摘要"
    return result


def _format_image_understanding_text(image_understanding: Optional[Dict[str, Any]], fallback_captions: Optional[str] = None) -> str:
    normalized = _normalize_image_understanding_output(image_understanding, fallback_captions)
    return json.dumps(normalized, ensure_ascii=False)


def _understand_images(req: ImageUnderstandRequest) -> Dict[str, Any]:
    urls = [str(url).strip() for url in (req.image_urls or []) if str(url).strip()]
    if not _bool(req.fetch_and_embed):
        urls = []
    prompt = req.custom_prompt or IMAGE_UNDERSTANDING_PROMPT
    text = _call_llm_multimodal(prompt, "仅输出 JSON，无任何额外文字", urls[: max(1, int(req.max_images or 3))]) if urls else None
    items = _normalize_json_output(text) if text else []
    data = items[0] if items and isinstance(items[0], dict) else None
    return _normalize_image_understanding_output(data, req.image_captions)


async def _call_llm_for_text_async(prompt: str, system: str = "仅输出JSON") -> Optional[str]:
    return await asyncio.to_thread(_call_llm_for_text, prompt, system)


async def _understand_images_async(req: ImageUnderstandRequest) -> Dict[str, Any]:
    return await asyncio.to_thread(_understand_images, req)

def _extract_fallback(title: Optional[str], link: Optional[str], content: str, published_at: Optional[str], image_captions: Optional[str]) -> List[dict]:
    t = " ".join([title or "", content or "", image_captions or ""]).strip()
    m_title = None
    for pat in [r"《([^》]{2,50})》", r"【([^】]{2,50})】", r"([^\n]{2,30}(大会|峰会|论坛|沙龙|黑客松|路演|发布会|年会|训练营|见面会|招聘会|讲座|开放日))"]:
        m = re.search(pat, t)
        if m:
            m_title = m.group(1)
            break
    date_pat = r"(\d{4}[./-]\d{1,2}[./-]\d{1,2}(?:\s+\d{1,2}:\d{2})?)"
    m_date = re.findall(date_pat, t)
    start_time = m_date[0] if m_date else None
    end_time = m_date[1] if len(m_date)>=2 else None
    city = None
    city_list = ["北京","上海","广州","深圳","杭州","南京","成都","武汉","西安","重庆","苏州","厦门","天津","合肥","郑州","长沙","济南","青岛","东莞","佛山","宁波"]
    for c in city_list:
        if c in t:
            city = c
            break
    address = None
    addr_m = re.search(r"(?:地点|地址|会场)[:：]\s*([^\n，。]{2,60})", t)
    if addr_m:
        address = addr_m.group(1)
        if city and city not in address:
            address = f"{city}·{address}"
    host = None
    host_m = re.search(r"(?:主办方|主办|承办|举办)[:：]\s*([^\n，。]{2,40})", t)
    if host_m:
        host = host_m.group(1)
    item = {
        "title": m_title or (title or None),
        "link": link or None,
        "description": None,
        "core_value": None,
        "start_time": start_time or None,
        "end_time": end_time or None,
        "city": city or None,
        "address": address or None,
        "host": host or None,
    }
    return [item]

def _infer_fixed_tags_from_text(text: str) -> List[str]:
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

def _normalize_fixed_tags(raw_tags: Any, fallback_text: str = "") -> List[str]:
    values: List[str] = []
    if isinstance(raw_tags, list):
        values = [str(x).strip() for x in raw_tags if str(x).strip()]
    elif isinstance(raw_tags, str):
        values = [x.strip() for x in re.split(r"[，,、/\n]+", raw_tags) if x.strip()]

    normalized: List[str] = []
    for tag in values:
        if tag in FIXED_ACTIVITY_TAGS and tag not in normalized:
            normalized.append(tag)

    if not normalized and fallback_text:
        normalized = _infer_fixed_tags_from_text(fallback_text)
    return normalized[:3]

def _normalize_quality_output(data: Optional[dict]) -> dict:
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
    tags = _normalize_fixed_tags(raw.get("tags"))

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

def _manual_required_quality(reason: str) -> dict:
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

def _normalize_datetime_value(value: Optional[str]) -> Optional[str]:
    s = (value or "").strip()
    if not s:
        return None
    return s.replace("/", "-")

def _normalize_memfire_datetime(value: Optional[str]) -> Optional[str]:
    s = _normalize_datetime_value(value)
    if not s:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return f"{s}T00:00:00+08:00"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}", s):
        return s.replace(" ", "T") + ":00+08:00"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", s):
        return s.replace(" ", "T") + "+08:00"
    return s.replace(" ", "T")

def _build_activity_records(
    events: List[dict],
    source_title: Optional[str],
    source_link: Optional[str],
    source_content: str,
    image_urls: Optional[List[str]],
) -> List[dict]:
    source_title = (source_title or "").strip()
    source_link = (source_link or "").strip()
    clean_content = re.sub(r"\s+", " ", source_content or "").strip()
    cover_image_url = ""
    if image_urls:
        for item in image_urls:
            if item and str(item).strip():
                cover_image_url = str(item).strip()
                break

    records: List[dict] = []
    for item in events or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or source_title or "").strip()
        original_link = str(item.get("link") or item.get("Link") or source_link or "").strip()
        description = str(item.get("description") or "").strip()
        core_value = str(item.get("core_value") or "").strip()
        city = str(item.get("city") or "").strip()
        address = str(item.get("address") or "").strip()
        host = str(item.get("host") or "").strip()
        image = str(item.get("image") or item.get("image_url") or item.get("cover_image_url") or cover_image_url or "").strip()
        brief = description or clean_content[:280]
        highlights = [core_value] if core_value else []

        records.append({
            "title": title or None,
            "description": description or None,
            "start_time": _normalize_datetime_value(item.get("start_time")),
            "end_time": _normalize_datetime_value(item.get("end_time")),
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

def _has_memfire_config() -> bool:
    return bool(os.getenv("MEMFIRE_BASE_URL") and os.getenv("MEMFIRE_SERVICE_ROLE_KEY"))

def _build_memfire_headers() -> Dict[str, str]:
    token = (os.getenv("MEMFIRE_SERVICE_ROLE_KEY") or "").strip().strip('"').strip("'")
    if not token:
        raise HTTPException(status_code=400, detail="MEMFIRE_SERVICE_ROLE_KEY 未配置")
    return {
        "apikey": token,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Prefer": "return=representation,missing=default",
    }

def _build_memfire_url(target_table: str) -> str:
    base_url = (os.getenv("MEMFIRE_BASE_URL") or "").strip().strip('"').strip("'").rstrip("/")
    if not base_url:
        raise HTTPException(status_code=400, detail="MEMFIRE_BASE_URL 未配置")
    return f"{base_url}/rest/v1/{target_table}"

def _build_memfire_activity_payload(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    title = str(record.get("title") or "").strip()
    start_time = _normalize_memfire_datetime(record.get("start_time"))
    if not title or not start_time:
        return None

    cover_image_url = str(record.get("cover_image_url") or record.get("image_url") or record.get("image") or "").strip() or None
    organizer = record.get("organizer") or record.get("host")
    payload: Dict[str, Any] = {
        "title": title,
        "start_time": start_time,
        "end_time": _normalize_memfire_datetime(record.get("end_time")),
        "city_id": record.get("city_id"),
        "community_id": record.get("community_id"),
        "organizer_id": record.get("organizer_id"),
        "location_name": record.get("location_name") or None,
        "cover_image_url": cover_image_url,
        "price": record.get("price"),
        "brief": record.get("brief") or None,
        "original_link": record.get("original_link") or None,
        "highlights": record.get("highlights") if isinstance(record.get("highlights"), list) else [],
        "category": record.get("category") or "活动",
        "organizer": organizer or None,
        "is_featured": bool(record.get("is_featured", False)),
        "is_active": True,
        "is_trending": bool(record.get("is_trending", False)),
    }
    return payload


def _build_image_request_headers(image_url: str) -> Dict[str, str]:
    host = urlparse(image_url).netloc.lower()
    referer = "https://mp.weixin.qq.com/" if "qpic.cn" in host else f"{urlparse(image_url).scheme}://{host}/"
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Referer": referer,
    }


def _resolve_media_file_path(value: Optional[str]) -> Optional[Path]:
    raw = str(value or "").strip()
    if not raw.startswith("/media/"):
        return None
    rel_path = raw[len("/media/"):].lstrip("/\\")
    if not rel_path:
        return None
    candidate = (DATA_DIR / rel_path).resolve()
    try:
        candidate.relative_to(DATA_DIR.resolve())
    except Exception:
        return None
    return candidate


def _guess_image_extension(value: str, content_type: Optional[str] = None) -> str:
    parsed_path = urlparse(value).path
    suffix = Path(parsed_path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
        return suffix
    guessed = mimetypes.guess_extension((content_type or "").split(";")[0].strip()) or ""
    if guessed == ".jpe":
        guessed = ".jpg"
    if guessed in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
        return guessed
    return ".jpg"


def _find_cached_structured_image(cache_key: str) -> Optional[str]:
    for suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
        file_path = STRUCTURED_IMAGE_CACHE_DIR / f"{cache_key}{suffix}"
        if file_path.exists() and file_path.stat().st_size > 0:
            return f"/media/structured_images/{file_path.name}"
    return None


async def _cache_image_locally(client: httpx.AsyncClient, image: Optional[str]) -> Optional[str]:
    value = str(image or "").strip()
    if not value or value.startswith("data:image/"):
        return value or None
    if value.startswith("/media/"):
        return value
    if not re.match(r"^https?://", value, flags=re.IGNORECASE):
        return value

    cache_key = hashlib.sha1(value.encode("utf-8")).hexdigest()
    cached_url = _find_cached_structured_image(cache_key)
    if cached_url:
        return cached_url

    try:
        resp = await client.get(value, headers=_build_image_request_headers(value))
        if resp.status_code >= 400 or not resp.content:
            return value
        content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
        if not content_type.startswith("image/"):
            return value
        STRUCTURED_IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        suffix = _guess_image_extension(value, content_type)
        file_path = STRUCTURED_IMAGE_CACHE_DIR / f"{cache_key}{suffix}"
        if not file_path.exists() or file_path.stat().st_size == 0:
            file_path.write_bytes(resp.content)
        return f"/media/structured_images/{file_path.name}"
    except Exception:
        return value


async def _read_image_binary(client: httpx.AsyncClient, image: Optional[str]) -> tuple[Optional[bytes], Optional[str]]:
    value = str(image or "").strip()
    if not value:
        return None, None
    if value.startswith("data:image/"):
        return None, None

    local_path = _resolve_media_file_path(value)
    if local_path and local_path.exists():
        content_type = mimetypes.guess_type(str(local_path))[0] or "image/jpeg"
        try:
            return local_path.read_bytes(), content_type
        except Exception:
            return None, None

    if not re.match(r"^https?://", value, flags=re.IGNORECASE):
        return None, None

    try:
        resp = await client.get(value, headers=_build_image_request_headers(value))
        if resp.status_code >= 400 or not resp.content:
            return None, None
        content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
        if not content_type.startswith("image/"):
            content_type = "image/jpeg"
        return resp.content, content_type
    except Exception:
        return None, None


async def _build_memfire_cover_image_value(client: httpx.AsyncClient, image: Optional[str]) -> Optional[str]:
    value = str(image or "").strip()
    if not value:
        return None
    if value.startswith("data:image/"):
        return value
    if not re.match(r"^https?://", value, flags=re.IGNORECASE) and not value.startswith("/media/"):
        return value
    try:
        content, content_type = await _read_image_binary(client, value)
        if not content:
            return value
        encoded = base64.b64encode(content).decode()
        return f"data:{content_type};base64,{encoded}"
    except Exception:
        return value

async def _insert_memfire_records(target_table: str, structured_records: List[dict]) -> dict:
    if target_table != "activities":
        raise HTTPException(status_code=400, detail="MemFire 目前仅支持 activities 表")

    payloads: List[Dict[str, Any]] = []
    skipped = 0
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        for record in structured_records:
            if not isinstance(record, dict):
                skipped += 1
                continue
            payload = _build_memfire_activity_payload(record)
            if not payload:
                skipped += 1
                continue
            payload["cover_image_url"] = await _build_memfire_cover_image_value(client, payload.get("cover_image_url"))
            payloads.append(payload)

        if not payloads:
            return {"target_table": target_table, "backend": "memfire", "inserted": 0, "skipped": skipped}

        resp = await client.post(
            _build_memfire_url(target_table),
            headers=_build_memfire_headers(),
            json=payloads,
        )
    if resp.status_code >= 400:
        detail = resp.text.strip() or f"MemFire 写入失败: HTTP {resp.status_code}"
        raise HTTPException(status_code=400, detail=detail)

    inserted_rows = []
    try:
        inserted_rows = resp.json() if resp.text else []
    except Exception:
        inserted_rows = []
    return {
        "target_table": target_table,
        "backend": "memfire",
        "inserted": len(payloads),
        "skipped": skipped,
        "inserted_rows": inserted_rows,
    }

async def _resolve_target_table(target_table: str):
    import config
    from sqlalchemy import MetaData, Table, Column, Integer, String, Text, Boolean, DateTime, JSON
    from sqlalchemy.exc import InvalidRequestError
    from sqlalchemy.sql import func
    from database.db_session import get_async_engine

    engine = get_async_engine(getattr(config, "SAVE_DATA_OPTION", "csv"))
    if engine is None:
        raise HTTPException(status_code=400, detail="数据库未配置")

    metadata = MetaData()
    async with engine.begin() as conn:
        if target_table == "activities":
            def _ensure_activities_table(sync_conn):
                Table(
                    "activities",
                    metadata,
                    Column("id", Integer, primary_key=True, autoincrement=True),
                    Column("title", String(255)),
                    Column("start_time", String(64)),
                    Column("end_time", String(64)),
                    Column("location_name", String(255)),
                    Column("cover_image_url", Text),
                    Column("price", String(64)),
                    Column("brief", Text),
                    Column("original_link", Text),
                    Column("highlights", JSON),
                    Column("category", String(64)),
                    Column("is_featured", Boolean, default=False),
                    Column("is_active", Boolean, default=True),
                    Column("organizer", String(255)),
                    Column("is_trending", Boolean, default=False),
                    Column("city", String(64)),
                    Column("host", String(255)),
                    Column("created_at", DateTime, server_default=func.now()),
                    Column("updated_at", DateTime, server_default=func.now(), onupdate=func.now()),
                    extend_existing=True,
                )
                metadata.create_all(sync_conn, tables=[metadata.tables["activities"]], checkfirst=True)
            await conn.run_sync(_ensure_activities_table)

        def _reflect(sync_conn):
            metadata.reflect(bind=sync_conn, only=[target_table])
        try:
            await conn.run_sync(_reflect)
        except InvalidRequestError:
            raise HTTPException(status_code=404, detail=f"目标表不存在: {target_table}")

    table = metadata.tables.get(target_table)
    if table is None:
        raise HTTPException(status_code=404, detail=f"目标表不存在: {target_table}")
    return table

async def _insert_structured_records(target_table: str, structured_records: List[dict]) -> dict:
    if not structured_records:
        return {"target_table": target_table, "inserted": 0}

    if target_table == "activities" and _has_memfire_config():
        return await _insert_memfire_records(target_table, structured_records)

    from sqlalchemy import insert
    from database.db_session import get_session

    table = await _resolve_target_table(target_table)
    inserted = 0

    async with get_session() as session:
        if session is None:
            raise HTTPException(status_code=400, detail="数据库未配置")

        for record in structured_records:
            if not isinstance(record, dict):
                continue
            payload: Dict[str, Any] = {k: v for k, v in record.items() if k in table.c}
            if not payload:
                continue
            if "is_active" in table.c:
                payload["is_active"] = True
            if "id" in table.c and "id" not in payload:
                col_type = str(table.c["id"].type).lower()
                if "uuid" in col_type:
                    payload["id"] = str(uuid.uuid4())
            await session.execute(insert(table).values(**payload))
            inserted += 1

    return {"target_table": target_table, "inserted": inserted}


def _structured_cache_row_to_activity_record(row: Any) -> Dict[str, Any]:
    core_value = getattr(row, "core_value", None)
    source_link = getattr(row, "source_link", None)
    link = getattr(row, "link", None) or source_link
    image = getattr(row, "image", None)
    highlights = [core_value] if core_value else []
    return {
        "title": getattr(row, "title", None),
        "description": getattr(row, "description", None),
        "start_time": getattr(row, "start_time", None),
        "end_time": getattr(row, "end_time", None),
        "location_name": getattr(row, "address", None) or getattr(row, "city", None),
        "cover_image_url": image,
        "image_url": image,
        "image": image,
        "price": None,
        "currency": "CNY",
        "brief": core_value or getattr(row, "description", None),
        "original_link": link,
        "highlights": highlights,
        "category": "活动",
        "is_featured": False,
        "is_active": True,
        "organizer": getattr(row, "host", None),
        "is_trending": False,
        "city": getattr(row, "city", None),
        "host": getattr(row, "host", None),
    }


def _should_materialize_structured_result(quality: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(quality, dict):
        return False
    score = quality.get("score")
    try:
        return score is not None and int(score) > STRUCTURED_ACTIVITY_SCORE_THRESHOLD
    except Exception:
        return False


async def _analyze_activity_request(req: EventAnalyzeRequest) -> Dict[str, Any]:
    return await run_structured_analysis(
        req,
        understand_images=_understand_images_async,
        call_llm_for_text=_call_llm_for_text_async,
        should_materialize_result=_should_materialize_structured_result,
        insert_structured_records=_insert_structured_records,
    )


def _build_extract_prompt(req: EventExtractRequest) -> str:
    base = (
        "你是一位活动信息结构化专家。请仔细阅读以下小红书帖子内容，从中提取与线下活动相关的信息，并严格按照以下要求输出：\n"
        "\n"
        "仅输出一个合法的 JSON 对象，不要任何解释、前缀或后缀。\n"
        "如果某项信息未在文本中明确提及，请对应字段填写 null。\n"
        "【重要】如果识别到多个活动，就输出多个json，逗号隔开即可\n"
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

def _build_quality_prompt(req: EventQualityRequest) -> str:
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

async def process_records_background(table: str, ids: List[int], custom_prompt: Optional[str]):
    """后台异步处理 AI 任务"""
    from sqlalchemy import select, update
    from database.db_session import get_session
    from database.models import Base
    import asyncio
    
    tbl = Base.metadata.tables.get(table)
    if tbl is None:
        return
        
    async with get_session() as session:
        if session is None:
            return
            
        # 1. 标记为处理中
        await session.execute(
            update(tbl).where(tbl.c.id.in_(ids)).values(ai_status=1)
        )
        await session.commit()
        
        # 获取记录
        rows = (await session.execute(select(tbl).where(tbl.c.id.in_(ids)))).mappings().all()
        
        # 2. 模拟 AI 处理过程，如已配置正式文字模型则优先走统一运行时
        text_settings = _resolve_text_ai_settings()
        has_real_ai = bool(text_settings.get("api_key"))
        
        def _norm_text(x: str) -> str:
            return (x or "").strip()
        def _count_cjk(s: str) -> int:
            return len(re.findall(r'[\u4e00-\u9fff]', s))
        def _strip_html(s: str) -> str:
            t = re.sub(r'<style[^>]*>.*?</style>', '', s, flags=re.DOTALL | re.IGNORECASE)
            t = re.sub(r'<[^>]+>', '', t)
            return t
        def _strip_links(s: str) -> str:
            t = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', s)
            t = re.sub(r'\[[^\]]*\]\([^)]*\)', '', t)
            t = re.sub(r'https?://\S+', '', t)
            return t
        def _analyze(row: dict) -> tuple[int, str, str, int]:
            title = _norm_text(str(row.get('title', '')))
            desc = _norm_text(str(row.get('desc', '')))
            content = _norm_text(str(row.get('content', '')))
            digest = _norm_text(str(row.get('digest', '')))
            content_text = _strip_html(content) if ('<' in content and '>' in content) else content
            content_text = _strip_links(content_text)
            base = " ".join([title, desc, digest, content_text]).strip()
            cjk = _count_cjk(base)
            if cjk < 20:
                score = 20
                is_spam = 1
            else:
                if cjk < 50:
                    len_score = 10
                elif cjk < 150:
                    len_score = 20
                elif cjk < 400:
                    len_score = 30
                else:
                    len_score = 40
                imgs_raw = str(row.get('image_list', '')) or str(row.get('pictures', ''))
                imgs = [x for x in (imgs_raw.split(',') if imgs_raw else []) if x.strip()]
                img_score = 0
                if imgs:
                    cnt = len(imgs)
                    img_score = 5 if cnt == 1 else 10 if cnt <= 3 else 15
                md_headings = len(re.findall(r'^\s*#', content, flags=re.MULTILINE))
                heading_score = 5 if md_headings >= 1 else 0
                cjk_chars = re.findall(r'[\u4e00-\u9fff]', base)
                uniq_ratio = (len(set(cjk_chars)) / len(cjk_chars)) if cjk_chars else 0
                uniq_score = int(uniq_ratio * 15)
                sentences = re.split(r'[。！？.!?]+', base)
                lens = [len(s.strip()) for s in sentences if s.strip()]
                avg_len = (sum(lens) / len(lens)) if lens else 0
                if 10 <= avg_len <= 50:
                    read_score = 15
                elif 5 <= avg_len <= 80:
                    read_score = 10
                else:
                    read_score = 5
                meta_score = 0
                if _norm_text(str(row.get('cover', ''))):
                    meta_score += 5
                if _norm_text(str(row.get('author_name', ''))) or _norm_text(str(row.get('account_nickname', ''))):
                    meta_score += 5
                score = len_score + img_score + heading_score + uniq_score + read_score + meta_score
                spam_kw = ['广告', '加微信', '私聊', '推广', '代刷', '买粉', '冲榜', '引流', '扫码', '返利']
                is_spam = 0
                low_quality = (len_score <= 10) or (uniq_ratio < 0.2 and cjk < 150)
                if any(k in base for k in spam_kw):
                    score = max(0, score - 30)
                    is_spam = 1
                elif low_quality:
                    score = max(0, score - 15)
            score = max(0, min(100, int(score)))
            if content_text:
                s = re.sub(r'\s+', ' ', content_text).strip()
                summary = s[:160]
            elif desc or digest:
                s2 = (desc or digest)
                summary = s2[:160]
            else:
                summary = (title or "无标题")
            tags = ",".join(_infer_fixed_tags_from_text(base))
            return score, summary, tags, is_spam
        def _llm_eval_sync(row: dict, custom_prompt: Optional[str]) -> Optional[dict]:
            title = _norm_text(str(row.get('title', '')))
            desc = _norm_text(str(row.get('desc', '')))
            digest = _norm_text(str(row.get('digest', '')))
            content = _norm_text(str(row.get('content', '')))
            content_text = _strip_html(content) if ('<' in content and '>' in content) else content
            base = " ".join([title, desc, digest, content_text]).strip()
            allowed_tags = "、".join(FIXED_ACTIVITY_TAGS)
            prompt = (custom_prompt or "") + "\n" + (
                "请对以下内容进行质量评估并返回JSON："
                "仅返回纯JSON，不要包含解释或其他文字。"
                "如果内容命中“招聘 / 课程售卖 / 政策解读 / 行业文章”相关关键词，请在原有判断基础上额外扣分。"
                f"tags 只能从以下 6 个标签中选择 1 到 3 个，不允许输出其他标签：{allowed_tags}。"
                "字段: score(0-100整数), summary(不超过160字), tags(数组或逗号分隔字符串), is_spam(0或1)。\n\n"
                f"标题: {title}\n摘要: {digest or desc}\n正文: {content_text[:4000]}"
            )
            try:
                text = runtime_call_llm_for_text(
                    prompt,
                    "你是一个严格的内容质量评估器。只输出JSON。",
                )
                if not text:
                    return None
                return json.loads(text)
            except Exception:
                return None
            return None
        for row in rows:
            record_id = row['id']
            base_score, base_summary, base_tags, base_spam = _analyze(row)
            if has_real_ai:
                data = await asyncio.to_thread(_llm_eval_sync, row, custom_prompt)
                if isinstance(data, dict):
                    try:
                        llm_score = int(data.get("score", 0))
                        llm_summary = str(data.get("summary", ""))[:160]
                        llm_tags = ",".join(_normalize_fixed_tags(data.get("tags"), base))
                        llm_spam = int(data.get("is_spam", 0))
                        score = max(0, min(100, int(0.6 * llm_score + 0.4 * base_score)))
                        summary = llm_summary or base_summary
                        tags = llm_tags or base_tags
                        is_spam = 1 if (llm_spam == 1 or base_spam == 1) else 0
                    except Exception:
                        score, summary, tags, is_spam = base_score, base_summary, base_tags, base_spam
                else:
                    score, summary, tags, is_spam = base_score, base_summary, base_tags, base_spam
            else:
                score, summary, tags, is_spam = base_score, base_summary, base_tags, base_spam
                
            # 3. 更新结果
            await session.execute(
                update(tbl).where(tbl.c.id == record_id).values(
                    ai_status=2,
                    ai_score=score,
                    ai_summary=summary,
                    ai_tags=tags,
                    is_spam=is_spam
                )
            )
        
        await session.commit()


@router.post("/process_batch")
async def process_batch(req: AIProcessRequest, background_tasks: BackgroundTasks):
    """提交批量 AI 处理任务"""
    import config
    if getattr(config, "SAVE_DATA_OPTION", "csv") not in ("sqlite", "db", "postgres"):
        raise HTTPException(status_code=400, detail="当前存储模式不支持数据库 AI 处理")
        
    from database.models import Base
    tbl = Base.metadata.tables.get(req.table)
    if tbl is None:
        raise HTTPException(status_code=404, detail="指定的表不存在")
        
    if not req.ids:
        return ok({"message": "没有指定要处理的记录"})
        
    background_tasks.add_task(process_records_background, req.table, req.ids, req.custom_prompt)
    
    return ok({"message": f"已提交 {len(req.ids)} 条数据的 AI 处理任务", "task_count": len(req.ids)})


@router.post("/activity/extract")
async def extract_events(req: EventExtractRequest):
    return ok(
        await run_structured_extract(
            req,
            call_llm_for_text=_call_llm_for_text_async,
            provider_name=_resolve_text_ai_settings().get("provider", "openai"),
        )
    )


@router.post("/activity/quality")
async def judge_quality(req: EventQualityRequest):
    return ok(
        await run_structured_quality(
            req,
            call_llm_for_text=_call_llm_for_text_async,
            provider_name=_resolve_text_ai_settings().get("provider", "openai"),
        )
    )


@router.post("/activity/analyze")
async def analyze_events(req: EventAnalyzeRequest):
    return ok(await analyze_activity_request_with_runtime(req))


@router.post("/activity/image_understand")
async def understand_activity_images(req: ImageUnderstandRequest):
    result = await understand_images_async(req)
    return ok({
        "result": result,
        "image_captions": format_structured_image_understanding_text(result, req.image_captions),
        "provider": _resolve_image_ai_settings().get("provider", "openai"),
    })


@router.post("/activity/preview_from_db")
async def preview_activities_from_db(req: ActivitySyncFromDbRequest):
    if not req.ids:
        raise HTTPException(status_code=400, detail="未选择要预览的记录")
    rows = await fetch_source_rows(req.source_table, req.ids)

    manual_status_map = await load_manual_review_statuses(
        req.source_table,
        req.ids,
        ensure_review_state_table=ensure_review_state_table,
    )
    result_items: List[dict] = []
    skipped_count = 0

    for row in rows:
        row_data = dict(row)
        record_id = int(row_data.get("id"))
        manual_status = manual_status_map.get(record_id, "pending")
        payload = build_source_analyze_payload(row_data)

        if not payload["content"]:
            skipped_count += 1
            continue

        analysis = await analyze_activity_request_with_runtime(
            EventAnalyzeRequest(
                title=payload["title"],
                link=payload["link"],
                published_at=payload["published_at"],
                content=payload["content"],
                image_captions=payload["image_captions"],
                image_urls=payload["image_urls"],
                custom_extract_prompt=req.custom_extract_prompt,
                custom_quality_prompt=req.custom_quality_prompt,
                target_table=req.target_table or "activities",
                auto_insert=False,
            )
        )

        structured_records = analysis.get("structured_records") or []
        quality = analysis.get("quality") or {}
        if not structured_records:
            skipped_count += 1
            continue

        for index, item in enumerate(structured_records):
            result_items.append({
                "row_key": f"{record_id}-{index}",
                "source_table": req.source_table,
                "source_record_id": record_id,
                "source_title": payload["title"] or "",
                "manual_review_status": manual_status,
                "quality_decision": quality.get("decision"),
                "quality_score": quality.get("score"),
                "quality_reason": quality.get("reason"),
                "used_fallback": analysis.get("used_fallback", False),
                "title": item.get("title"),
                "link": item.get("original_link") or payload["link"],
                "description": item.get("description"),
                "core_value": (item.get("highlights") or [None])[0],
                "start_time": item.get("start_time"),
                "end_time": item.get("end_time"),
                "city": item.get("city"),
                "address": item.get("location_name"),
                "host": item.get("host") or item.get("organizer"),
                "image": item.get("image") or item.get("image_url") or item.get("cover_image_url"),
            })

    return ok({
        "source_table": req.source_table,
        "requested": len(req.ids),
        "processed": len(rows),
        "skipped": skipped_count,
        "items": result_items,
    })


@router.post("/activity/materialize_from_db")
async def materialize_activities_from_db(req: ActivitySyncFromDbRequest):
    if not req.ids:
        raise HTTPException(status_code=400, detail="未选择要生成的记录")
    await ensure_review_state_table()

    job = await structured_job_service.create_job(
        source_table=req.source_table,
        ids=req.ids,
        target_table=req.target_table or "activities",
        custom_extract_prompt=req.custom_extract_prompt,
        custom_quality_prompt=req.custom_quality_prompt,
        trigger_type="manual",
    )

    structured_materialize_service.start_job(
        job.id,
        fetch_source_rows=fetch_source_rows,
        load_manual_review_statuses=lambda source_table, ids: load_manual_review_statuses(
            source_table,
            ids,
            ensure_review_state_table=ensure_review_state_table,
        ),
        build_source_analyze_payload=build_source_analyze_payload,
        analyze_activity_request=analyze_activity_request_with_runtime,
        make_event_analyze_request=lambda job, payload: EventAnalyzeRequest(
            title=payload["title"],
            link=payload["link"],
            published_at=payload["published_at"],
            content=payload["content"],
            image_captions=payload["image_captions"],
            image_urls=payload["image_urls"],
            custom_extract_prompt=job.custom_extract_prompt,
            custom_quality_prompt=job.custom_quality_prompt,
            target_table=job.target_table or "activities",
            auto_insert=False,
        ),
        should_materialize_result=should_materialize_structured_result,
        build_structured_cache_payloads=build_structured_cache_payloads,
        replace_structured_cache_rows=lambda source_table, source_record_ids, payloads: replace_structured_cache_rows(
            source_table,
            source_record_ids,
            payloads,
            ensure_review_state_table=ensure_review_state_table,
            cache_image_locally=cache_image_locally,
        ),
    )

    return ok({
        "job": serialize_structured_job(job),
        "message": "已创建结构化生成任务（后台执行）",
    })


@router.get("/activity/materialize_jobs")
async def list_materialize_jobs(
    source_table: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
):
    await ensure_review_state_table()
    jobs, total = await structured_job_service.list_jobs(
        source_table=source_table,
        status=status,
        limit=limit,
        offset=offset,
    )
    return ok({
        "jobs": [serialize_structured_job(j) for j in jobs],
        "total": total,
        "limit": max(1, min(int(limit or 20), 100)),
        "offset": max(0, int(offset or 0)),
    })


@router.get("/activity/materialize_jobs/{job_id}")
async def get_materialize_job(job_id: int, include_items: bool = False):
    await ensure_review_state_table()
    detail = await structured_job_service.get_job_detail(int(job_id))
    if not detail:
        raise HTTPException(status_code=404, detail="任务不存在")
    job = detail["job"]
    payload: Dict[str, Any] = {"job": serialize_structured_job(job)}
    if include_items:
        payload["items"] = [serialize_structured_job_item(it) for it in detail["items"]]
    return ok(payload)


@router.post("/activity/materialize_jobs/{job_id}/cancel")
async def cancel_materialize_job(job_id: int):
    await ensure_review_state_table()
    job = await structured_job_service.cancel_job(int(job_id))
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return ok({
        "job": serialize_structured_job(job),
        "message": "取消请求已提交",
    })


@router.get("/activity/structured_records")
async def list_structured_activity_records(
    source_table: Optional[str] = None,
    keyword: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    high_quality_only: bool = False,
):
    from sqlalchemy import desc, func, or_, select
    from database.db_session import get_session
    from database.webui_models import StructuredActivityRecord

    await ensure_review_state_table()

    page_size = max(1, min(int(limit or 20), 100))
    page_offset = max(0, int(offset or 0))

    async with get_session() as session:
        if session is None:
            raise HTTPException(status_code=400, detail="数据库未配置")

        base_stmt = select(StructuredActivityRecord)
        if source_table:
            base_stmt = base_stmt.where(StructuredActivityRecord.source_table == source_table)
        if keyword:
            like = f"%{keyword.strip()}%"
            base_stmt = base_stmt.where(
                or_(
                    StructuredActivityRecord.title.ilike(like),
                    StructuredActivityRecord.description.ilike(like),
                    StructuredActivityRecord.core_value.ilike(like),
                    StructuredActivityRecord.source_title.ilike(like),
                )
            )

        high_quality_stmt = base_stmt.where(
            StructuredActivityRecord.quality_score > STRUCTURED_ACTIVITY_SCORE_THRESHOLD
        )
        all_total = int(
            (
                await session.execute(
                    select(func.count()).select_from(base_stmt.subquery())
                )
            ).scalar_one()
        )
        high_quality_total = int(
            (
                await session.execute(
                    select(func.count()).select_from(high_quality_stmt.subquery())
                )
            ).scalar_one()
        )

        stmt = high_quality_stmt if high_quality_only else base_stmt
        stmt = stmt.order_by(desc(StructuredActivityRecord.updated_at), desc(StructuredActivityRecord.id))
        rows = (
            await session.execute(
                stmt.offset(page_offset).limit(page_size + 1)
            )
        ).scalars().all()
        await backfill_structured_row_images(
            session,
            rows,
            cache_image_locally=cache_image_locally,
        )

    has_more = len(rows) > page_size
    items = rows[:page_size]
    return ok({
        "records": [
            {
                "id": row.id,
                "source_table": row.source_table,
                "source_record_id": row.source_record_id,
                "event_index": row.event_index,
                "source_title": row.source_title,
                "source_link": row.source_link,
                "source_published_at": row.source_published_at,
                "title": row.title,
                "link": row.link,
                "description": row.description,
                "core_value": row.core_value,
                "start_time": row.start_time,
                "end_time": row.end_time,
                "city": row.city,
                "address": row.address,
                "host": row.host,
                "image": row.image,
                "quality_decision": row.quality_decision,
                "quality_score": row.quality_score,
                "quality_reason": row.quality_reason,
                "manual_review_status": row.manual_review_status,
                "used_fallback": row.used_fallback,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in items
        ],
        "has_more": has_more,
        "limit": page_size,
        "offset": page_offset,
        "high_quality_only": high_quality_only,
        "counts": {
            "all": all_total,
            "high_quality": high_quality_total,
            "current": high_quality_total if high_quality_only else all_total,
        },
    })


@router.post("/activity/sync_structured_results")
async def sync_structured_activity_records(req: StructuredResultSyncRequest):
    from sqlalchemy import select
    from database.db_session import get_session
    from database.webui_models import StructuredActivityRecord

    if not req.ids:
        raise HTTPException(status_code=400, detail="未选择要同步的结构化记录")

    await ensure_review_state_table()

    async with get_session() as session:
        if session is None:
            raise HTTPException(status_code=400, detail="数据库未配置")
        rows = (
            await session.execute(
                select(StructuredActivityRecord).where(StructuredActivityRecord.id.in_(req.ids))
            )
        ).scalars().all()
        await backfill_structured_row_images(
            session,
            rows,
            cache_image_locally=cache_image_locally,
        )

    structured_records = [_structured_cache_row_to_activity_record(row) for row in rows]
    insert_result = await insert_structured_records(req.target_table or "activities", structured_records)

    return ok({
        "target_table": req.target_table or "activities",
        "requested": len(req.ids),
        "processed": len(rows),
        "inserted": int((insert_result or {}).get("inserted") or 0),
        "items": [{"id": row.id, "title": row.title, "source_record_id": row.source_record_id} for row in rows],
    })


@router.post("/activity/sync_from_db")
async def sync_activities_from_db(req: ActivitySyncFromDbRequest):
    if not req.ids:
        raise HTTPException(status_code=400, detail="未选择要同步的记录")
    rows = await fetch_source_rows(req.source_table, req.ids)

    manual_status_map = await load_manual_review_statuses(
        req.source_table,
        req.ids,
        ensure_review_state_table=ensure_review_state_table,
    )
    result_items: List[dict] = []
    inserted_count = 0
    skipped_count = 0

    for row in rows:
        row_data = dict(row)
        record_id = int(row_data.get("id"))
        manual_status = manual_status_map.get(record_id, "pending")
        payload = build_source_analyze_payload(row_data)

        if not payload["content"]:
            skipped_count += 1
            result_items.append({
                "id": record_id,
                "title": payload["title"] or "",
                "manual_review_status": manual_status,
                "status": "skipped",
                "reason": "记录缺少可用于结构化的正文内容",
            })
            continue

        analysis = await analyze_activity_request_with_runtime(
            EventAnalyzeRequest(
                title=payload["title"],
                link=payload["link"],
                published_at=payload["published_at"],
                content=payload["content"],
                image_captions=payload["image_captions"],
                image_urls=payload["image_urls"],
                custom_extract_prompt=req.custom_extract_prompt,
                custom_quality_prompt=req.custom_quality_prompt,
                target_table=req.target_table or "activities",
                auto_insert=True,
            )
        )

        insert_result = analysis.get("insert_result")
        if manual_status == "passed" and not insert_result and analysis.get("structured_records"):
            insert_result = await insert_structured_records(
                req.target_table or "activities",
                analysis["structured_records"],
            )
            analysis["insert_result"] = insert_result
            analysis["quality"] = {
                **(analysis.get("quality") or {}),
                "decision": "通过",
                "reason": "人工审核已通过，直接执行远程同步",
            }

        inserted = int((analysis.get("insert_result") or {}).get("inserted") or 0)
        if inserted > 0:
            inserted_count += inserted
            status = "inserted"
            reason = (analysis.get("quality") or {}).get("reason") or ""
        else:
            skipped_count += 1
            status = "skipped"
            reason = (analysis.get("quality") or {}).get("reason") or "未满足入库条件"

        result_items.append({
            "id": record_id,
            "title": payload["title"] or "",
            "manual_review_status": manual_status,
            "status": status,
            "inserted": inserted,
            "quality_decision": (analysis.get("quality") or {}).get("decision"),
            "reason": reason,
            "used_fallback": analysis.get("used_fallback", False),
        })

    return ok({
        "source_table": req.source_table,
        "target_table": req.target_table or "activities",
        "requested": len(req.ids),
        "processed": len(rows),
        "inserted": inserted_count,
        "skipped": skipped_count,
        "items": result_items,
    })

@router.post("/activity/manual_review")
async def manual_review_activity(req: ManualActivityReviewRequest):
    decision = (req.decision or "").strip()
    decision_pass = decision in {"通过", "人工通过", "pass", "approved"}
    if not decision_pass:
        return ok({
            "decision": decision or "不通过",
            "review_status": "manual_rejected",
            "insert_result": None,
        })

    insert_result = await insert_structured_records(
        req.target_table or "activities",
        req.structured_records or [],
    )
    return ok({
        "decision": "通过",
        "review_status": "manual_passed",
        "insert_result": insert_result,
    })

def _build_mm_quality_prompt(req: MultiModalQualityRequest) -> str:
    allowed_tags = "、".join(FIXED_ACTIVITY_TAGS)
    body = (
        "你将收到一段关于某个线下或线上活动的文本描述，并结合提供的图片信息进行审核，仅输出JSON：\n"
        "如果内容命中“招聘 / 课程售卖 / 政策解读 / 行业文章”相关关键词，请在原有判断基础上额外扣分。\n"
        f"标签 tags 只能从这 6 个标签中选择 1 到 3 个，不允许输出其他标签：{allowed_tags}。\n"
        "返回字段: {\"decision\":\"通过|不通过|需要人为打分\",\"score\":0-100整数或null,\"reason\":\"简要说明\",\"tags\":[\"标签1\",\"标签2\"],\"reliable_fields\":[\"字段名\"...],\"missing_fields\":[\"字段名\"...]}\n"
        f"标题: {req.title or ''}\n"
        f"发布时间: {req.published_at or ''}\n"
        f"正文: {req.content}\n"
        f"图片理解: {req.image_captions or ''}\n"
    )
    if req.custom_prompt:
        body = req.custom_prompt + "\n" + body
    return body

@router.post("/multimodal/quality")
async def multimodal_quality(req: MultiModalQualityRequest):
    prompt = _build_mm_quality_prompt(req)
    urls = (req.image_urls or [])[: (req.max_images or 2)]
    if not _bool(req.fetch_and_embed):
        urls = []
    text = _call_llm_multimodal(prompt, "只返回纯JSON，不要包含其他文字", urls)
    data = None
    used_images = bool(urls)
    if text:
        try:
            data = json.loads(text)
        except Exception:
            data = None
    if not data:
        result = build_manual_required_quality("AI打分不可用，需要人为打分")
    else:
        result = normalize_structured_quality_output(data)
    return ok({
        "result": result,
        "provider": _resolve_image_ai_settings().get("provider", "openai"),
        "used_images": used_images,
        "used_fallback": False,
    })
