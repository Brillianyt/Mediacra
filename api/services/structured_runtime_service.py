"""结构化任务运行时能力。

为路由层和独立 worker 提供共享的 LLM、图片理解、入库、图片本地化等能力，
避免 worker 直接依赖 `api.routers.ai`。
"""

import asyncio
import base64
import hashlib
import mimetypes
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from fastapi import HTTPException

from api.services.config_service import config_service
from api.services.dimension_resolver_service import resolve_activity_dimensions
from api.services.structured_analysis_service import (
    ImageUnderstandRequest,
    analyze_activity_request as run_structured_analysis,
    normalize_image_understanding_output,
    normalize_json_output,
)

load_dotenv()

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


def bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def int_value(value: Any, default: int) -> int:
    try:
        if value is None or str(value).strip() == "":
            return default
        return int(str(value).strip())
    except Exception:
        return default


def normalize_deepseek_chat_url(base_url: str) -> str:
    value = (base_url or "").strip().rstrip("/")
    if not value:
        return "https://api.deepseek.com/chat/completions"
    lowered = value.lower()
    if lowered.endswith("/chat/completions"):
        return value
    if lowered.endswith("/v1"):
        return f"{value}/chat/completions"
    parsed = urlparse(value)
    if parsed.netloc.endswith("api.deepseek.com"):
        return f"{value}/chat/completions"
    return value


def normalize_openai_chat_url(base_url: str) -> str:
    value = (base_url or "").strip().rstrip("/")
    if not value:
        return "https://api.openai.com/v1/chat/completions"
    lowered = value.lower()
    if lowered.endswith("/chat/completions"):
        return value
    if lowered.endswith("/v1"):
        return f"{value}/chat/completions"
    return f"{value}/v1/chat/completions"


def extract_http_error_detail(response: httpx.Response) -> str:
    detail = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or error.get("type") or "").strip()
            elif error is not None:
                detail = str(error).strip()
    except Exception:
        detail = ""
    if not detail:
        detail = (response.text or "").strip().replace("\r", " ").replace("\n", " ")
    return detail[:240]


def extract_chat_completion_text(data: Dict[str, Any]) -> Optional[str]:
    choices = data.get("choices") or []
    if not choices:
        return None
    message = choices[0].get("message", {})
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = str(item.get("text") or "").strip()
                if text:
                    parts.append(text)
        return "\n".join(parts).strip() or None
    return None


def call_openai_compatible_chat(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: List[Dict[str, Any]],
    timeout: int,
    max_tokens: int,
) -> Tuple[Optional[str], Optional[str]]:
    url = normalize_openai_chat_url(base_url)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "stream": False,
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, headers=headers, json=payload)
            if response.status_code >= 400:
                detail = extract_http_error_detail(response)
                error = f"HTTP {response.status_code}" + (f" - {detail}" if detail else "")
                return None, error
            data = response.json()
    except Exception as exc:
        return None, str(exc).strip() or "request failed"
    text = extract_chat_completion_text(data)
    if text is None:
        return None, "未返回 choices 或 message.content"
    return text, None


async def ensure_review_state_table() -> None:
    import database.webui_models  # noqa: F401
    from database.db_session import create_tables, get_async_engine
    from sqlalchemy import inspect as sqlalchemy_inspect
    from sqlalchemy import text

    await create_tables()
    engine = get_async_engine(config_service.get("SAVE_DATA_OPTION", "csv"))
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


