import re

with open('api/routers/data.py', 'r', encoding='utf-8') as f:
    content = f.read()

new_endpoints = """
from pydantic import BaseModel
from typing import List, Any, Dict

class UpdateRecordRequest(BaseModel):
    updates: Dict[str, Any]

class BatchDeleteRequest(BaseModel):
    ids: List[int]

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
    sort_by: Optional[str] = None,
    sort_desc: bool = True
):
    \"\"\"读取指定表的记录预览（仅在 DB 模式可用）。支持多维度过滤。\"\"\"
    import config
    if not _is_db_mode(getattr(config, "SAVE_DATA_OPTION", "csv")):
        raise HTTPException(status_code=400, detail="数据库未配置")

    if not table or not table.replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="invalid table")
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))

    from sqlalchemy import select, or_, desc, asc
    from database.db_session import get_session
    from database.models import Base
    import database.webui_models  # noqa: F401

    tbl = Base.metadata.tables.get(table)
    if tbl is None:
        raise HTTPException(status_code=404, detail="table not found")

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
        
        # 为了懒加载，也返回是否有下一页
        has_more = len(records) == limit
        
        return ok({"records": records, "table": table, "limit": limit, "offset": offset, "has_more": has_more})

@router.put("/db/records/{table}/{record_id}")
async def update_db_record(table: str, record_id: int, req: UpdateRecordRequest):
    \"\"\"更新单条记录，如修改标签、状态、修正内容等\"\"\"
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

@router.delete("/db/records/{table}/{record_id}")
async def delete_db_record(table: str, record_id: int):
    \"\"\"物理删除单条脏数据\"\"\"
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
        await session.commit()
        return ok({"deleted": result.rowcount})

@router.post("/db/records/{table}/batch_delete")
async def batch_delete_db_records(table: str, req: BatchDeleteRequest):
    \"\"\"批量物理删除数据\"\"\"
    from sqlalchemy import delete
    from database.db_session import get_session
    from database.models import Base
    
    tbl = Base.metadata.tables.get(table)
    if tbl is None:
        raise HTTPException(status_code=404, detail="table not found")
        
    async with get_session() as session:
        if session is None:
            raise HTTPException(status_code=400, detail="数据库未配置")
            
        stmt = delete(tbl).where(tbl.c.id.in_(req.ids))
        result = await session.execute(stmt)
        await session.commit()
        return ok({"deleted": result.rowcount})
"""

pattern = re.compile(r'@router\.get\("/db/records"\)\nasync def get_db_records.*?return ok\(\{"records": records, "table": table, "limit": limit, "offset": offset\}\)', re.DOTALL)
if pattern.search(content):
    content = pattern.sub(new_endpoints, content)
    with open('api/routers/data.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("Success")
else:
    print("Pattern not found!")
