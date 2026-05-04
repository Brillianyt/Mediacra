# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/api/routers/data.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#
# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

import os
import json
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from api.schemas.common import ok

router = APIRouter(prefix="/data", tags=["data"])

# Data directory
DATA_DIR = Path(__file__).parent.parent.parent / "data"


def get_file_info(file_path: Path) -> dict:
    """Get file information"""
    stat = file_path.stat()
    record_count = None

    # Try to get record count
    try:
        if file_path.suffix == ".json":
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    record_count = len(data)
        elif file_path.suffix == ".csv":
            with open(file_path, "r", encoding="utf-8") as f:
                record_count = sum(1 for _ in f) - 1  # Subtract header row
    except Exception:
        pass

    return {
        "name": file_path.name,
        "path": str(file_path.relative_to(DATA_DIR)),
        "size": stat.st_size,
        "modified_at": stat.st_mtime,
        "record_count": record_count,
        "type": file_path.suffix[1:] if file_path.suffix else "unknown"
    }


@router.get("/files")
async def list_data_files(platform: Optional[str] = None, file_type: Optional[str] = None):
    """Get data file list"""
    if not DATA_DIR.exists():
        return ok({"files": []})

    files = []
    supported_extensions = {".json", ".csv", ".xlsx", ".xls"}

    for root, dirs, filenames in os.walk(DATA_DIR):
        root_path = Path(root)
        for filename in filenames:
            file_path = root_path / filename
            if file_path.suffix.lower() not in supported_extensions:
                continue

            # Platform filter
            if platform:
                rel_path = str(file_path.relative_to(DATA_DIR))
                if platform.lower() not in rel_path.lower():
                    continue

            # Type filter
            if file_type and file_path.suffix[1:].lower() != file_type.lower():
                continue

            try:
                files.append(get_file_info(file_path))
            except Exception:
                continue

    # Sort by modification time (newest first)
    files.sort(key=lambda x: x["modified_at"], reverse=True)

    return ok({"files": files})