def call_llm_for_text(prompt: str, system: str = "仅输出JSON") -> Optional[str]:
    try:
        settings = config_service.resolve_text_ai_settings()
        provider = settings["provider"]
        model = settings["model"] or ("gpt-4o-mini" if provider == "openai" else "")
        api_key = settings["api_key"]
        base_url = settings["base_url"]
        timeout = int_value(settings.get("timeout"), 60)
        max_tokens = int_value(settings.get("max_tokens"), 1200)
        if provider == "openai" and api_key:
            text, _error = call_openai_compatible_chat(
                base_url=base_url,
                api_key=api_key,
                model=model or "gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                timeout=timeout,
                max_tokens=max_tokens,
            )
            return text
        if provider == "anthropic" and api_key:
            try:
                import anthropic
            except Exception:
                return None
            client_kwargs = {"api_key": api_key}
            if base_url:
                client_kwargs["base_url"] = base_url
            client = anthropic.Anthropic(**client_kwargs)
            try:
                msg = client.messages.create(
                    model=model or "claude-3-haiku-20240307",
                    max_tokens=max_tokens,
                    temperature=0.2,
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                )
                parts = getattr(msg, "content", [])
                if parts and hasattr(parts[0], "text"):
                    return parts[0].text or ""
                if parts and isinstance(parts[0], dict):
                    return parts[0].get("text", "")
                return None
            except Exception:
                return None
        if provider == "deepseek" and api_key:
            url = normalize_deepseek_chat_url(base_url)
            model_name = model or "deepseek-chat"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
                "max_tokens": max_tokens,
                "stream": False,
            }
            try:
                with httpx.Client(timeout=timeout) as client:
                    response = client.post(url, headers=headers, json=payload)
                    if response.status_code >= 400:
                        return None
                    data = response.json()
                    choices = data.get("choices") or []
                    if choices:
                        return choices[0].get("message", {}).get("content", "")
            except Exception:
                return None
    except Exception:
        return None
    return None


async def call_llm_for_text_async(prompt: str, system: str = "仅输出JSON") -> Optional[str]:
    return await asyncio.to_thread(call_llm_for_text, prompt, system)


def download_images_as_base64(urls: List[str], limit: int = 2) -> List[dict]:
    result: List[dict] = []
    if not urls:
        return result
    use_urls = [url for url in urls if url][:limit]
    try:
        with httpx.Client(timeout=20) as client:
            for url in use_urls:
                try:
                    local_file = resolve_media_file_path(url)
                    if local_file and local_file.exists():
                        content = local_file.read_bytes()
                        content_type = mimetypes.guess_type(local_file.name)[0] or "image/jpeg"
                        encoded = base64.b64encode(content).decode()
                        result.append({"media_type": content_type, "data": encoded})
                        continue
                    response = client.get(url)
                    if response.status_code >= 400:
                        continue
                    content_type = response.headers.get("Content-Type", "").split(";")[0].strip() or "image/jpeg"
                    encoded = base64.b64encode(response.content).decode()
                    result.append({"media_type": content_type, "data": encoded})
                except Exception:
                    continue
    except Exception:
        return []
    return result


def convert_image_ref_for_multimodal(image_ref: str) -> Optional[str]:
    value = str(image_ref or "").strip()
    if not value:
        return None
    if value.startswith("data:image/"):
        return value
    local_file = resolve_media_file_path(value)
    if local_file and local_file.exists():
        content = local_file.read_bytes()
        media_type = mimetypes.guess_type(local_file.name)[0] or "image/jpeg"
        encoded = base64.b64encode(content).decode()
        return f"data:{media_type};base64,{encoded}"
    return value


def model_supports_vision(provider: str, model: str) -> bool:
    name = str(model or "").strip().lower()
    kind = str(provider or "").strip().lower()
    if kind == "openai":
        if not name:
            return True
        return any(
            token in name
            for token in ("gpt-4o", "gpt-4.1", "gpt-4-turbo", "vision", "o1", "o3", "qwen-vl", "ocr")
        )
    if kind == "anthropic":
        if not name:
            return True
        return any(token in name for token in ("claude-3", "claude-4"))
    if kind == "deepseek":
        return any(token in name for token in ("vl", "vision", "janus"))
    return False


def summarize_runtime_error(exc: Exception, fallback: str) -> str:
    detail = str(exc or "").strip()
    if not detail:
        return fallback
    return f"{fallback}: {detail[:240]}"


def prepare_image_refs_for_multimodal(image_urls: List[str], limit: int = 3) -> Tuple[List[str], bool, bool]:
    image_refs: List[str] = []
    used_local_image = False
    image_fetch_succeeded = False
    for url in image_urls[: max(1, limit)]:
        value = str(url or "").strip()
        if not value:
            continue
        try:
            local_file = resolve_media_file_path(value)
            converted = convert_image_ref_for_multimodal(value)
        except Exception:
            continue
        if not converted:
            continue
        image_refs.append(converted)
        image_fetch_succeeded = True
        if converted.startswith("data:image/") or bool(local_file and local_file.exists()):
            used_local_image = True
    return image_refs, used_local_image, image_fetch_succeeded


