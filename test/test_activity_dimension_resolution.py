import asyncio
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services.dimension_resolver_service import (  # noqa: E402
    audit_tag_resolution,
    clear_dimension_cache,
    get_city_rows,
    get_tag_rows,
    resolve_activity_dimensions,
    resolve_city_from_rows,
)
from api.services.structured_runtime_service import build_dimension_resolution_detail  # noqa: E402


def test_resolve_city_from_rows_matches_label_and_suffix():
    clear_dimension_cache()
    rows = [
        {"id": "city-sh", "name": "Shanghai", "label": "上海", "slug": "shanghai"},
        {"id": "city-bj", "name": "Beijing", "label": "北京", "slug": "beijing"},
    ]

    exact = resolve_city_from_rows("上海", rows)
    assert exact is not None
    assert exact["id"] == "city-sh"
    assert exact["match_type"] == "label_exact"

    suffix = resolve_city_from_rows("上海市", rows)
    assert suffix is not None
    assert suffix["id"] == "city-sh"

    english = resolve_city_from_rows("Shanghai", rows)
    assert english is not None
    assert english["id"] == "city-sh"


def test_resolve_activity_dimensions_uses_local_dimension_rows():
    clear_dimension_cache()

    async def _run() -> None:
        async with httpx.AsyncClient(timeout=5) as client:
            city_rows = await get_city_rows(client)
            tag_rows = await get_tag_rows(client)
            assert city_rows
            assert tag_rows

            resolved = await resolve_activity_dimensions(
                {
                    "title": "测试活动",
                    "city": "上海市",
                    "category": "AI教学工坊",
                    "tags": ["AI教学工坊", "创业孵化"],
                },
                client=client,
            )

        resolution = resolved.get("_dimension_resolution") or {}
        city_resolution = resolution.get("city") or {}
        category_resolution = resolution.get("category") or {}

        assert resolved.get("city_id")
        assert city_resolution.get("status") == "resolved"
        assert city_resolution.get("label") in {"上海", "Shanghai"}
        assert resolved.get("_resolved_category_tag_id")
        assert resolved.get("category") == "AI教学工坊"
        assert category_resolution.get("status") == "resolved"
        assert len(resolved.get("_resolved_tag_ids") or []) >= 1

    asyncio.run(_run())


def test_resolve_activity_dimensions_marks_unknown_city_unresolved():
    clear_dimension_cache()

    async def _run() -> None:
        async with httpx.AsyncClient(timeout=5) as client:
            resolved = await resolve_activity_dimensions(
                {
                    "title": "未知城市活动",
                    "city": "火星基地",
                },
                client=client,
            )

        resolution = resolved.get("_dimension_resolution") or {}
        city_resolution = resolution.get("city") or {}
        assert city_resolution.get("status") == "unresolved"
        assert city_resolution.get("raw_value") == "火星基地"

    asyncio.run(_run())


def test_audit_tag_resolution_reports_unresolved_tags():
    rows = [
        {"id": "tag-workshop", "name": "AI教学工坊", "slug": "workshop", "core": "讲师指导+动手实操"},
        {"id": "tag-startup", "name": "创业孵化", "slug": "startup-camp", "core": "创业资源孵化器"},
    ]

    audit = audit_tag_resolution(["AI教学工坊", "神秘内测局", "创业孵化"], rows)

    assert [item["id"] for item in audit["resolved"]] == ["tag-workshop", "tag-startup"]
    assert audit["unresolved"] == [
        {
            "status": "unresolved",
            "raw_value": "神秘内测局",
            "normalized_value": "神秘内测局",
        }
    ]


def test_build_dimension_resolution_detail_includes_category_and_tags():
    detail = build_dimension_resolution_detail(
        {
            "title": "标签活动",
            "tags": ["AI教学工坊", "创业孵化"],
        },
        {
            "title": "标签活动",
            "city_id": "city-sh",
            "category": "AI教学工坊",
            "_resolved_category_tag_id": "tag-workshop",
            "_dimension_resolution": {
                "city": {
                    "status": "resolved",
                    "id": "city-sh",
                    "label": "上海",
                    "name": "Shanghai",
                    "raw_value": "上海市",
                },
                "category": {
                    "status": "resolved",
                    "id": "tag-workshop",
                    "name": "AI教学工坊",
                    "raw_value": "AI教学工坊",
                },
                "tags": [
                    {"id": "tag-workshop", "name": "AI教学工坊", "raw_value": "AI教学工坊"},
                    {"id": "tag-startup", "name": "创业孵化", "raw_value": "创业孵化"},
                ],
                "unresolved_tags": [
                    {
                        "status": "unresolved",
                        "raw_value": "神秘内测局",
                        "normalized_value": "神秘内测局",
                    }
                ],
            },
        },
    )

    assert detail["title"] == "标签活动"
    assert detail["city"]["id"] == "city-sh"
    assert detail["category"]["id"] == "tag-workshop"
    assert detail["category"]["name"] == "AI教学工坊"
    assert detail["tags"]["count"] == 2
    assert detail["tags"]["ids"] == ["tag-workshop", "tag-startup"]
    assert detail["unresolved_tags"]["count"] == 1
    assert detail["unresolved_tags"]["raw_values"] == ["神秘内测局"]