@router.get("/files/{file_path:path}")
async def get_file_content(file_path: str, preview: bool = True, limit: int = 100):
    """Get file content or preview"""
    full_path = DATA_DIR / file_path

    if not full_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    if not full_path.is_file():
        raise HTTPException(status_code=400, detail="Not a file")

    # Security check: ensure within DATA_DIR
    try:
        full_path.resolve().relative_to(DATA_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")

    if preview:
        # Return preview data
        try:
            if full_path.suffix == ".json":
                with open(full_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return ok({"data": data[:limit], "total": len(data)})
                    return ok({"data": data, "total": 1})
            elif full_path.suffix == ".csv":
                import csv
                with open(full_path, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    rows = []
                    for i, row in enumerate(reader):
                        if i >= limit:
                            break
                        rows.append(row)
                    # Re-read to get total count
                    f.seek(0)
                    total = sum(1 for _ in f) - 1
                    return ok({"data": rows, "total": total})
            elif full_path.suffix.lower() in (".xlsx", ".xls"):
                import pandas as pd
                # Read first limit rows
                df = pd.read_excel(full_path, nrows=limit)
                # Get total row count (only read first column to save memory)
                df_count = pd.read_excel(full_path, usecols=[0])
                total = len(df_count)
                # Convert to list of dictionaries, handle NaN values
                rows = df.where(pd.notnull(df), None).to_dict(orient='records')
                return ok({
                    "data": rows,
                    "total": total,
                    "columns": list(df.columns)
                })
            else:
                raise HTTPException(status_code=400, detail="Unsupported file type for preview")
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON file")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    else:
        # Return file download
        return FileResponse(
            path=full_path,
            filename=full_path.name,
            media_type="application/octet-stream"
        )


@router.get("/download/{file_path:path}")
async def download_file(file_path: str):
    """Download file"""
    full_path = DATA_DIR / file_path

    if not full_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    if not full_path.is_file():
        raise HTTPException(status_code=400, detail="Not a file")

    # Security check
    try:
        full_path.resolve().relative_to(DATA_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")

    return FileResponse(
        path=full_path,
        filename=full_path.name,
        media_type="application/octet-stream"
    )


@router.get("/images/wechat")
async def list_wechat_images(nickname: str, article_id: str, limit: int = 50):
    """
    列出某篇微信公众号文章在本地已保存的图片，返回可直接用于 <img> 的相对 URL 列表。
    目录结构：data/<WECHAT_IMAGE_SAVE_DIR>/<公众号昵称>/<article_id>/*.jpg|*.png...
    """
    if not nickname or not article_id:
        raise HTTPException(status_code=400, detail="缺少参数 nickname 或 article_id")

    # Load config dynamically to get WECHAT_IMAGE_SAVE_DIR
    try:
        import config
        img_dir_rel = getattr(config, "WECHAT_IMAGE_SAVE_DIR", "wechat/images")
    except Exception:
        img_dir_rel = "wechat/images"

    # Sanitize nickname to align with saver
    safe_nick = nickname
    try:
        from store.wechat.wechat_store_media import _sanitize_filename as _san
        safe_nick = _san(nickname)
    except Exception:
        safe_nick = nickname.strip().replace("/", "_").replace("\\", "_").strip(" .")

    # Build absolute directory
    base_dir = DATA_DIR / Path(img_dir_rel)
    target_dir = base_dir / safe_nick / str(article_id).strip()
    if not target_dir.exists() or not target_dir.is_dir():
        return ok({"items": []})

    exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
    files = []
    try:
        for p in sorted(target_dir.iterdir()):
            if p.is_file() and p.suffix.lower() in exts:
                # Build web URL mounted at /media
                rel_path = p.relative_to(DATA_DIR).as_posix()
                files.append(f"/media/{rel_path}")
                if len(files) >= max(1, min(limit, 200)):
                    break
    except Exception:
        files = []

    return ok({"items": files})


@router.get("/stats")
async def get_data_stats():
    """Get data statistics"""
    if not DATA_DIR.exists():
        return ok({"total_files": 0, "total_size": 0, "by_platform": {}, "by_type": {}})

    stats = {
        "total_files": 0,
        "total_size": 0,
        "by_platform": {},
        "by_type": {}
    }

    supported_extensions = {".json", ".csv", ".xlsx", ".xls"}

    for root, dirs, filenames in os.walk(DATA_DIR):
        root_path = Path(root)
        for filename in filenames:
            file_path = root_path / filename
            if file_path.suffix.lower() not in supported_extensions:
                continue

            try:
                stat = file_path.stat()
                stats["total_files"] += 1
                stats["total_size"] += stat.st_size

                # Statistics by type
                file_type = file_path.suffix[1:].lower()
                stats["by_type"][file_type] = stats["by_type"].get(file_type, 0) + 1

                # Statistics by platform (inferred from path)
                rel_path = str(file_path.relative_to(DATA_DIR))
                for platform in ["xhs", "dy", "ks", "bili", "wb", "tieba", "zhihu"]:
                    if platform in rel_path.lower():
                        stats["by_platform"][platform] = stats["by_platform"].get(platform, 0) + 1
                        break
            except Exception:
                continue

    return ok(stats)


# -------------------- DB data browsing (sqlite/db/postgres) --------------------


def _is_db_mode(save_option: str) -> bool:
    return save_option in ("sqlite", "db", "postgres")


async def _ensure_review_state_table() -> None:
    import database.webui_models  # noqa: F401
    from database.db_session import create_tables

    await create_tables()


async def _cleanup_deleted_source_related_rows(session: Any, source_table: str, record_ids: List[int]) -> Dict[str, int]:
    from sqlalchemy import delete
    from database.webui_models import RecordReviewState, StructuredActivityRecord

    normalized_ids = sorted({int(record_id) for record_id in record_ids if record_id is not None})
    if not normalized_ids:
        return {"structured_deleted": 0, "review_state_deleted": 0}

    review_result = await session.execute(
        delete(RecordReviewState).where(
            RecordReviewState.source_table == source_table,
            RecordReviewState.source_record_id.in_(normalized_ids),
        )
    )
    structured_result = await session.execute(
        delete(StructuredActivityRecord).where(
            StructuredActivityRecord.source_table == source_table,
            StructuredActivityRecord.source_record_id.in_(normalized_ids),
        )
    )
    return {
        "structured_deleted": int(structured_result.rowcount or 0),
        "review_state_deleted": int(review_result.rowcount or 0),
    }


@router.get("/db/tables")
async def list_db_tables(platform: Optional[str] = None):
    """列出数据库表及记录数（仅在 DB 模式可用）。"""
    import config
    if not _is_db_mode(getattr(config, "SAVE_DATA_OPTION", "csv")):
        raise HTTPException(status_code=400, detail="数据库未配置")

    from sqlalchemy import select, func
    from database.db_session import get_session
    from database.models import Base
    import database.webui_models  # noqa: F401

    tables = Base.metadata.tables

    # 默认仅返回主要内容表，避免 WebUI 表干扰
    platform_to_table = {
        "xhs": "xhs_note",
        "dy": "douyin_aweme",
        "ks": "kuaishou_video",
        "bili": "bilibili_video",
        "wb": "weibo_note",
        "tieba": "tieba_note",
        "zhihu": "zhihu_content",
        "wechat": "wechat_article",
    }

    selected = []
    if platform:
        tname = platform_to_table.get(platform)
        if tname:
            selected = [tname]
    if not selected:
        selected = list(platform_to_table.values())

    result = []
    async with get_session() as session:
        if session is None:
            raise HTTPException(status_code=400, detail="数据库未配置")

        for tname in selected:
            table = tables.get(tname)
            if table is None:
                continue
            try:
                total = (await session.execute(select(func.count()).select_from(table))).scalar() or 0
            except Exception:
                total = 0
            result.append({"table": tname, "count": total})

    return ok({"items": result})



from pydantic import BaseModel
from typing import List, Any, Dict

class UpdateRecordRequest(BaseModel):
    updates: Dict[str, Any]

class BatchDeleteRequest(BaseModel):
    ids: List[int]

class ManualReviewRequest(BaseModel):
    manual_review_status: str
    review_note: Optional[str] = None

@router.get("/db/records")
async def get_db_records(
    table: str, 
    limit: int = 100, 
    offset: int = 0,
    keyword: Optional[str] = None,
    min_ai_score: Optional[int] = None,
    max_ai_score: Optional[int] = None,
    ai_status: Optional[int] = None,
    is_spam: Optional[int] = None,
    manual_review_status: Optional[str] = None,
    structured_only: bool = False,
    sort_by: Optional[str] = None,
    sort_desc: bool = True
):
    """读取指定表的记录预览（仅在 DB 模式可用）。支持多维度过滤。"""
    import config
    if not _is_db_mode(getattr(config, "SAVE_DATA_OPTION", "csv")):
        raise HTTPException(status_code=400, detail="数据库未配置")

    if not table or not table.replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="invalid table")
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))

    from sqlalchemy import select, or_, desc, asc, exists, and_
    from database.db_session import get_session
    from database.models import Base
    from database.webui_models import RecordReviewState

    await _ensure_review_state_table()

    tbl = Base.metadata.tables.get(table)
    if tbl is None:
        raise HTTPException(status_code=404, detail="table not found")

    normalized_review_status = (manual_review_status or "").strip().lower() or None
    valid_review_status = {"pending", "passed", "rejected"}
    if normalized_review_status and normalized_review_status not in valid_review_status:
        raise HTTPException(status_code=400, detail="invalid manual_review_status")

    async with get_session() as session:
        if session is None:
            raise HTTPException(status_code=400, detail="数据库未配置")
            
        stmt = select(tbl)
        
        # 动态过滤条件
        if keyword:
            search_cols = []
            for col_name in ['title', 'desc', 'content', 'content_text']:
                if col_name in tbl.c:
                    search_cols.append(tbl.c[col_name].ilike(f"%{keyword}%"))
            if search_cols:
                stmt = stmt.where(or_(*search_cols))
                
        if min_ai_score is not None and 'ai_score' in tbl.c:
            stmt = stmt.where(tbl.c['ai_score'] >= min_ai_score)
        if max_ai_score is not None and 'ai_score' in tbl.c:
            stmt = stmt.where(tbl.c['ai_score'] <= max_ai_score)
        if ai_status is not None and 'ai_status' in tbl.c:
            stmt = stmt.where(tbl.c['ai_status'] == ai_status)
        if is_spam is not None and 'is_spam' in tbl.c:
            stmt = stmt.where(tbl.c['is_spam'] == is_spam)

        review_base = and_(
            RecordReviewState.source_table == table,
            RecordReviewState.source_record_id == tbl.c.id,
        )
        review_status_exists = lambda status: exists(  # noqa: E731
            select(1).select_from(RecordReviewState).where(
                and_(review_base, RecordReviewState.manual_review_status == status)
            )
        )
        review_any_exists = exists(
            select(1).select_from(RecordReviewState).where(review_base)
        )

        if normalized_review_status == "pending":
            stmt = stmt.where(or_(~review_any_exists, review_status_exists("pending")))
        elif normalized_review_status in {"passed", "rejected"}:
            stmt = stmt.where(review_status_exists(normalized_review_status))

        if structured_only:
            structured_clauses = [review_status_exists("passed")]
            if "ai_score" in tbl.c:
                structured_clauses.append(tbl.c["ai_score"] >= 60)
            if "is_spam" in tbl.c:
                stmt = stmt.where(or_(tbl.c["is_spam"].is_(None), tbl.c["is_spam"] == 0))
            stmt = stmt.where(or_(*structured_clauses))
            
        # 排序
        if sort_by and sort_by in tbl.c:
            order_col = tbl.c[sort_by]
            stmt = stmt.order_by(desc(order_col) if sort_desc else asc(order_col))
        elif 'id' in tbl.c:
            stmt = stmt.order_by(desc(tbl.c['id']))
            
        # 分页
        stmt = stmt.offset(offset).limit(limit)
        
        rows = (await session.execute(stmt)).mappings().all()
        records = [dict(r) for r in rows]

        record_ids = [r.get("id") for r in records if r.get("id") is not None]
        review_map: Dict[int, Dict[str, Any]] = {}
        if record_ids:
            review_rows = (
                await session.execute(
                    select(
                        RecordReviewState.source_record_id,
                        RecordReviewState.manual_review_status,
                        RecordReviewState.review_note,
                        RecordReviewState.updated_at,
                    ).where(
                        RecordReviewState.source_table == table,
                        RecordReviewState.source_record_id.in_(record_ids),
                    )
                )
            ).all()
            review_map = {
                row.source_record_id: {
                    "manual_review_status": row.manual_review_status,
                    "review_note": row.review_note or "",
                    "manual_review_updated_at": row.updated_at.isoformat() if row.updated_at else None,
                }
                for row in review_rows
            }
        for record in records:
            review_state = review_map.get(record.get("id"), {})
            record["manual_review_status"] = review_state.get("manual_review_status", "pending")
            record["review_note"] = review_state.get("review_note", "")
            record["manual_review_updated_at"] = review_state.get("manual_review_updated_at")
        
        # 为了懒加载，也返回是否有下一页
        has_more = len(records) == limit
        
        return ok({"records": records, "table": table, "limit": limit, "offset": offset, "has_more": has_more})

@router.put("/db/records/{table}/{record_id}")
async def update_db_record(table: str, record_id: int, req: UpdateRecordRequest):
    """更新单条记录，如修改标签、状态、修正内容等"""
    from sqlalchemy import update
    from database.db_session import get_session
    from database.models import Base
    
    tbl = Base.metadata.tables.get(table)
    if tbl is None:
        raise HTTPException(status_code=404, detail="table not found")
        
    async with get_session() as session:
        if session is None:
            raise HTTPException(status_code=400, detail="数据库未配置")
        
        # 安全过滤 update 字段
        valid_updates = {k: v for k, v in req.updates.items() if k in tbl.c}
        if not valid_updates:
            return ok({"updated": 0})
            
        stmt = update(tbl).where(tbl.c.id == record_id).values(**valid_updates)
        result = await session.execute(stmt)
        await session.commit()
        return ok({"updated": result.rowcount})

@router.put("/db/records/{table}/{record_id}/manual_review")
async def update_record_manual_review(table: str, record_id: int, req: ManualReviewRequest):
    """更新单条记录的人工审核状态"""
    from sqlalchemy import select
    from database.db_session import get_session
    from database.models import Base
    from database.webui_models import RecordReviewState

    await _ensure_review_state_table()

    tbl = Base.metadata.tables.get(table)
    if tbl is None:
        raise HTTPException(status_code=404, detail="table not found")

    status = (req.manual_review_status or "").strip().lower()
    if status not in {"pending", "passed", "rejected"}:
        raise HTTPException(status_code=400, detail="invalid manual_review_status")

    async with get_session() as session:
        if session is None:
            raise HTTPException(status_code=400, detail="数据库未配置")

        exists_stmt = select(tbl.c.id).where(tbl.c.id == record_id).limit(1)
        exists_row = (await session.execute(exists_stmt)).scalar()
        if exists_row is None:
            raise HTTPException(status_code=404, detail="record not found")

        state = (
            await session.execute(
                select(RecordReviewState).where(
                    RecordReviewState.source_table == table,
                    RecordReviewState.source_record_id == record_id,
                ).limit(1)
            )
        ).scalar_one_or_none()

        if state is None:
            state = RecordReviewState(
                source_table=table,
                source_record_id=record_id,
                manual_review_status=status,
                review_note=(req.review_note or "").strip(),
            )
            session.add(state)
        else:
            state.manual_review_status = status
            state.review_note = (req.review_note or "").strip()

        await session.flush()
        await session.refresh(state)
        return ok({
            "source_table": table,
            "source_record_id": record_id,
            "manual_review_status": state.manual_review_status,
            "review_note": state.review_note or "",
            "manual_review_updated_at": state.updated_at.isoformat() if state.updated_at else None,
        })

@router.delete("/db/records/{table}/{record_id}")
async def delete_db_record(table: str, record_id: int):
    """物理删除单条脏数据"""
    from sqlalchemy import delete
    from database.db_session import get_session
    from database.models import Base
    
    tbl = Base.metadata.tables.get(table)
    if tbl is None:
        raise HTTPException(status_code=404, detail="table not found")
        
    async with get_session() as session:
        if session is None:
            raise HTTPException(status_code=400, detail="数据库未配置")
            
        stmt = delete(tbl).where(tbl.c.id == record_id)
        result = await session.execute(stmt)
        cleanup_result = await _cleanup_deleted_source_related_rows(session, table, [record_id])
        await session.commit()
        return ok({
            "deleted": int(result.rowcount or 0),
            "structured_deleted": cleanup_result["structured_deleted"],
            "review_state_deleted": cleanup_result["review_state_deleted"],
        })

@router.post("/db/records/{table}/batch_delete")
async def batch_delete_db_records(table: str, req: BatchDeleteRequest):
    """批量物理删除数据"""
    from sqlalchemy import delete
    from database.db_session import get_session
    from database.models import Base
    
    tbl = Base.metadata.tables.get(table)
    if tbl is None:
        raise HTTPException(status_code=404, detail="table not found")
        
    async with get_session() as session:
        if session is None:
            raise HTTPException(status_code=400, detail="数据库未配置")
        record_ids = [int(record_id) for record_id in (req.ids or []) if record_id is not None]
        if not record_ids:
            return ok({"deleted": 0, "structured_deleted": 0, "review_state_deleted": 0})

        stmt = delete(tbl).where(tbl.c.id.in_(record_ids))
        result = await session.execute(stmt)
        cleanup_result = await _cleanup_deleted_source_related_rows(session, table, record_ids)
        await session.commit()
        return ok({
            "deleted": int(result.rowcount or 0),
            "structured_deleted": cleanup_result["structured_deleted"],
            "review_state_deleted": cleanup_result["review_state_deleted"],
        })



@router.get("/db/stats")
async def get_db_stats():
    """数据库数据概览（仅在 DB 模式可用）。"""
    import config
    if not _is_db_mode(getattr(config, "SAVE_DATA_OPTION", "csv")):
        raise HTTPException(status_code=400, detail="数据库未配置")

    items = (await list_db_tables())
    rows = items.get("data", {}).get("items", [])
    total = sum((r.get("count") or 0) for r in rows)
    return ok({"total_records": total, "by_table": rows})