def call_llm_multimodal_with_diagnostics(prompt: str, system: str, image_urls: List[str]) -> Dict[str, Any]:
    settings = config_service.resolve_image_ai_settings()
    provider = settings["provider"]
    model = settings["model"] or ("gpt-4o-mini" if provider == "openai" else "")
    api_key = settings["api_key"]
    base_url = settings["base_url"]
    timeout = int_value(settings.get("timeout"), 60)
    max_tokens = int_value(settings.get("max_tokens"), 1200)
    diagnostics: Dict[str, Any] = {
        "text": None,
        "vision_provider": provider or None,
        "vision_model": model or None,
        "vision_supported": model_supports_vision(provider, model),
        "used_local_image": False,
        "image_fetch_succeeded": False,
        "image_understanding_error": None,
    }
    if not api_key:
        diagnostics["image_understanding_error"] = "图片 API Key 未配置"
        return diagnostics
    if not image_urls:
        diagnostics["image_understanding_error"] = "未提供可用于图片理解的图片"
        return diagnostics
    if not diagnostics["vision_supported"]:
        diagnostics["image_understanding_error"] = "当前图片模型可能不支持视觉输入，请切换到支持图片的模型"
        return diagnostics
    if provider == "openai" and api_key:
        image_refs, used_local_image, image_fetch_succeeded = prepare_image_refs_for_multimodal(image_urls, limit=3)
        diagnostics["used_local_image"] = used_local_image
        diagnostics["image_fetch_succeeded"] = image_fetch_succeeded
        if not image_refs:
            diagnostics["image_understanding_error"] = "图片读取失败，无法生成可提交给模型的图片内容"
            return diagnostics
        images = [{"type": "image_url", "image_url": {"url": url}} for url in image_refs]
        text, error = call_openai_compatible_chat(
            base_url=base_url,
            api_key=api_key,
            model=model or "gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": [{"type": "text", "text": prompt}, *images]},
            ],
            timeout=timeout,
            max_tokens=max_tokens,
        )
        diagnostics["text"] = text
        if not text:
            diagnostics["image_understanding_error"] = (
                f"图片模型调用失败: {error}" if error else "图片模型返回为空"
            )
            return diagnostics
        return diagnostics
    if provider == "deepseek" and api_key:
        image_refs, used_local_image, image_fetch_succeeded = prepare_image_refs_for_multimodal(image_urls, limit=3)
        diagnostics["used_local_image"] = used_local_image
        diagnostics["image_fetch_succeeded"] = image_fetch_succeeded
        if not image_refs:
            diagnostics["image_understanding_error"] = "图片读取失败，无法生成可提交给模型的图片内容"
            return diagnostics
        url = normalize_deepseek_chat_url(base_url)
        model_name = model or "deepseek-chat"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        images = [{"type": "image_url", "image_url": {"url": image_url}} for image_url in image_refs]
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": [{"type": "text", "text": prompt}, *images]},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "stream": False,
        }
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(url, headers=headers, json=payload)
                if response.status_code >= 400:
                    detail = extract_http_error_detail(response)
                    diagnostics["image_understanding_error"] = (
                        f"图片模型调用失败: HTTP {response.status_code}" + (f" - {detail}" if detail else "")
                    )
                    return diagnostics
                data = response.json()
                choices = data.get("choices") or []
                if choices:
                    diagnostics["text"] = choices[0].get("message", {}).get("content", "")
                    if not diagnostics["text"]:
                        diagnostics["image_understanding_error"] = "图片模型返回为空"
                    return diagnostics
                diagnostics["image_understanding_error"] = "图片模型未返回 choices"
                return diagnostics
        except Exception as exc:
            diagnostics["image_understanding_error"] = summarize_runtime_error(exc, "图片模型调用失败")
            return diagnostics
    if provider == "anthropic" and api_key:
        diagnostics["used_local_image"] = any(
            bool(resolve_media_file_path(str(url or "").strip()))
            for url in image_urls[:2]
            if str(url or "").strip()
        )
        try:
            import anthropic
        except Exception:
            diagnostics["image_understanding_error"] = "anthropic SDK 未安装，无法调用图片模型"
            return diagnostics
        client_kwargs = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        client = anthropic.Anthropic(**client_kwargs)
        embeds = download_images_as_base64(image_urls, limit=2)
        diagnostics["image_fetch_succeeded"] = bool(embeds)
        if not embeds:
            diagnostics["image_understanding_error"] = "图片读取失败，无法生成可提交给模型的图片内容"
            return diagnostics
        content = [{"type": "text", "text": prompt}]
        for item in embeds:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": item["media_type"],
                        "data": item["data"],
                    },
                }
            )
        try:
            msg = client.messages.create(
                model=model or "claude-3-haiku-20240307",
                max_tokens=max_tokens,
                temperature=0.2,
                system=system,
                messages=[{"role": "user", "content": content}],
            )
            parts = getattr(msg, "content", [])
            if parts and hasattr(parts[0], "text"):
                diagnostics["text"] = parts[0].text or ""
                if not diagnostics["text"]:
                    diagnostics["image_understanding_error"] = "图片模型返回为空"
                return diagnostics
            if parts and isinstance(parts[0], dict):
                diagnostics["text"] = parts[0].get("text", "")
                if not diagnostics["text"]:
                    diagnostics["image_understanding_error"] = "图片模型返回为空"
                return diagnostics
            diagnostics["image_understanding_error"] = "图片模型未返回可解析内容"
            return diagnostics
        except Exception as exc:
            diagnostics["image_understanding_error"] = summarize_runtime_error(exc, "图片模型调用失败")
            return diagnostics
    diagnostics["image_understanding_error"] = "当前图片 Provider 不支持多模态调用"
    return diagnostics


