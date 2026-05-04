# -*- coding: utf-8 -*-
"""
配置管理服务 — 读写 .env 文件、分组展示、变更历史
修改配置后自动热重载 config 模块，切换数据库时自动建表。
"""

from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.config_meta import (
    CONFIG_GROUPS,
    get_group_key_by_field,
    get_sensitive_keys,
)
from database.webui_models import ConfigHistory

# Project .env path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"

# 所有敏感 key（来源于 config_meta）
_SENSITIVE_KEYS = get_sensitive_keys()


class ConfigService:
    """配置管理服务"""

    # ------ .env 读取 ------

    @staticmethod
    def _read_env_file() -> Dict[str, str]:
        """解析 .env 文件为 dict"""
        result: Dict[str, str] = {}
        if not ENV_FILE.exists():
            return result
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                result[key] = value
        return result

    @staticmethod
    def _write_env_file(env: Dict[str, str]) -> None:
        """将 dict 写回 .env（保持注释 & 顺序）"""
        lines: List[str] = []
        existing_keys: set = set()

        if ENV_FILE.exists():
            for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
                stripped = raw.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    key = stripped.split("=", 1)[0].strip()
                    existing_keys.add(key)
                    if key in env:
                        lines.append(f'{key}="{env[key]}"')
                    else:
                        lines.append(raw)
                else:
                    lines.append(raw)

        # append new keys
        for key, value in env.items():
            if key not in existing_keys:
                lines.append(f'{key}="{value}"')

        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ------ public API ------

    def get(self, key: str, default: str = "") -> str:
        """统一配置读取入口 — 直接从 .env 文件读取。

        所有需要读取运行时配置的新代码应通过此方法，
        避免 `from config import xxx` 与 .env 直读之间的不一致。

        用法::
            from api.services.config_service import config_service
            auth_key = config_service.get("WECHAT_AUTH_KEY")
        """
        return self._read_env_file().get(key, default)

    @staticmethod
    def _infer_provider_from_base_url(base_url: str) -> str:
        value = (base_url or "").strip().lower()
        if not value:
            return ""
        if "deepseek" in value:
            return "deepseek"
        if "anthropic" in value or "claude" in value:
            return "anthropic"
        return "openai"

    @staticmethod
    def _default_model_for_provider(provider: str) -> str:
        provider_name = (provider or "").strip().lower()
        if provider_name == "deepseek":
            return "deepseek-chat"
        if provider_name == "anthropic":
            return "claude-3-5-sonnet-latest"
        return "gpt-4o-mini"

    @staticmethod
    def _default_image_model(base_url: str, provider: str) -> str:
        value = (base_url or "").strip().lower()
        provider_name = (provider or "").strip().lower()
        if "dashscope.aliyuncs.com/compatible-mode" in value:
            return "qwen-vl-ocr-latest"
        return ConfigService._default_model_for_provider(provider_name)

    @staticmethod
    def _get_provider_api_key(env: Dict[str, str], provider: str, explicit_key: str = "") -> str:
        if explicit_key:
            return explicit_key
        provider_name = (provider or "").strip().lower()
        if provider_name == "openai":
            return env.get("OPENAI_API_KEY", "")
        if provider_name == "deepseek":
            return env.get("DEEPSEEK_API_KEY", "")
        if provider_name == "anthropic":
            return env.get("ANTHROPIC_API_KEY", "")
        return ""

    def resolve_text_ai_settings(self, env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        current_env = env or self._read_env_file()
        explicit_provider = (current_env.get("TEXT_AI_PROVIDER") or "").strip().lower()
        legacy_provider = (current_env.get("AI_PROVIDER") or "").strip().lower()
        base_url = (
            current_env.get("TEXT_AI_BASE_URL")
            or (current_env.get("DEEPSEEK_BASE_URL", "") if (explicit_provider or legacy_provider) == "deepseek" else "")
        ).strip()
        inferred_provider = self._infer_provider_from_base_url(base_url)
        provider = explicit_provider or inferred_provider or legacy_provider or "openai"
        explicit_model = (current_env.get("TEXT_AI_MODEL") or "").strip()
        legacy_model = (current_env.get("AI_MODEL") or "").strip()
        model = explicit_model or (
            legacy_model if legacy_provider and provider == legacy_provider else self._default_model_for_provider(provider)
        )
        api_key = self._get_provider_api_key(
            current_env,
            provider,
            explicit_key=(current_env.get("TEXT_AI_API_KEY") or "").strip(),
        )
        return {
            "provider": provider,
            "model": model,
            "base_url": base_url,
            "api_key": api_key,
            "timeout": (current_env.get("TEXT_AI_TIMEOUT") or "").strip(),
            "max_tokens": (current_env.get("TEXT_AI_MAX_TOKENS") or "").strip(),
        }

    def resolve_image_ai_settings(self, env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        current_env = env or self._read_env_file()
        text_settings = self.resolve_text_ai_settings(current_env)
        base_url = (
            current_env.get("IMAGE_UNDERSTANDING_BASE_URL")
            or text_settings["base_url"]
            or current_env.get("DEEPSEEK_BASE_URL", "")
        ).strip()
        explicit_provider = (current_env.get("IMAGE_UNDERSTANDING_PROVIDER") or "").strip().lower()
        inferred_provider = self._infer_provider_from_base_url(base_url)
        provider = explicit_provider or inferred_provider or text_settings["provider"] or "openai"
        explicit_model = (current_env.get("IMAGE_UNDERSTANDING_MODEL") or "").strip()
        text_model = (current_env.get("TEXT_AI_MODEL") or "").strip()
        legacy_model = (current_env.get("AI_MODEL") or "").strip()
        model = explicit_model or (
            text_model if provider == text_settings["provider"] and text_model else (
                legacy_model if provider == (current_env.get("AI_PROVIDER") or "").strip().lower() and legacy_model
                else self._default_image_model(base_url, provider)
            )
        )
        api_key = self._get_provider_api_key(
            current_env,
            provider,
            explicit_key=(current_env.get("IMAGE_UNDERSTANDING_API_KEY") or "").strip(),
        )
        return {
            "enabled": (current_env.get("IMAGE_UNDERSTANDING_ENABLED") or "").strip(),
            "provider": provider,
            "model": model,
            "base_url": base_url,
            "api_key": api_key,
            "max_images": (current_env.get("IMAGE_UNDERSTANDING_MAX_IMAGES") or "").strip(),
            "timeout": (current_env.get("IMAGE_UNDERSTANDING_TIMEOUT") or "").strip(),
            "use_local_first": (current_env.get("IMAGE_UNDERSTANDING_USE_LOCAL_FIRST") or "").strip(),
        }

    def _resolve_field_raw_value(self, env: Dict[str, str], field: dict) -> str:
        value = env.get(field["key"], "")
        if value:
            return value
        for alias in field.get("aliases", []) or []:
            alias_value = env.get(alias, "")
            if alias_value:
                return alias_value
        return ""

    def get_all_groups(
        self,
        *,
        include_keys: Optional[List[str]] = None,
        exclude_keys: Optional[List[str]] = None,
    ) -> List[dict]:
        """获取所有配置分组 (值脱敏)"""
        env = self._read_env_file()
        if "FEISHU_BITABLE_APP_TOKEN" not in env and "FEISHU_APP_TOKEN" in env:
            env["FEISHU_BITABLE_APP_TOKEN"] = env["FEISHU_APP_TOKEN"]
        include_set = set(include_keys or [])
        exclude_set = set(exclude_keys or [])
        groups = []
        for group in CONFIG_GROUPS:
            group_key = str(group.get("key") or "")
            if include_set and group_key not in include_set:
                continue
            if exclude_set and group_key in exclude_set:
                continue
            fields = []
            for f in group["fields"]:
                raw_value = self._resolve_field_raw_value(env, f)
                value = "****" if f.get("sensitive") and raw_value else raw_value
                fields.append({**f, "value": value})
            groups.append({**group, "fields": fields})
        return groups

    async def update_configs(
        self, configs: Dict[str, str], session: Optional[AsyncSession] = None
    ) -> List[str]:
        """批量更新配置并记录历史，然后热重载 config 模块。
        如果 SAVE_DATA_OPTION 切换到了数据库类型，自动创建表。"""
        env = self._read_env_file()
        if "FEISHU_APP_TOKEN" in env and "FEISHU_BITABLE_APP_TOKEN" not in env:
            env["FEISHU_BITABLE_APP_TOKEN"] = env["FEISHU_APP_TOKEN"]
        if "FEISHU_APP_TOKEN" in configs and "FEISHU_BITABLE_APP_TOKEN" not in configs:
            configs["FEISHU_BITABLE_APP_TOKEN"] = configs["FEISHU_APP_TOKEN"]

        updated: List[str] = []

        for key, new_value in configs.items():
            old_value = env.get(key, "")
            if old_value == new_value:
                continue
            env[key] = new_value
            updated.append(key)

            # 记录变更历史
            if session:
                group_key = self._find_group(key)
                session.add(ConfigHistory(
                    config_group=group_key,
                    config_key=key,
                    old_value="****" if key in _SENSITIVE_KEYS else old_value,
                    new_value="****" if key in _SENSITIVE_KEYS else new_value,
                    change_source="webui",
                ))

        if updated:
            self._write_env_file(env)
            # ---- 关键: 热重载 config 模块 ----
            self._hot_reload_config()

            # 如果 SAVE_DATA_OPTION 变了，且新值是数据库类型，自动建表
            if "SAVE_DATA_OPTION" in updated:
                new_option = env.get("SAVE_DATA_OPTION", "csv")
                if new_option in ("db", "sqlite", "postgres"):
                    await self._auto_init_db(new_option)

        return updated

    @staticmethod
    def _hot_reload_config():
        """热重载 config 模块，使 .env 修改立即生效到内存"""
        try:
            from config import reload_from_env
            reload_from_env()
        except Exception as e:
            print(f"[ConfigService] Hot reload failed: {e}")

    @staticmethod
    async def _auto_init_db(db_type: str):
        """切换到数据库模式时自动创建表"""
        try:
            from database.db_session import _engines, create_tables
            import database.webui_models  # noqa: F401 — ensure WebUI tables are in metadata
            # 清除旧引擎缓存，确保用新配置重建连接
            if db_type in _engines:
                old_engine = _engines.pop(db_type)
                await old_engine.dispose()
            await create_tables(db_type)
            print(f"[ConfigService] Auto-initialized {db_type} tables")
        except Exception as e:
            print(f"[ConfigService] Auto DB init failed: {e}")

    async def get_history(
        self, session: AsyncSession, page: int = 1, size: int = 20
    ) -> tuple:
        """获取变更历史 (items, total)"""
        from sqlalchemy import func as sa_func
        total_q = await session.execute(
            select(sa_func.count()).select_from(ConfigHistory)
        )
        total = total_q.scalar() or 0

        result = await session.execute(
            select(ConfigHistory)
            .order_by(ConfigHistory.changed_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        items = result.scalars().all()
        return items, total

    @staticmethod
    def _find_group(key: str) -> str:
        return get_group_key_by_field(key)


config_service = ConfigService()
