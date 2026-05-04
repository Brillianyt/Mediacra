"""结构化任务执行编排服务。"""

import asyncio
from typing import Any, Awaitable, Callable, Dict, List

from api.services.structured_job_service import structured_job_service


def serialize_structured_job(job: Any) -> Dict[str, Any]:
    return {
        "id": job.id,
        "source_table": job.source_table,
        "target_table": job.target_table,
        "trigger_type": job.trigger_type,
        "status": job.status,
        "requested_ids": job.requested_ids or [],
        "total_count": job.total_count,
        "processed_count": job.processed_count,
        "success_count": job.success_count,
        "failed_count": job.failed_count,
        "skipped_count": job.skipped_count,
        "current_stage": job.current_stage,
        "result_summary": job.result_summary or {},
        "error_message": job.error_message,
        "worker_id": job.worker_id,
        "attempt_count": job.attempt_count,
        "heartbeat_at": job.heartbeat_at.isoformat() if job.heartbeat_at else None,
        "lease_until": job.lease_until.isoformat() if job.lease_until else None,
        "next_run_at": job.next_run_at.isoformat() if job.next_run_at else None,
        "last_error_stage": job.last_error_stage,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


def serialize_structured_job_item(item: Any) -> Dict[str, Any]:
    return {
        "id": item.id,
        "job_id": item.job_id,
        "source_record_id": item.source_record_id,
        "source_title": item.source_title,
        "status": item.status,
        "current_stage": item.current_stage,
        "structured_count": item.structured_count,
        "quality_score": item.quality_score,
        "quality_decision": item.quality_decision,
        "used_fallback": item.used_fallback,
        "retry_count": item.retry_count,
        "result_payload": item.result_payload or {},
        "error_message": item.error_message,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        "finished_at": item.finished_at.isoformat() if item.finished_at else None,
    }


def _resolve_final_status(job: Any, cancelled: bool) -> str:
    if cancelled:
        return "cancelled"
    if int(job.failed_count or 0) > 0:
        if int(job.success_count or 0) > 0 or int(job.skipped_count or 0) > 0:
            return "partial_success"
        return "failed"
    return "success"


class StructuredMaterializeService:
    """负责编排结构化任务执行，但不持有业务细节实现。"""

    def start_job(self, job_id: int, **callbacks: Any) -> asyncio.Task:
        task = asyncio.create_task(self.run_job(job_id, **callbacks))
        structured_job_service.attach_task(job_id, task)
        return task

    async def run_job(
        self,
        job_id: int,
        *,
        fetch_source_rows: Callable[[str, List[int]], Awaitable[List[Dict[str, Any]]]],
        load_manual_review_statuses: Callable[[str, List[int]], Awaitable[Dict[int, str]]],
        build_source_analyze_payload: Callable[[Dict[str, Any]], Dict[str, Any]],
        analyze_activity_request: Callable[[Any], Awaitable[Dict[str, Any]]],
        make_event_analyze_request: Callable[[Any, Dict[str, Any]], Any],
        should_materialize_result: Callable[[Any], bool],
        build_structured_cache_payloads: Callable[[str, int, Dict[str, Any], str, Dict[str, Any]], List[Dict[str, Any]]],
        replace_structured_cache_rows: Callable[[str, List[int], List[Dict[str, Any]]], Awaitable[None]],
    ) -> None:
        cancelled = False
        try:
            job_detail = await structured_job_service.get_job_detail(job_id)
            if not job_detail:
                return

            job = job_detail["job"]
            items = job_detail["items"]
            requested_ids = [int(x) for x in (job.requested_ids or [])]

            await structured_job_service.mark_job_running(job_id, current_stage="loading_source_rows")
            rows = await fetch_source_rows(job.source_table, requested_ids)
            row_map = {int(row["id"]): row for row in rows if row.get("id") is not None}
            manual_status_map = await load_manual_review_statuses(job.source_table, requested_ids)

            all_payloads: List[Dict[str, Any]] = []
            replaced_source_ids: List[int] = []

            for item in items:
                record_id = int(item.source_record_id)
                if await structured_job_service.is_cancel_requested(job_id):
                    cancelled = True
                    break
                if str(item.status or "") in {"saved", "skipped", "cancelled"}:
                    continue

                row_data = row_map.get(record_id)
                if not row_data:
                    await structured_job_service.mark_item_failed(
                        job_id,
                        record_id,
                        source_title=item.source_title or "",
                        reason="源记录不存在或已被删除",
                        result_payload={},
                    )
                    continue

                payload = build_source_analyze_payload(row_data)
                source_title = payload.get("title") or ""
                await structured_job_service.mark_item_running(
                    job_id,
                    record_id,
                    source_title=source_title,
                )

                if not payload.get("content"):
                    await structured_job_service.mark_item_finished(
                        job_id,
                        record_id,
                        item_status="skipped",
                        current_stage="skipped",
                        source_title=source_title,
                        reason="记录缺少可用于结构化的正文内容",
                        result_payload={
                            "source_record_id": record_id,
                            "source_title": source_title,
                            "status": "skipped",
                        },
                    )
                    continue

                try:
                    analysis = await analyze_activity_request(
                        make_event_analyze_request(job, payload)
                    )
                    if await structured_job_service.is_cancel_requested(job_id):
                        cancelled = True
                        await structured_job_service.mark_item_finished(
                            job_id,
                            record_id,
                            item_status="cancelled",
                            current_stage="cancelled",
                            source_title=source_title,
                            reason="任务已取消",
                            result_payload={
                                "source_record_id": record_id,
                                "source_title": source_title,
                                "status": "cancelled",
                            },
                        )
                        break
                except Exception as exc:
                    await structured_job_service.mark_item_failed(
                        job_id,
                        record_id,
                        source_title=source_title,
                        reason=f"结构化分析失败: {exc}",
                        result_payload={
                            "source_record_id": record_id,
                            "source_title": source_title,
                            "status": "failed",
                        },
                    )
                    continue

                cache_payloads = build_structured_cache_payloads(
                    job.source_table,
                    record_id,
                    payload,
                    manual_status_map.get(record_id, "pending"),
                    analysis,
                )
                replaced_source_ids.append(record_id)
                if cache_payloads:
                    all_payloads.extend(cache_payloads)

                result_payload = {
                    "source_record_id": record_id,
                    "source_title": source_title,
                    "status": "saved" if cache_payloads else "skipped",
                    "structured_count": len(cache_payloads),
                    "quality_decision": (analysis.get("quality") or {}).get("decision"),
                    "quality_score": (analysis.get("quality") or {}).get("score"),
                    "used_fallback": analysis.get("used_fallback", False),
                    "reason": (
                        ((analysis.get("quality") or {}).get("reason") or "")
                        if cache_payloads
                        else "未抽取到有效结构化结果"
                    ),
                }
                await structured_job_service.mark_item_finished(
                    job_id,
                    record_id,
                    item_status=result_payload["status"],
                    current_stage="finished",
                    source_title=source_title,
                    structured_count=len(cache_payloads),
                    quality_score=result_payload["quality_score"],
                    quality_decision=result_payload["quality_decision"] or "",
                    used_fallback=bool(result_payload["used_fallback"]),
                    reason=result_payload["reason"],
                    result_payload=result_payload,
                )

            if replaced_source_ids or all_payloads:
                await structured_job_service.mark_job_running(job_id, current_stage="writing_cache")
                await replace_structured_cache_rows(job.source_table, replaced_source_ids, all_payloads)

            final_detail = await structured_job_service.get_job_detail(job_id)
            if not final_detail:
                return

            final_job = final_detail["job"]
            cancelled = cancelled or await structured_job_service.is_cancel_requested(job_id)
            await structured_job_service.mark_job_finished(
                job_id,
                status=_resolve_final_status(final_job, cancelled),
                current_stage="finished",
                result_summary={
                    "source_table": final_job.source_table,
                    "target_table": final_job.target_table,
                    "requested": len(final_job.requested_ids or []),
                    "processed": final_job.processed_count,
                    "saved": final_job.success_count,
                    "failed": final_job.failed_count,
                    "skipped": final_job.skipped_count,
                    "sources_replaced": len(replaced_source_ids),
                },
            )
        except asyncio.CancelledError:
            await structured_job_service.mark_job_finished(
                job_id,
                status="cancelled",
                current_stage="cancelled",
                error_message="任务已取消",
            )
        except Exception as exc:
            await structured_job_service.mark_job_finished(
                job_id,
                status="failed",
                current_stage="failed",
                error_message=str(exc),
            )
        finally:
            structured_job_service.detach_task(job_id)


structured_materialize_service = StructuredMaterializeService()