def call_llm_multimodal(prompt: str, system: str, image_urls: List[str]) -> Optional[str]:
    return call_llm_multimodal_with_diagnostics(prompt, system, image_urls).get("text")


def understand_images(req: ImageUnderstandRequest) -> Dict[str, Any]:
    settings = config_service.resolve_image_ai_settings()
    provider = settings["provider"]
    model = settings["model"] or ("gpt-4o-mini" if provider == "openai" else "")
    if settings.get("enabled") and not bool_value(settings["enabled"]):
        disabled_result = normalize_image_understanding_output(None, req.image_captions)
        disabled_result.update({
            "vision_provider": provider or None,
            "vision_model": model or None,
            "vision_supported": model_supports_vision(provider, model),
            "used_local_image": False,
            "image_fetch_succeeded": False,
            "image_understanding_error": "图片理解已禁用",
        })
        return disabled_result
    urls = [str(url).strip() for url in (req.image_urls or []) if str(url).strip()]
    if not bool_value(req.fetch_and_embed):
        urls = []
    prompt = req.custom_prompt or IMAGE_UNDERSTANDING_PROMPT
    max_images = int_value(settings.get("max_images"), int(req.max_images or 3))
    multimodal_result = (
        call_llm_multimodal_with_diagnostics(prompt, "仅输出 JSON，无任何额外文字", urls[: max(1, max_images)])
        if urls
        else {
            "text": None,
            "vision_provider": provider or None,
            "vision_model": model or None,
            "vision_supported": model_supports_vision(provider, model),
            "used_local_image": False,
            "image_fetch_succeeded": False,
            "image_understanding_error": "未提供可用于图片理解的图片",
        }
    )
    text = multimodal_result.get("text")
    items = normalize_json_output(text) if text else []
    data = items[0] if items and isinstance(items[0], dict) else None
    result = normalize_image_understanding_output(data, req.image_captions)
    result.update({
        "vision_provider": multimodal_result.get("vision_provider"),
        "vision_model": multimodal_result.get("vision_model"),
        "vision_supported": bool(multimodal_result.get("vision_supported")),
        "used_local_image": bool(multimodal_result.get("used_local_image")),
        "image_fetch_succeeded": bool(multimodal_result.get("image_fetch_succeeded")),
        "image_understanding_error": multimodal_result.get("image_understanding_error"),
    })
    if text and not data and not result.get("image_understanding_error"):
        result["image_understanding_error"] = "图片模型返回了内容，但未能解析为合法 JSON"
    return result


