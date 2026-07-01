# -*- coding: utf-8 -*-
"""
Pipeline 步骤引擎 — 可编排的任务管道

每个步骤（PipelineStep 子类）是独立的、可复用的执行单元。
通过 PipelineContext 在步骤间传递中间产物（如 CSV 路径）。

注册的步骤类型：
  crawl              — 通用爬虫采集（search/creator 模式）
  subscription_crawl — 遍历活跃订阅逐个爬取

添加新步骤：
  1. 继承 PipelineStep，设置 step_type 类变量
  2. 实现 run(ctx, log) 方法
  3. 将类加入 STEP_REGISTRY
"""
"""

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, ClassVar, Dict, List, Optional, Type

from api.services.config_service import config_service

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent


# ─── 上下文 ────────────────────────────────────────────────────────────────────

class PipelineContext:
    """
    Pipeline 执行上下文，在步骤间传递状态。

    Attributes:
        task_id:        关联的 ScheduledTask.id（0 表示手动触发）
        execution_id:   TaskExecution.id，用于写日志
        platform:       当前任务的默认平台（步骤可覆盖）
        vars:           步骤间共享变量（例如上一步输出的 CSV 路径）
        aborted:        某步骤失败后设为 True，后续步骤将跳过
        step_results:   每步的执行摘要，写入 result_summary
    """

    def __init__(self, task_id: int, execution_id: int, platform: str = ""):
        self.task_id = task_id
        self.execution_id = execution_id
        self.platform = platform
        self.vars: Dict[str, Any] = {}
        self.aborted: bool = False
        self.step_results: List[Dict] = []


# ─── 基类 ─────────────────────────────────────────────────────────────────────

class PipelineStep(ABC):
    """所有步骤的抽象基类"""

    step_type: ClassVar[str] = ""

    def __init__(self, config: Dict[str, Any]):
        self.config = config

    @abstractmethod
    async def run(self, ctx: PipelineContext, log: Callable[[str], Any]) -> None:
        """
        执行步骤逻辑。
        失败时应设置 ctx.aborted = True 并 await log(error_msg)，
        不应直接 raise（由 run_pipeline 的 try/except 兜底）。
        """
        ...

    async def _run_subprocess(
        self,
        cmd: List[str],
        ctx: PipelineContext,
        log: Callable[[str], Any],
        *,
        cwd: Optional[Path] = None,
    ) -> int:
        """
        运行子进程并将 stdout/stderr 实时写入执行日志。
        返回 exit code。
        """
        env = {**os.environ, "PYTHONUTF8": "1"}
        work_dir = cwd or PROJECT_ROOT

        # 打印执行命令（隐藏长路径已在参数层）
        await log(f"$ {' '.join(str(c) for c in cmd)}")

        proc = await asyncio.create_subprocess_exec(
            *[str(c) for c in cmd],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(work_dir),
            env=env,
        )

        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            await log(line.decode("utf-8", errors="replace").rstrip())

        exit_code = await proc.wait()
        return exit_code


# ─── 步骤实现 ─────────────────────────────────────────────────────────────────

class CrawlStep(PipelineStep):
    """
    通用爬虫采集步骤（search / creator 模式）。

    config keys:
      platform      — 平台（优先级 > ctx.platform）
      crawler_type  — search | creator（默认 search）
      keywords      — 搜索关键词（search 模式）
      creator_ids   — 创作者 ID 列表（creator 模式）
      limit         — 采集数量上限
      login_type    — cookie | phone（默认 cookie）
      save_option   — json | csv | db（默认 json）
      headless      — 是否无头模式（默认 True）
      timeout_seconds — 等待超时（默认 1800）
    """

    step_type = "crawl"

    async def run(self, ctx: PipelineContext, log: Callable) -> None:
        from api.services.crawler_manager import crawler_manager
        from api.schemas import CrawlerStartRequest

        cfg = self.config
        platform = cfg.get("platform") or ctx.platform

        status = crawler_manager.get_status()
        if status.get("status") == "running":
            ctx.aborted = True
            await log("[crawl] ERROR: 另一个爬虫任务正在运行，中止管道")
            return

        req = CrawlerStartRequest(
            platform=platform,
            login_type=cfg.get("login_type", "cookie"),
            crawler_type=cfg.get("crawler_type", "search"),
            keywords=cfg.get("keywords", ""),
            creator_ids=cfg.get("creator_ids", ""),
            save_option=cfg.get("save_option", "json"),
            headless=(lambda _v: (_v if isinstance(_v, bool) else str(_v).strip().lower() in ("1","true","yes","y","on")))(
                cfg.get("headless", __import__("api.services.config_service", fromlist=["config_service"]).config_service.get("HEADLESS", "false"))
            ),
        )
        started = await crawler_manager.start(req)
        if not started:
            ctx.aborted = True
            await log("[crawl] ERROR: 爬虫启动失败")
            return

        timeout = int(cfg.get("timeout_seconds", 1800))
        for _ in range(timeout):
            await asyncio.sleep(1)
            s = crawler_manager.get_status()
            if s.get("status") != "running":
                break

        await log(f"[crawl] OK platform={platform}")
        ctx.step_results.append({"step": "crawl", "platform": platform, "status": "ok"})


class SubscriptionCrawlStep(PipelineStep):
    """
    遍历活跃订阅逐个爬取（对应原 subscription_combo Step 1）。

    config keys:
      platform         — 平台过滤（不填则爬所有活跃订阅）
      only_creator_ids — 仅爬指定 ID 列表（字符串列表或逗号分隔字符串）
      limit            — 最多爬取前 N 个订阅
      timeout_seconds  — 单个爬取等待超时（默认 3600）
      crawl_config     — 传给爬虫的通用配置（login_type/save_option/headless）
    """

    step_type = "subscription_crawl"

    async def run(self, ctx: PipelineContext, log: Callable) -> None:
        from api.services.crawler_manager import crawler_manager
        from api.schemas import CrawlerStartRequest
        from database.webui_models import Subscription
        from database.db_session import get_session
        from sqlalchemy import select

        cfg = self.config
        platform_filter = cfg.get("platform") or ctx.platform
        timeout = int(cfg.get("timeout_seconds", 3600))
        limit = int(cfg.get("limit", 0) or 0)

        # 解析 only_creator_ids
        raw_ids = cfg.get("only_creator_ids", [])
        if isinstance(raw_ids, str):
            only_ids = {i.strip() for i in raw_ids.split(",") if i.strip()}
        else:
            only_ids = set(raw_ids)

        # 加载订阅列表
        async with get_session() as session:
            if not session:
                ctx.aborted = True
                await log("[subscription_crawl] ERROR: 数据库不可用")
                return

            q = select(Subscription).where(
                Subscription.is_active == True,  # noqa: E712
            )
            if platform_filter:
                q = q.where(Subscription.platform == platform_filter)
            q = q.order_by(Subscription.updated_at.desc())
            subs = (await session.execute(q)).scalars().all()

        if only_ids:
            subs = [s for s in subs if s.creator_id in only_ids]
        if limit > 0:
            subs = subs[:limit]

        await log(f"[subscription_crawl] 待爬取 {len(subs)} 个订阅")

        crawled = 0
        skipped = 0
        for sub in subs:
            await log(f"[subscription_crawl] ▶ {sub.creator_name}({sub.creator_id})")

            status = crawler_manager.get_status()
            if status.get("status") == "running":
                skipped += 1
                await log(f"[subscription_crawl] skip {sub.creator_id}: 爬虫忙，跳过")
                continue

            crawl_cfg = dict(cfg.get("crawl_config") or {})
            if sub.crawl_config:
                crawl_cfg.update(sub.crawl_config)

            req = CrawlerStartRequest(
                platform=sub.platform,
                login_type=crawl_cfg.get("login_type", "cookie"),
                crawler_type="creator",
                creator_ids=sub.creator_id,
                save_option=crawl_cfg.get("save_option", "json"),
                headless=(lambda _v: (_v if isinstance(_v, bool) else str(_v).strip().lower() in ("1","true","yes","y","on")))(
                    crawl_cfg.get("headless", __import__("api.services.config_service", fromlist=["config_service"]).config_service.get("HEADLESS", "false"))
                ),
            )
            started = await crawler_manager.start(req)
            if not started:
                await log(f"[subscription_crawl] WARN: 启动失败 {sub.creator_id}，跳过")
                skipped += 1
                continue

            for _ in range(timeout):
                await asyncio.sleep(1)
                s = crawler_manager.get_status()
                if s.get("status") != "running":
                    break

            # 更新 last_crawled_at
            async with get_session() as upd_session:
                if upd_session:
                    r = await upd_session.execute(
                        select(Subscription).where(Subscription.id == sub.id)
                    )
                    rec = r.scalars().first()
                    if rec:
                        rec.last_crawled_at = datetime.now()
                        await upd_session.commit()

            crawled += 1
            await log(f"[subscription_crawl] ✓ {sub.creator_id} done")

        await log(f"[subscription_crawl] OK crawled={crawled} skipped={skipped}")
        ctx.step_results.append({
            "step": "subscription_crawl",
            "crawled": crawled,
            "skipped": skipped,
            "total": len(subs),
            "status": "ok",
        })


# FeishuPushStep 已移除 (DEPRECATED 2026-07-01)


# FeishuPullStep 已移除 (DEPRECATED 2026-07-01)


# ─── 步骤注册表 ───────────────────────────────────────────────────────────────

STEP_REGISTRY: Dict[str, Type[PipelineStep]] = {
    "crawl": CrawlStep,
    "subscription_crawl": SubscriptionCrawlStep,
    # feishu_push / feishu_pull / feishu_push_json 已移除 (DEPRECATED)
}


def get_step_registry_info() -> List[Dict[str, Any]]:
    """返回所有已注册步骤的元信息（供 API 和前端使用）"""
    return [
        {
            "step_type": cls.step_type,
            "class_name": cls.__name__,
            "description": (cls.__doc__ or "").strip().splitlines()[0] if cls.__doc__ else "",
        }
        for cls in STEP_REGISTRY.values()
    ]


# ─── 管道执行器 ───────────────────────────────────────────────────────────────

async def run_pipeline(
    steps_config: List[Dict[str, Any]],
    ctx: PipelineContext,
    log: Callable[[str], Any],
) -> None:
    """
    按序执行 pipeline 中的所有步骤。
    任意步骤失败（ctx.aborted=True）后中止，不继续执行后续步骤。

    Args:
        steps_config:  pipeline 步骤配置列表（来自 task_config["pipeline"]）
        ctx:           执行上下文（步骤间共享）
        log:           异步日志函数 async def log(line: str) -> None
    """
    total = len(steps_config)
    await log(f"[pipeline] 开始执行，共 {total} 个步骤")

    for i, step_cfg in enumerate(steps_config):
        if ctx.aborted:
            await log(f"[pipeline] 已中止，跳过步骤 {i+1}~{total}")
            break

        step_type = step_cfg.get("step", "")
        cls = STEP_REGISTRY.get(step_type)

        await log(f"[pipeline] ▶ Step {i+1}/{total}: {step_type}")

        if cls is None:
            ctx.aborted = True
            await log(f"[pipeline] ERROR: 未知步骤类型 {step_type!r}，中止管道")
            return

        step = cls(step_cfg)
        try:
            await step.run(ctx, log)
        except Exception as exc:
            ctx.aborted = True
            await log(f"[pipeline] EXCEPTION in {step_type}: {exc}")
            logger.exception(f"[Pipeline] Step {step_type} raised exception")
            break

        if ctx.aborted:
            await log(
                f"[pipeline] ✗ 步骤 {step_type} 标记失败，"
                f"中止（已完成 {i}/{total} 步）"
            )
            break

        await log(f"[pipeline] ✓ Step {i+1}/{total}: {step_type} 完成")

    if not ctx.aborted:
        await log(f"[pipeline] ✅ 全部 {total} 个步骤执行完毕")
    else:
        completed = len(ctx.step_results)
        await log(f"[pipeline] ✗ 管道中止（{completed}/{total} 步完成）")
