"""最小可运行的结构化任务 worker。"""

import argparse
import asyncio
import os
import socket
import uuid
from contextlib import suppress

from api.services.structured_analysis_service import (
    EventAnalyzeRequest,
)
from api.services.structured_job_service import structured_job_service
from api.services.structured_materialize_helpers import (
    build_source_analyze_payload,
    build_structured_cache_payloads,
    fetch_source_rows,
    load_manual_review_statuses,
    replace_structured_cache_rows,
)
from api.services.structured_materialize_service import structured_materialize_service
from api.services.structured_runtime_service import (
    analyze_activity_request_with_runtime,
    cache_image_locally,
    ensure_review_state_table,
    should_materialize_structured_result,
)


def build_worker_id() -> str:
    host = socket.gethostname() or "unknown-host"
    pid = os.getpid()
    suffix = uuid.uuid4().hex[:8]
    return f"structured-worker-{host}-{pid}-{suffix}"


async def heartbeat_loop(
    job_id: int,
    *,
    worker_id: str,
    lease_seconds: int,
    interval_seconds: int,
) -> None:
    while True:
        ok = await structured_job_service.heartbeat_job(
            job_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        if not ok:
            return
        await asyncio.sleep(max(5, int(interval_seconds)))


async def run_claimed_job(
    job_id: int,
    *,
    worker_id: str,
    lease_seconds: int,
    heartbeat_interval: int,
) -> None:
    heartbeat_task = asyncio.create_task(
        heartbeat_loop(
            job_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            interval_seconds=heartbeat_interval,
        )
    )
    try:
        await structured_materialize_service.run_job(
            job_id,
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
            should_materialize_result=_should_materialize_structured_result,
            build_structured_cache_payloads=build_structured_cache_payloads,
            replace_structured_cache_rows=lambda source_table, source_record_ids, payloads: replace_structured_cache_rows(
                source_table,
                source_record_ids,
                payloads,
                ensure_review_state_table=ensure_review_state_table,
                cache_image_locally=cache_image_locally,
            ),
        )
    finally:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task


async def run_claimed_job_with_logging(
    job_id: int,
    *,
    worker_id: str,
    lease_seconds: int,
    heartbeat_interval: int,
) -> None:
    print(f"[structured_worker] claimed job #{job_id}")
    try:
        await run_claimed_job(
            job_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            heartbeat_interval=heartbeat_interval,
        )
        print(f"[structured_worker] finished job #{job_id}")
    except Exception as exc:
        print(f"[structured_worker] job #{job_id} failed: {exc}")
        raise


async def worker_main(
    *,
    poll_seconds: int,
    lease_seconds: int,
    heartbeat_interval: int,
    retry_delay_seconds: int,
    max_attempts: int,
    max_concurrency: int,
    once: bool,
) -> int:
    worker_id = build_worker_id()
    await structured_job_service.ensure_worker_columns()
    print(f"[structured_worker] started: {worker_id}")

    concurrency = max(1, int(max_concurrency or 1))
    inflight: set[asyncio.Task] = set()

    while True:
        inflight = {task for task in inflight if not task.done()}

        recovered = await structured_job_service.recover_stale_jobs(
            max_attempts=max_attempts,
            retry_delay_seconds=retry_delay_seconds,
        )
        if recovered:
            print(f"[structured_worker] recovered stale jobs: {recovered}")

        claimed_any = False
        while len(inflight) < concurrency:
            job = await structured_job_service.claim_next_job(
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            )
            if not job:
                break
            claimed_any = True
            task = asyncio.create_task(
                run_claimed_job_with_logging(
                    job.id,
                    worker_id=worker_id,
                    lease_seconds=lease_seconds,
                    heartbeat_interval=heartbeat_interval,
                )
            )
            inflight.add(task)
            if once:
                await task
                return 0

        if inflight:
            done, pending = await asyncio.wait(
                inflight,
                timeout=max(2, int(poll_seconds)),
                return_when=asyncio.FIRST_COMPLETED,
            )
            inflight = set(pending)
            for task in done:
                await task
            continue

        if not claimed_any and once:
            print("[structured_worker] no pending job")
            return 0

        await asyncio.sleep(max(2, int(poll_seconds)))

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the structured materialize worker")
    parser.add_argument("--poll-seconds", type=int, default=5, help="No-job polling interval")
    parser.add_argument("--lease-seconds", type=int, default=120, help="Job lease duration")
    parser.add_argument("--heartbeat-seconds", type=int, default=30, help="Lease heartbeat interval")
    parser.add_argument("--retry-delay-seconds", type=int, default=10, help="Recovered job retry delay")
    parser.add_argument("--max-attempts", type=int, default=3, help="Max attempts before stale jobs fail")
    parser.add_argument("--max-concurrency", type=int, default=1, help="Max concurrent claimed jobs")
    parser.add_argument("--once", action="store_true", help="Claim and run at most one job")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(
        worker_main(
            poll_seconds=args.poll_seconds,
            lease_seconds=args.lease_seconds,
            heartbeat_interval=args.heartbeat_seconds,
            retry_delay_seconds=args.retry_delay_seconds,
            max_attempts=args.max_attempts,
            max_concurrency=args.max_concurrency,
            once=args.once,
        )
    )