def clone_request_with_image_urls(req: Any, image_urls: List[str]) -> Any:
    if hasattr(req, "model_copy"):
        try:
            return req.model_copy(update={"image_urls": image_urls})
        except Exception:
            pass
    if hasattr(req, "copy"):
        try:
            return req.copy(update={"image_urls": image_urls})
        except Exception:
            pass
    if isinstance(req, dict):
        copied = dict(req)
        copied["image_urls"] = image_urls
        return copied
    try:
        setattr(req, "image_urls", image_urls)
    except Exception:
        return req
    return req


async def localize_image_urls_for_runtime(image_urls: Optional[List[str]]) -> List[str]:
    cleaned: List[str] = []
    seen: set[str] = set()
    for item in image_urls or []:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        cleaned.append(value)
    if not cleaned:
        return []

    settings = config_service.resolve_image_ai_settings()
    use_local_first = settings.get("use_local_first")
    if str(use_local_first or "").strip() and not bool_value(use_local_first):
        return cleaned

    localized: List[str] = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for image_url in cleaned:
            localized_value = await cache_image_locally(client, image_url)
            localized.append(str(localized_value or image_url).strip())
    return localized


async def understand_images_async(req: ImageUnderstandRequest) -> Dict[str, Any]:
    localized_urls = await localize_image_urls_for_runtime(req.image_urls)
    runtime_req = clone_request_with_image_urls(req, localized_urls)
    return await asyncio.to_thread(understand_images, runtime_req)


def normalize_datetime_value(value: Optional[str]) -> Optional[str]:
    text = (value or "").strip()
    if not text:
        return None
    return text.replace("/", "-")


def normalize_memfire_datetime(value: Optional[str]) -> Optional[str]:
    text = normalize_datetime_value(value)
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return f"{text}T00:00:00+08:00"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}", text):
        return text.replace(" ", "T") + ":00+08:00"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", text):
        return text.replace(" ", "T") + "+08:00"
    return text.replace(" ", "T")


def has_memfire_config() -> bool:
    return bool(os.getenv("MEMFIRE_BASE_URL") and os.getenv("MEMFIRE_SERVICE_ROLE_KEY"))


def build_memfire_headers() -> Dict[str, str]:
    token = (os.getenv("MEMFIRE_SERVICE_ROLE_KEY") or "").strip().strip('"').strip("'")
    if not token:
        raise HTTPException(status_code=400, detail="MEMFIRE_SERVICE_ROLE_KEY 未配置")
    return {
        "apikey": token,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Prefer": "return=representation,missing=default",
    }


def build_memfire_url(target_table: str) -> str:
    base_url = (os.getenv("MEMFIRE_BASE_URL") or "").strip().strip('"').strip("'").rstrip("/")
    if not base_url:
        raise HTTPException(status_code=400, detail="MEMFIRE_BASE_URL 未配置")
    return f"{base_url}/rest/v1/{target_table}"


