"""结构化任务相关的源数据读取与 payload 构造 helper。"""

import hashlib
import html
import json
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import HTTPException
import httpx
from sqlalchemy import delete, insert, select

from database.db_session import get_session
from database.models import Base
from database.webui_models import RecordReviewState, StructuredActivityRecord


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
STRUCTURED_IMAGE_CACHE_DIR = DATA_DIR / "structured_images"


def json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def first_non_empty(row: Dict[str, Any], keys: List[str]) -> Optional[str]:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def dedupe_preserve_order(items: List[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def normalize_image_candidate(value: Any) -> Optional[str]:
    text = str(value or "").strip().strip('"').strip("'")
    if not text:
        return None
    text = text.replace("\\x26amp;", "&").replace("&amp;", "&")
    text = html.unescape(text)
    if text.startswith("http://mmbiz.qpic.cn/"):
        text = "https://" + text[len("http://") :]
    return text


def prefer_local_cached_image(value: Optional[str]) -> Optional[str]:
    candidate = normalize_image_candidate(value)
    if not candidate or candidate.startswith("/media/") or candidate.startswith("data:image/"):
        return candidate
    if not re.match(r"^https?://", candidate, flags=re.IGNORECASE):
        return candidate

    cache_key = hashlib.sha1(candidate.encode("utf-8")).hexdigest()
    for suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
        file_path = STRUCTURED_IMAGE_CACHE_DIR / f"{cache_key}{suffix}"
        if file_path.exists() and file_path.stat().st_size > 0:
            return f"/media/structured_images/{file_path.name}"
    return candidate


def extract_image_urls_from_value(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        urls: List[str] = []
        for item in value:
            urls.extend(extract_image_urls_from_value(item))
        return dedupe_preserve_order(urls)
    if isinstance(value, dict):
        urls: List[str] = []
        for key in ("url", "src", "image", "image_url", "cover", "cover_url"):
            if value.get(key):
                urls.extend(extract_image_urls_from_value(value.get(key)))
        return dedupe_preserve_order(urls)

    text = str(value).strip()
    if not text:
        return []

    parsed: Any = None
    if text[:1] in ("[", "{"):
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
    if parsed is not None:
        return extract_image_urls_from_value(parsed)

    candidates: List[str] = []
    candidates.extend(
        re.findall(r'!\[[^\]]*\]\((https?://[^)\s]+)\)', text, flags=re.IGNORECASE)
    )
    candidates.extend(re.findall(r'https?://[^\s<>"\'),]+', text, flags=re.IGNORECASE))

    normalized = normalize_image_candidate(text)
    if normalized and "," not in normalized and "\n" not in normalized and (
        normalized.startswith("http://")
        or normalized.startswith("https://")
        or normalized.startswith("data:image/")
    ):
        candidates.append(normalized)
    elif "," in text:
        for part in text.split(","):
            normalized_part = normalize_image_candidate(part)
            if normalized_part and (
                normalized_part.startswith("http://")
                or normalized_part.startswith("https://")
                or normalized_part.startswith("data:image/")
            ):
                candidates.append(normalized_part)

    return dedupe_preserve_order(
        [item for item in (prefer_local_cached_image(x) for x in candidates) if item]
    )


def extract_source_image_urls(row: Dict[str, Any]) -> List[str]:
    ordered_keys = [
        "cover",
        "cover_url",
        "video_cover_url",
        "image",
        "image_url",
        "cover_image_url",
        "image_list",
        "pictures",
        "images",
        "media_urls",
    ]
    urls: List[str] = []
    for key in ordered_keys:
        urls.extend(extract_image_urls_from_value(row.get(key)))
    for key in ("content", "content_text", "text", "body"):
        urls.extend(extract_image_urls_from_value(row.get(key)))
    return dedupe_preserve_order(urls)


def build_source_analyze_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    title = first_non_empty(row, ["title", "name", "event_title"])
    link = first_non_empty(
        row,
        ["link", "note_url", "video_url", "article_url", "url", "tieba_link", "user_link"],
    )
    published_at = first_non_empty(
        row,
        ["published_at", "publish_time", "create_time", "created_at", "add_ts", "pub_ts"],
    )
    desc = first_non_empty(row, ["desc", "digest", "summary"])
    content = first_non_empty(row, ["content", "content_text", "text", "body"])
    image_captions = first_non_empty(row, ["image_captions", "ocr_text"])

    parts = [x for x in [title, desc, content] if x]
    merged_content = "\n\n".join(parts).strip()

    deduped_images = extract_source_image_urls(row)

    return {
        "title": title,
        "link": link,
        "published_at": published_at,
        "content": merged_content,
        "image_captions": image_captions,
        "primary_image_url": deduped_images[0] if deduped_images else None,
        "image_urls": deduped_images or None,
    }


async def load_manual_review_statuses(
    source_table: str,
    ids: List[int],
    *,
    ensure_review_state_table: Optional[Callable[[], Awaitable[None]]] = None,
) -> Dict[int, str]:
    if not ids:
        return {}
    if ensure_review_state_table is not None:
        await ensure_review_state_table()

    async with get_session() as session:
        if session is None:
            return {}
        rows = (
            await session.execute(
                select(
                    RecordReviewState.source_record_id,
                    RecordReviewState.manual_review_status,
                ).where(
                    RecordReviewState.source_table == source_table,
                    RecordReviewState.source_record_id.in_(ids),
                )
            )
        ).all()
    return {int(record_id): str(status or "pending") for record_id, status in rows}


async def fetch_source_rows(source_table: str, ids: List[int]) -> List[Dict[str, Any]]:
    if not ids:
        return []

    table = Base.metadata.tables.get(source_table)
    if table is None:
        raise HTTPException(status_code=404, detail="source table not found")

    async with get_session() as session:
        if session is None:
            raise HTTPException(status_code=400, detail="数据库未配置")
        rows = (
            await session.execute(select(table).where(table.c.id.in_(ids)))
        ).mappings().all()
    return [dict(row) for row in rows]


def build_structured_cache_payloads(
    source_table: str,
    source_record_id: int,
    source_payload: Dict[str, Any],
    manual_status: str,
    analysis: Dict[str, Any],
) -> List[Dict[str, Any]]:
    quality = analysis.get("quality") or {}
    events = analysis.get("events") or []
    structured_records = analysis.get("structured_records") or []
    result: List[Dict[str, Any]] = []

    for index, item in enumerate(structured_records):
        raw_event = events[index] if index < len(events) and isinstance(events[index], dict) else {}
        image = (
            item.get("image")
            or item.get("image_url")
            or item.get("cover_image_url")
            or raw_event.get("image")
            or source_payload.get("primary_image_url")
            or (source_payload.get("image_urls") or [None])[0]
        )
        result.append(
            {
                "source_table": source_table,
                "source_record_id": source_record_id,
                "event_index": index,
                "source_title": source_payload.get("title") or "",
                "source_link": source_payload.get("link") or "",
                "source_published_at": source_payload.get("published_at") or "",
                "title": item.get("title"),
                "link": item.get("original_link") or source_payload.get("link"),
                "description": item.get("description"),
                "core_value": (item.get("highlights") or [None])[0],
                "start_time": item.get("start_time"),
                "end_time": item.get("end_time"),
                "city": item.get("city"),
                "address": item.get("location_name"),
                "host": item.get("host") or item.get("organizer"),
                "image": image,
                "quality_decision": quality.get("decision") or "",
                "quality_score": quality.get("score"),
                "quality_reason": quality.get("reason") or "",
                "manual_review_status": manual_status or "pending",
                "used_fallback": bool(analysis.get("used_fallback", False)),
                "raw_event_json": json_text(raw_event),
                "raw_structured_json": json_text(item),
            }
        )
    return result


async def backfill_structured_row_images(
    session: Any,
    rows: List[Any],
    *,
    cache_image_locally: Callable[[httpx.AsyncClient, Any], Awaitable[Optional[str]]],
) -> None:
    if not rows:
        return

    ids_by_table: Dict[str, List[int]] = {}
    row_map: Dict[str, Dict[int, List[Any]]] = {}
    updated = False

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for row in rows:
            current_image = str(getattr(row, "image", None) or "").strip()
            if current_image:
                localized_image = await cache_image_locally(client, current_image)
                if localized_image and localized_image != current_image:
                    row.image = localized_image
                    updated = True
                continue

            source_table = str(getattr(row, "source_table", "") or "").strip()
            source_record_id = getattr(row, "source_record_id", None)
            if not source_table or source_record_id is None:
                continue

            record_id = int(source_record_id)
            ids_by_table.setdefault(source_table, []).append(record_id)
            row_map.setdefault(source_table, {}).setdefault(record_id, []).append(row)

        for source_table, ids in ids_by_table.items():
            table = Base.metadata.tables.get(source_table)
            if table is None or not ids:
                continue

            source_rows = (
                await session.execute(select(table).where(table.c.id.in_(sorted(set(ids)))))
            ).mappings().all()
            for source_row in source_rows:
                payload = build_source_analyze_payload(dict(source_row))
                image = payload.get("primary_image_url")
                if not image:
                    continue
                localized_image = await cache_image_locally(client, image) or image
                for target_row in row_map.get(source_table, {}).get(int(source_row["id"]), []):
                    target_row.image = localized_image
                    updated = True

    if updated:
        await session.commit()


async def replace_structured_cache_rows(
    source_table: str,
    source_record_ids: List[int],
    payloads: List[Dict[str, Any]],
    *,
    ensure_review_state_table: Optional[Callable[[], Awaitable[None]]] = None,
    cache_image_locally: Callable[[httpx.AsyncClient, Any], Awaitable[Optional[str]]],
) -> None:
    if ensure_review_state_table is not None:
        await ensure_review_state_table()

    async with get_session() as session:
        if session is None:
            raise HTTPException(status_code=400, detail="数据库未配置")

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            for payload in payloads:
                payload["image"] = await cache_image_locally(client, payload.get("image")) or payload.get("image")

        if source_record_ids:
            await session.execute(
                delete(StructuredActivityRecord).where(
                    StructuredActivityRecord.source_table == source_table,
                    StructuredActivityRecord.source_record_id.in_(source_record_ids),
                )
            )

        for payload in payloads:
            await session.execute(insert(StructuredActivityRecord).values(**payload))
        await session.commit()
