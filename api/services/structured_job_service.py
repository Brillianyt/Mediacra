# -*- coding: utf-8 -*-
"""结构化生成任务服务。"""

import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select, text
from sqlalchemy import inspect as sqlalchemy_inspect

from api.services.config_service import config_service
from database.db_session import create_tables, get_async_engine, get_session
from database.webui_models import StructuredJob, StructuredJobItem


class StructuredJobService:
    """管理结构化任务的创建、查询、取消和进度更新。"""

    def __init__(self) -> None:
        self._task_handles: Dict[int, asyncio.Task] = {}

    async def ensure_worker_columns(self) -> None:
        import database.webui_models  # noqa: F401

        await create_tables()
        engine = get_async_engine(config_service.get("SAVE_DATA_OPTION", "csv"))
        if not engine:
            return

        def _get_columns(sync_conn) -> set:
            inspector = sqlalchemy_inspect(sync_conn)
            return {col["name"] for col in inspector.get_columns("webui_structured_job")}

        dialect = engine.dialect.name
        datetime_type = "TIMESTAMP" if dialect == "postgresql" else "DATETIME"
        column_definitions = [
            ("worker_id", "VARCHAR(128)"),
            ("heartbeat_at", datetime_type),
            ("lease_until", datetime_type),
            ("attempt_count", "INTEGER DEFAULT 0"),
            ("next_run_at", datetime_type),
            ("last_error_stage", "VARCHAR(64)"),
        ]

        async with engine.begin() as conn:
            try:
                columns = await conn.run_sync(_get_columns)
            except Exception:
                return
            for column_name, ddl in column_definitions:
                if column_name in columns:
                    continue
                await conn.execute(
                    text(f"ALTER TABLE webui_structured_job ADD COLUMN {column_name} {ddl}")
                )

    async def create_job(
        self,
        *,
        source_table: str,
        ids: List[int],
        target_table: str = "activities",
        custom_extract_prompt: Optional[str] = None,
        custom_quality_prompt: Optional[str] = None,
        trigger_type: str = "manual",
    ) -> StructuredJob:
        await self.ensure_worker_columns()
        normalized_ids = [int(record_id) for record_id in ids or []]
        async with get_session() as session:
            if session is None:
                raise RuntimeError("数据库未配置")

            job = StructuredJob(
                source_table=source_table,
                target_table=target_table or "activities",
                trigger_type=trigger_type or "manual",
                status="pending",
                requested_ids=normalized_ids,
                total_count=len(normalized_ids),
                current_stage="queued",
                custom_extract_prompt=custom_extract_prompt,
                custom_quality_prompt=custom_quality_prompt,
                result_summary={},
                attempt_count=0,
            )
            session.add(job)
            await session.flush()

            for record_id in normalized_ids:
                session.add(
                    StructuredJobItem(
                        job_id=job.id,
                        source_record_id=record_id,
                        status="pending",
                        current_stage="queued",
                        result_payload={},
                    )
                )

            await session.flush()
            await session.refresh(job)
            return job

    def attach_task(self, job_id: int, task: asyncio.Task) -> None:
        self._task_handles[job_id] = task

    def detach_task(self, job_id: int) -> None:
        self._task_handles.pop(job_id, None)

    async def mark_job_running(self, job_id: int, *, current_stage: str) -> None:
        async with get_session() as session:
            if session is None:
                return
            job = await session.get(StructuredJob, job_id)
            if not job:
                return
            if job.status == "cancel_requested":
                job.status = "cancelled"
                job.current_stage = current_stage
                job.finished_at = datetime.now()
                job.lease_until = None
                return
            job.status = "running"
            job.current_stage = current_stage
            job.started_at = job.started_at or datetime.now()
            job.last_error_stage = None

    async def mark_job_finished(
        self,
        job_id: int,
        *,
        status: str,
        current_stage: str,
        error_message: Optional[str] = None,
        result_summary: Optional[Dict[str, Any]] = None,
    ) -> None:
        async with get_session() as session:
            if session is None:
                return
            job = await session.get(StructuredJob, job_id)
            if not job:
                return
            job.status = status
            job.current_stage = current_stage
            job.error_message = error_message
            job.finished_at = datetime.now()
            job.lease_until = None
            job.heartbeat_at = datetime.now()
            if error_message:
                job.last_error_stage = current_stage
            if result_summary is not None:
                job.result_summary = result_summary

    async def claim_next_job(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 300,
    ) -> Optional[StructuredJob]:
        await self.ensure_worker_columns()
        now = datetime.now()
        lease_until = now + timedelta(seconds=max(30, int(lease_seconds or 300)))

        async with get_session() as session:
            if session is None:
                return None

            result = await session.execute(
                select(StructuredJob.id)
                .where(StructuredJob.status == "pending")
                .order_by(StructuredJob.created_at.asc())
                .limit(10)
            )
            candidate_ids = [int(job_id) for job_id in result.scalars().all()]
            for candidate_id in candidate_ids:
                job = await session.get(StructuredJob, candidate_id)
                if not job or str(job.status or "") != "pending":
                    continue
                if job.next_run_at and job.next_run_at > now:
                    continue
                if job.lease_until and job.lease_until > now:
                    continue

                job.status = "running"
                job.current_stage = "claimed"
                job.worker_id = worker_id
                job.heartbeat_at = now
                job.lease_until = lease_until
                job.attempt_count = int(job.attempt_count or 0) + 1
                job.started_at = job.started_at or now
                await session.flush()
                await session.refresh(job)
                return job
        return None

    async def heartbeat_job(
        self,
        job_id: int,
        *,
        worker_id: str,
        lease_seconds: int = 300,
    ) -> bool:
        now = datetime.now()
        lease_until = now + timedelta(seconds=max(30, int(lease_seconds or 300)))
        async with get_session() as session:
            if session is None:
                return False
            job = await session.get(StructuredJob, job_id)
            if not job:
                return False
            if str(job.worker_id or "") != str(worker_id):
                return False
            if str(job.status or "") not in {"running", "cancel_requested"}:
                return False
            job.heartbeat_at = now
            job.lease_until = lease_until
            await session.flush()
            return True

    async def recover_stale_jobs(
        self,
        *,
        max_attempts: int = 3,
        retry_delay_seconds: int = 10,
    ) -> int:
        await self.ensure_worker_columns()
        now = datetime.now()
        recovered_count = 0

        async with get_session() as session:
            if session is None:
                return 0

            result = await session.execute(
                select(StructuredJob).where(
                    StructuredJob.status.in_(["running", "cancel_requested"]),
                    StructuredJob.lease_until.is_not(None),
                    StructuredJob.lease_until < now,
                )
            )
            stale_jobs = result.scalars().all()
            for job in stale_jobs:
                if str(job.status or "") == "cancel_requested":
                    job.status = "cancelled"
                    job.current_stage = "cancelled"
                    job.error_message = "取消请求未完成，因 worker 租约过期已自动收口为已取消"
                    job.finished_at = now
                    job.worker_id = None
                    job.heartbeat_at = now
                    job.lease_until = None
                    job.last_error_stage = "cancelled"

                    item_result = await session.execute(
                        select(StructuredJobItem).where(
                            StructuredJobItem.job_id == job.id,
                            StructuredJobItem.status.in_(["pending", "running", "cancel_requested"]),
                        )
                    )
                    for item in item_result.scalars().all():
                        item.status = "cancelled"
                        item.current_stage = "cancelled"
                        item.finished_at = now
                        item.error_message = "任务已取消"

                    await self._refresh_job_counters(session, job)
                    recovered_count += 1
                    continue

                attempts = int(job.attempt_count or 0)
                expired_reason = "worker 租约过期，任务已回收"
                if attempts >= max(1, int(max_attempts or 1)):
                    job.status = "failed"
                    job.current_stage = "lease_expired"
                    job.error_message = f"{expired_reason}，且已达到最大重试次数"
                    job.finished_at = now
                else:
                    job.status = "pending"
                    job.current_stage = "queued"
                    job.error_message = expired_reason
                    job.next_run_at = now + timedelta(seconds=max(0, int(retry_delay_seconds or 0)))

                job.worker_id = None
                job.heartbeat_at = now
                job.lease_until = None
                job.last_error_stage = "lease_expired"

                item_result = await session.execute(
                    select(StructuredJobItem).where(
                        StructuredJobItem.job_id == job.id,
                        StructuredJobItem.status == "running",
                    )
                )
                for item in item_result.scalars().all():
                    if attempts >= max(1, int(max_attempts or 1)):
                        item.status = "failed"
                        item.current_stage = "lease_expired"
                        item.finished_at = now
                    else:
                        item.status = "pending"
                        item.current_stage = "queued"
                        item.finished_at = None
                    item.error_message = expired_reason

                await self._refresh_job_counters(session, job)
                recovered_count += 1

            return recovered_count

    async def mark_item_running(self, job_id: int, source_record_id: int, *, source_title: str) -> None:
        async with get_session() as session:
            if session is None:
                return
            item = await self._get_job_item(session, job_id, source_record_id)
            if not item:
                return
            item.status = "running"
            item.current_stage = "analyzing"
            item.source_title = source_title or item.source_title or ""

    async def mark_item_finished(
        self,
        job_id: int,
        source_record_id: int,
        *,
        item_status: str,
        current_stage: str,
        source_title: str = "",
        structured_count: int = 0,
        quality_score: Optional[int] = None,
        quality_decision: str = "",
        used_fallback: bool = False,
        reason: str = "",
        result_payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        async with get_session() as session:
            if session is None:
                return
            item = await self._get_job_item(session, job_id, source_record_id)
            job = await session.get(StructuredJob, job_id)
            if not item or not job:
                return

            item.status = item_status
            item.current_stage = current_stage
            item.source_title = source_title or item.source_title or ""
            item.structured_count = int(structured_count or 0)
            item.quality_score = quality_score
            item.quality_decision = quality_decision or ""
            item.used_fallback = bool(used_fallback)
            item.error_message = reason or None
            item.result_payload = result_payload or {}
            item.finished_at = datetime.now()

            await self._refresh_job_counters(session, job)

    async def mark_item_failed(
        self,
        job_id: int,
        source_record_id: int,
        *,
        source_title: str = "",
        reason: str,
        result_payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        await self.mark_item_finished(
            job_id,
            source_record_id,
            item_status="failed",
            current_stage="failed",
            source_title=source_title,
            reason=reason,
            result_payload=result_payload,
        )

    async def cancel_job(self, job_id: int) -> Optional[StructuredJob]:
        async with get_session() as session:
            if session is None:
                return None
            job = await session.get(StructuredJob, job_id)
            if not job:
                return None

            if job.status in {"success", "partial_success", "failed", "cancelled"}:
                return job

            job.status = "cancel_requested"
            job.current_stage = "cancel_requested"

            result = await session.execute(
                select(StructuredJobItem).where(
                    StructuredJobItem.job_id == job_id,
                    StructuredJobItem.status == "pending",
                )
            )
            for item in result.scalars().all():
                item.status = "cancelled"
                item.current_stage = "cancelled"
                item.error_message = "任务已取消"
                item.finished_at = datetime.now()

            await self._refresh_job_counters(session, job)

        task = self._task_handles.get(job_id)
        if task and not task.done():
            task.cancel()

        return await self.get_job(job_id)

    async def get_job(self, job_id: int) -> Optional[StructuredJob]:
        async with get_session() as session:
            if session is None:
                return None
            return await session.get(StructuredJob, job_id)

    async def get_job_detail(self, job_id: int) -> Optional[Dict[str, Any]]:
        async with get_session() as session:
            if session is None:
                return None
            job = await session.get(StructuredJob, job_id)
            if not job:
                return None

            result = await session.execute(
                select(StructuredJobItem)
                .where(StructuredJobItem.job_id == job_id)
                .order_by(StructuredJobItem.id.asc())
            )
            items = result.scalars().all()
            return {
                "job": job,
                "items": items,
            }

    async def list_jobs(
        self,
        *,
        source_table: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Tuple[List[StructuredJob], int]:
        async with get_session() as session:
            if session is None:
                return [], 0

            base_stmt = select(StructuredJob)
            count_stmt = select(func.count()).select_from(StructuredJob)

            if source_table:
                base_stmt = base_stmt.where(StructuredJob.source_table == source_table)
                count_stmt = count_stmt.where(StructuredJob.source_table == source_table)
            if status:
                base_stmt = base_stmt.where(StructuredJob.status == status)
                count_stmt = count_stmt.where(StructuredJob.status == status)

            total = int((await session.execute(count_stmt)).scalar() or 0)
            rows = (
                await session.execute(
                    base_stmt.order_by(StructuredJob.created_at.desc())
                    .offset(max(0, int(offset or 0)))
                    .limit(max(1, min(int(limit or 20), 100)))
                )
            ).scalars().all()
            return rows, total

    async def is_cancel_requested(self, job_id: int) -> bool:
        async with get_session() as session:
            if session is None:
                return False
            job = await session.get(StructuredJob, job_id)
            if not job:
                return False
            return job.status in {"cancel_requested", "cancelled"}

    @staticmethod
    async def _get_job_item(session: Any, job_id: int, source_record_id: int) -> Optional[StructuredJobItem]:
        result = await session.execute(
            select(StructuredJobItem).where(
                StructuredJobItem.job_id == job_id,
                StructuredJobItem.source_record_id == source_record_id,
            )
        )
        return result.scalars().first()

    @staticmethod
    async def _refresh_job_counters(session: Any, job: StructuredJob) -> None:
        result = await session.execute(
            select(StructuredJobItem).where(StructuredJobItem.job_id == job.id)
        )
        items = result.scalars().all()

        processed = 0
        success = 0
        failed = 0
        skipped = 0
        cancelled = 0
        for item in items:
            status = str(item.status or "")
            if status in {"saved", "skipped", "failed", "cancelled"}:
                processed += 1
            if status == "saved":
                success += 1
            elif status == "failed":
                failed += 1
            elif status == "skipped":
                skipped += 1
            elif status == "cancelled":
                cancelled += 1

        job.total_count = len(items)
        job.processed_count = processed
        job.success_count = success
        job.failed_count = failed
        job.skipped_count = skipped + cancelled


structured_job_service = StructuredJobService()