def build_memfire_activity_payload(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    title = str(record.get("title") or "").strip()
    start_time = normalize_memfire_datetime(record.get("start_time"))
    if not title or not start_time:
        return None

    cover_image_url = (
        str(record.get("cover_image_url") or record.get("image_url") or record.get("image") or "").strip() or None
    )
    organizer = record.get("organizer") or record.get("host")
    payload = {
        "title": title,
        "start_time": start_time,
        "end_time": normalize_memfire_datetime(record.get("end_time")),
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
    payload["is_active"] = True
    return payload


def build_dimension_resolution_detail(source_record: Dict[str, Any], resolved_record: Dict[str, Any]) -> Dict[str, Any]:
    resolution = resolved_record.get("_dimension_resolution")
    if not isinstance(resolution, dict):
        resolution = {}

    detail: Dict[str, Any] = {
        "title": str(resolved_record.get("title") or source_record.get("title") or "").strip() or None,
    }

    city_resolution = resolution.get("city")
    if isinstance(city_resolution, dict):
        detail["city"] = {
            "status": city_resolution.get("status"),
            "id": city_resolution.get("id") or resolved_record.get("city_id"),
            "label": city_resolution.get("label"),
            "name": city_resolution.get("name"),
            "raw_value": city_resolution.get("raw_value"),
        }

    category_resolution = resolution.get("category")
    if isinstance(category_resolution, dict):
        detail["category"] = {
            "status": category_resolution.get("status"),
            "id": category_resolution.get("id") or resolved_record.get("_resolved_category_tag_id"),
            "name": category_resolution.get("name") or resolved_record.get("category"),
            "raw_value": category_resolution.get("raw_value"),
        }
    unresolved_category = resolution.get("unresolved_category")
    if isinstance(unresolved_category, dict):
        detail["unresolved_category"] = {
            "raw_value": unresolved_category.get("raw_value"),
            "normalized_value": unresolved_category.get("normalized_value"),
        }

    tag_resolution = resolution.get("tags")
    if isinstance(tag_resolution, list):
        detail["tags"] = {
            "count": len(tag_resolution),
            "ids": [item.get("id") for item in tag_resolution if isinstance(item, dict) and item.get("id")],
            "names": [item.get("name") for item in tag_resolution if isinstance(item, dict) and item.get("name")],
            "raw_values": [
                item.get("raw_value") for item in tag_resolution if isinstance(item, dict) and item.get("raw_value")
            ],
        }
    else:
        raw_tags = source_record.get("tags")
        if raw_tags:
            detail["tags"] = {
                "count": 0,
                "ids": [],
                "names": [],
                "raw_values": raw_tags if isinstance(raw_tags, list) else [str(raw_tags)],
            }
    unresolved_tag_resolution = resolution.get("unresolved_tags")
    if isinstance(unresolved_tag_resolution, list):
        detail["unresolved_tags"] = {
            "count": len(unresolved_tag_resolution),
            "raw_values": [
                item.get("raw_value")
                for item in unresolved_tag_resolution
                if isinstance(item, dict) and item.get("raw_value")
            ],
            "normalized_values": [
                item.get("normalized_value")
                for item in unresolved_tag_resolution
                if isinstance(item, dict) and item.get("normalized_value")
            ],
        }

    return detail


def build_image_request_headers(image_url: str) -> Dict[str, str]:
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


def resolve_media_file_path(value: Optional[str]) -> Optional[Path]:
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


def guess_image_extension(value: str, content_type: Optional[str] = None) -> str:
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


def find_cached_structured_image(cache_key: str) -> Optional[str]:
    for suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
        file_path = STRUCTURED_IMAGE_CACHE_DIR / f"{cache_key}{suffix}"
        if file_path.exists() and file_path.stat().st_size > 0:
            return f"/media/structured_images/{file_path.name}"
    return None


async def cache_image_locally(client: httpx.AsyncClient, image: Optional[str]) -> Optional[str]:
    value = str(image or "").strip()
    if not value or value.startswith("data:image/"):
        return value or None
    if value.startswith("/media/"):
        return value
    if not re.match(r"^https?://", value, flags=re.IGNORECASE):
        return value

    cache_key = hashlib.sha1(value.encode("utf-8")).hexdigest()
    cached_url = find_cached_structured_image(cache_key)
    if cached_url:
        return cached_url

    try:
        response = await client.get(value, headers=build_image_request_headers(value))
        if response.status_code >= 400 or not response.content:
            return value
        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
        if not content_type.startswith("image/"):
            return value
        STRUCTURED_IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        suffix = guess_image_extension(value, content_type)
        file_path = STRUCTURED_IMAGE_CACHE_DIR / f"{cache_key}{suffix}"
        if not file_path.exists() or file_path.stat().st_size == 0:
            file_path.write_bytes(response.content)
        return f"/media/structured_images/{file_path.name}"
    except Exception:
        return value


async def read_image_binary(client: httpx.AsyncClient, image: Optional[str]) -> tuple[Optional[bytes], Optional[str]]:
    value = str(image or "").strip()
    if not value or value.startswith("data:image/"):
        return None, None

    local_path = resolve_media_file_path(value)
    if local_path and local_path.exists():
        content_type = mimetypes.guess_type(str(local_path))[0] or "image/jpeg"
        try:
            return local_path.read_bytes(), content_type
        except Exception:
            return None, None

    if not re.match(r"^https?://", value, flags=re.IGNORECASE):
        return None, None

    try:
        response = await client.get(value, headers=build_image_request_headers(value))
        if response.status_code >= 400 or not response.content:
            return None, None
        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
        if not content_type.startswith("image/"):
            content_type = "image/jpeg"
        return response.content, content_type
    except Exception:
        return None, None


async def build_memfire_cover_image_value(client: httpx.AsyncClient, image: Optional[str]) -> Optional[str]:
    value = str(image or "").strip()
    if not value:
        return None
    if value.startswith("data:image/"):
        return value
    if not re.match(r"^https?://", value, flags=re.IGNORECASE) and not value.startswith("/media/"):
        return value
    try:
        content, content_type = await read_image_binary(client, value)
        if not content:
            return value
        encoded = base64.b64encode(content).decode()
        return f"data:{content_type};base64,{encoded}"
    except Exception:
        return value


async def insert_memfire_records(target_table: str, structured_records: List[dict]) -> dict:
    if target_table != "activities":
        raise HTTPException(status_code=400, detail="MemFire 目前仅支持 activities 表")

    payloads: List[Dict[str, Any]] = []
    skipped = 0
    skipped_details: List[Dict[str, Any]] = []
    resolved_city_count = 0
    resolved_category_count = 0
    unresolved_category_count = 0
    resolved_tag_record_count = 0
    unresolved_tag_count = 0
    resolution_details: List[Dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        for record in structured_records:
            if not isinstance(record, dict):
                skipped += 1
                skipped_details.append({"reason": "record_not_dict"})
                continue
            resolved_record = await resolve_activity_dimensions(record, client=client)
            resolution = resolved_record.get("_dimension_resolution") or {}
            resolution_detail = build_dimension_resolution_detail(record, resolved_record)
            city_resolution = resolution.get("city") if isinstance(resolution, dict) else None
            category_resolution = resolution.get("category") if isinstance(resolution, dict) else None
            tag_resolution = resolution.get("tags") if isinstance(resolution, dict) else None
            unresolved_tag_resolution = resolution.get("unresolved_tags") if isinstance(resolution, dict) else None
            if isinstance(city_resolution, dict) and city_resolution.get("status") == "resolved":
                resolved_city_count += 1
            if isinstance(category_resolution, dict) and category_resolution.get("status") == "resolved":
                resolved_category_count += 1
            elif isinstance(category_resolution, dict) and category_resolution.get("status") == "unresolved":
                unresolved_category_count += 1
            if isinstance(tag_resolution, list) and tag_resolution:
                resolved_tag_record_count += 1
            if isinstance(unresolved_tag_resolution, list):
                unresolved_tag_count += len(unresolved_tag_resolution)
            if isinstance(city_resolution, dict) and city_resolution.get("status") == "unresolved":
                skipped += 1
                skipped_details.append({
                    "reason": "unresolved_city",
                    **resolution_detail,
                })
                continue
            payload = build_memfire_activity_payload(resolved_record)
            if not payload:
                skipped += 1
                skipped_details.append({
                    "reason": "invalid_payload",
                    **resolution_detail,
                })
                continue
            # MemFire 远程表存在按时间派生 is_active 的逻辑；
            # 当 end_time 为空时会把 is_active 算成 NULL，导致 NOT NULL 约束失败。
            # 对仅给出开始时间的活动，回填 end_time=start_time 作为最小兜底。
            payload["end_time"] = payload.get("end_time") or payload.get("start_time")
            payload["is_active"] = True
            payload["cover_image_url"] = await build_memfire_cover_image_value(client, payload.get("cover_image_url"))
            payloads.append(payload)
            resolution_details.append({
                "reason": "ready_for_insert",
                **resolution_detail,
            })

        if not payloads:
            return {
                "target_table": target_table,
                "backend": "memfire",
                "inserted": 0,
                "skipped": skipped,
                "skipped_details": skipped_details,
                "dimension_resolution_details": resolution_details,
                "dimension_resolution_stats": {
                    "resolved_city_count": resolved_city_count,
                    "resolved_category_count": resolved_category_count,
                    "unresolved_category_count": unresolved_category_count,
                    "resolved_tag_record_count": resolved_tag_record_count,
                    "unresolved_tag_count": unresolved_tag_count,
                },
            }

        response = await client.post(
            build_memfire_url(target_table),
            headers=build_memfire_headers(),
            json=payloads,
        )

    if response.status_code >= 400:
        detail = response.text.strip() or f"MemFire 写入失败: HTTP {response.status_code}"
        raise HTTPException(status_code=400, detail=detail)

    inserted_rows = []
    try:
        inserted_rows = response.json() if response.text else []
    except Exception:
        inserted_rows = []
    return {
        "target_table": target_table,
        "backend": "memfire",
        "inserted": len(payloads),
        "skipped": skipped,
        "inserted_rows": inserted_rows,
        "skipped_details": skipped_details,
        "dimension_resolution_details": resolution_details,
        "dimension_resolution_stats": {
            "resolved_city_count": resolved_city_count,
            "resolved_category_count": resolved_category_count,
            "unresolved_category_count": unresolved_category_count,
            "resolved_tag_record_count": resolved_tag_record_count,
            "unresolved_tag_count": unresolved_tag_count,
        },
    }


async def resolve_target_table(target_table: str):
    from database.db_session import get_async_engine
    from sqlalchemy import JSON, Boolean, Column, DateTime, Integer, MetaData, String, Table, Text
    from sqlalchemy.exc import InvalidRequestError
    from sqlalchemy.sql import func

    engine = get_async_engine(config_service.get("SAVE_DATA_OPTION", "csv"))
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
                    Column("is_featured", Boolean, default=False, server_default=text("false")),
                    Column("is_active", Boolean, default=True, server_default=text("true")),
                    Column("organizer", String(255)),
                    Column("is_trending", Boolean, default=False, server_default=text("false")),
                    Column("city", String(64)),
                    Column("host", String(255)),
                    Column("created_at", DateTime, server_default=func.now()),
                    Column("updated_at", DateTime, server_default=func.now(), onupdate=func.now()),
                    extend_existing=True,
                )
                metadata.create_all(sync_conn, tables=[metadata.tables["activities"]], checkfirst=True)
                sync_conn.execute(text("ALTER TABLE activities ALTER COLUMN is_active SET DEFAULT true"))

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


async def insert_structured_records(target_table: str, structured_records: List[dict]) -> dict:
    if not structured_records:
        return {"target_table": target_table, "inserted": 0}

    if target_table == "activities" and has_memfire_config():
        return await insert_memfire_records(target_table, structured_records)

    from database.db_session import get_session
    from sqlalchemy import insert

    table = await resolve_target_table(target_table)
    inserted = 0
    async with get_session() as session:
        if session is None:
            raise HTTPException(status_code=400, detail="数据库未配置")

        for record in structured_records:
            if not isinstance(record, dict):
                continue
            payload: Dict[str, Any] = {key: value for key, value in record.items() if key in table.c}
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


def should_materialize_structured_result(quality: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(quality, dict):
        return False
    score = quality.get("score")
    try:
        return score is not None and int(score) > STRUCTURED_ACTIVITY_SCORE_THRESHOLD
    except Exception:
        return False


async def analyze_activity_request_with_runtime(req: Any) -> Dict[str, Any]:
    localized_urls = await localize_image_urls_for_runtime(getattr(req, "image_urls", None))
    runtime_req = clone_request_with_image_urls(req, localized_urls)
    return await run_structured_analysis(
        runtime_req,
        understand_images=understand_images_async,
        call_llm_for_text=call_llm_for_text_async,
        should_materialize_result=should_materialize_structured_result,
        insert_structured_records=insert_structured_records,
    )
