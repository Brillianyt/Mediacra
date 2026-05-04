import csv
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATABASE_TYPE_DIR = PROJECT_ROOT / "DATABASE_TYPE"
CACHE_TTL_SECONDS = 300

_DIMENSION_CACHE: Dict[str, tuple[float, List[Dict[str, Any]]]] = {}


def clear_dimension_cache() -> None:
    _DIMENSION_CACHE.clear()


def _normalize_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("\u3000", " ")
    text = re.sub(r"\s+", "", text)
    return text.casefold()


def _normalize_city_alias(value: Any) -> List[str]:
    text = str(value or "").strip()
    if not text:
        return []
    values = [text]
    lowered = text.casefold()
    if lowered.endswith(" city"):
        values.append(text[:-5].strip())
    for suffix in ("市", "省", "特别行政区"):
        if text.endswith(suffix) and len(text) > len(suffix):
            values.append(text[: -len(suffix)].strip())
    normalized: List[str] = []
    seen: set[str] = set()
    for item in values:
        key = _normalize_text(item)
        if key and key not in seen:
            seen.add(key)
            normalized.append(key)
    return normalized


def _normalize_slug(value: Any) -> str:
    text = str(value or "").strip().casefold()
    if not text:
        return ""
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def _parse_list_value(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[，,、/\n]+", value) if item.strip()]
    return []


def _cache_get(cache_key: str) -> Optional[List[Dict[str, Any]]]:
    cached = _DIMENSION_CACHE.get(cache_key)
    if not cached:
        return None
    expires_at, rows = cached
    if time.time() >= expires_at:
        _DIMENSION_CACHE.pop(cache_key, None)
        return None
    return rows


def _cache_set(cache_key: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    _DIMENSION_CACHE[cache_key] = (time.time() + CACHE_TTL_SECONDS, rows)
    return rows


def _read_local_csv_rows(filename: str) -> List[Dict[str, Any]]:
    file_path = DATABASE_TYPE_DIR / filename
    if not file_path.exists():
        return []
    try:
        with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except Exception:
        return []


def _has_memfire_config() -> bool:
    return bool(os.getenv("MEMFIRE_BASE_URL") and os.getenv("MEMFIRE_SERVICE_ROLE_KEY"))


def _build_memfire_headers() -> Dict[str, str]:
    token = (os.getenv("MEMFIRE_SERVICE_ROLE_KEY") or "").strip().strip('"').strip("'")
    return {
        "apikey": token,
        "Authorization": f"Bearer {token}",
    }


def _build_memfire_table_url(table_name: str) -> str:
    base_url = (os.getenv("MEMFIRE_BASE_URL") or "").strip().strip('"').strip("'").rstrip("/")
    return f"{base_url}/rest/v1/{table_name}"


async def _fetch_remote_rows(
    client: httpx.AsyncClient,
    *,
    table_name: str,
    select: str,
) -> List[Dict[str, Any]]:
    if not _has_memfire_config():
        return []
    try:
        response = await client.get(
            _build_memfire_table_url(table_name),
            headers=_build_memfire_headers(),
            params={"select": select, "limit": "2000"},
        )
        if response.status_code >= 400:
            return []
        payload = response.json()
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
    except Exception:
        return []
    return []


async def get_city_rows(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    cache_key = "cities"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    rows = await _fetch_remote_rows(client, table_name="cities", select="id,name,label,slug")
    if not rows:
        rows = _read_local_csv_rows("cities_rows.csv")
    return _cache_set(cache_key, rows)


async def get_tag_rows(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    cache_key = "tags"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    rows = await _fetch_remote_rows(client, table_name="tags", select="id,name,slug,core")
    if not rows:
        rows = _read_local_csv_rows("tags_rows.csv")
    return _cache_set(cache_key, rows)


def resolve_city_from_rows(city_text: Any, rows: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    aliases = _normalize_city_alias(city_text)
    if not aliases:
        return None

    best_match: Optional[Dict[str, Any]] = None
    best_score = -1
    ambiguous = False

    for row in rows:
        if not isinstance(row, dict):
            continue
        label = _normalize_text(row.get("label"))
        name = _normalize_text(row.get("name"))
        slug = _normalize_slug(row.get("slug"))
        for alias in aliases:
            score = -1
            match_type = ""
            if alias and alias == label:
                score = 100
                match_type = "label_exact"
            elif alias and alias == name:
                score = 90
                match_type = "name_exact"
            elif alias and alias == slug:
                score = 80
                match_type = "slug_exact"
            elif alias and alias == _normalize_slug(row.get("label")):
                score = 70
                match_type = "label_slug"
            elif alias and alias == _normalize_slug(row.get("name")):
                score = 60
                match_type = "name_slug"

            if score < 0:
                continue
            candidate = {
                "status": "resolved",
                "id": row.get("id"),
                "label": row.get("label") or row.get("name"),
                "name": row.get("name"),
                "slug": row.get("slug"),
                "match_type": match_type,
                "raw_value": str(city_text or "").strip(),
            }
            if score > best_score:
                best_match = candidate
                best_score = score
                ambiguous = False
            elif score == best_score and best_match and best_match.get("id") != candidate.get("id"):
                ambiguous = True

    if ambiguous:
        return None
    return best_match


def resolve_tags_from_rows(tag_values: Sequence[str], rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    resolved: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_tag in tag_values:
        normalized = _normalize_text(raw_tag)
        normalized_slug = _normalize_slug(raw_tag)
        if not normalized and not normalized_slug:
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            tag_id = str(row.get("id") or "").strip()
            if not tag_id or tag_id in seen_ids:
                continue
            name = _normalize_text(row.get("name"))
            slug = _normalize_slug(row.get("slug"))
            core = _normalize_text(row.get("core"))
            if normalized in {name, core} or (normalized_slug and normalized_slug == slug):
                seen_ids.add(tag_id)
                resolved.append({
                    "status": "resolved",
                    "id": tag_id,
                    "name": row.get("name"),
                    "slug": row.get("slug"),
                    "raw_value": str(raw_tag).strip(),
                })
                break
    return resolved


def audit_tag_resolution(tag_values: Sequence[str], rows: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    resolved = resolve_tags_from_rows(tag_values, rows)
    matched_raw_values = {
        _normalize_text(item.get("raw_value")) for item in resolved if isinstance(item, dict) and item.get("raw_value")
    }

    unresolved: List[Dict[str, Any]] = []
    seen_unresolved: set[str] = set()
    for raw_tag in tag_values:
        raw_value = str(raw_tag or "").strip()
        normalized = _normalize_text(raw_value)
        if not normalized or normalized in matched_raw_values or normalized in seen_unresolved:
            continue
        seen_unresolved.add(normalized)
        unresolved.append({
            "status": "unresolved",
            "raw_value": raw_value,
            "normalized_value": normalized,
        })

    return {
        "resolved": resolved,
        "unresolved": unresolved,
    }


async def resolve_activity_dimensions(
    record: Dict[str, Any],
    *,
    client: httpx.AsyncClient,
) -> Dict[str, Any]:
    enriched = dict(record)
    resolution: Dict[str, Any] = {}

    city_id = str(enriched.get("city_id") or "").strip()
    city_text = enriched.get("city")
    if city_id:
        resolution["city"] = {
            "status": "provided",
            "id": city_id,
            "raw_value": str(city_text or "").strip() or None,
        }
    elif str(city_text or "").strip():
        city_rows = await get_city_rows(client)
        city_match = resolve_city_from_rows(city_text, city_rows)
        if city_match:
            enriched["city_id"] = city_match["id"]
            resolution["city"] = city_match
        else:
            resolution["city"] = {
                "status": "unresolved",
                "raw_value": str(city_text).strip(),
            }

    raw_tags = _parse_list_value(enriched.get("tags"))
    if raw_tags:
        tag_rows = await get_tag_rows(client)
        tag_audit = audit_tag_resolution(raw_tags, tag_rows)
        resolved_tags = tag_audit["resolved"]
        unresolved_tags = tag_audit["unresolved"]
        if resolved_tags:
            resolution["tags"] = resolved_tags
            enriched["_resolved_tag_ids"] = [item["id"] for item in resolved_tags]
        if unresolved_tags:
            resolution["unresolved_tags"] = unresolved_tags

    category_id = str(enriched.get("category_id") or "").strip()
    category_text = str(enriched.get("category") or "").strip()
    if category_id:
        enriched["_resolved_category_tag_id"] = category_id
        resolution["category"] = {
            "status": "provided",
            "id": category_id,
            "raw_value": category_text or None,
        }
    elif category_text:
        tag_rows = await get_tag_rows(client)
        category_audit = audit_tag_resolution([category_text], tag_rows)
        resolved_category = category_audit["resolved"]
        unresolved_category = category_audit["unresolved"]
        if resolved_category:
            enriched["_resolved_category_tag_id"] = resolved_category[0]["id"]
            enriched["category"] = resolved_category[0].get("name") or category_text
            resolution["category"] = resolved_category[0]
        else:
            resolution["category"] = {
                "status": "unresolved",
                "raw_value": category_text,
            }
        if unresolved_category:
            resolution["unresolved_category"] = unresolved_category[0]

    enriched["_dimension_resolution"] = resolution
    return enriched
