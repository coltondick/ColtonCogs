from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

import aiohttp
import aiosqlite
import discord
from discord.ext import tasks
from redbot.core import Config, app_commands, commands
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path

log = logging.getLogger("red.goldaccess")

DEFAULT_API_URL = "https://8k.cms-only.ru/api/api.php"
ROLE_PAID = "IPTV Paid Customer"
LEGACY_ROLE_PAID = "IPTV Subscriber"
ROLE_TRIAL = "IPTV Trial"
ROLE_EXEMPT = "IPTV Access Exempt"
ACTIVE_DB_STATUSES = ("active", "verification_error")


class ProviderError(RuntimeError):
    """Safe provider-facing error that does not contain the API key."""


class ProviderConfigurationError(ProviderError):
    pass


class ProviderRejected(ProviderError):
    pass


class ProviderUncertain(ProviderError):
    pass


@dataclass(frozen=True)
class ProviderAccount:
    provider_user_id: str
    notes: str
    expires_at: int
    enabled: bool
    raw_status: str

    @property
    def active(self) -> bool:
        now = int(datetime.now(timezone.utc).timestamp())
        return self.enabled and self.expires_at > now


@dataclass(frozen=True)
class ProvisionedAccount:
    provider_user_id: str
    playlist_url: str


class GoldAccess(commands.Cog):
    """Gate Discord roles using active Gold Panel IPTV subscriptions."""

    access = app_commands.Group(
        name="access",
        description="Verify and inspect your IPTV Discord access",
    )
    accessadmin = app_commands.Group(
        name="accessadmin",
        description="Configure and manage IPTV subscription access",
    )

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(
            self,
            identifier=891274603115,
            force_registration=True,
        )
        self.config.register_global(
            api_url=DEFAULT_API_URL,
            device_info_action="device_info",
            device_info_id_parameter="id",
            sync_minutes=10,
            verification_grace_minutes=60,
        )
        self.config.register_guild(
            paid_role_id=None,
            trial_role_id=None,
            exempt_role_id=None,
            logs_channel_id=None,
            kick_when_inactive=False,
            protected_category_ids=[],
            paid_only_gate_migrated=False,
            enabled=False,
        )

        self._session: Optional[aiohttp.ClientSession] = None
        self._db: Optional[aiosqlite.Connection] = None
        self._ready = asyncio.Event()
        self._sync_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        data_path = cog_data_path(self)
        data_path.mkdir(parents=True, exist_ok=True)

        self._db = await aiosqlite.connect(data_path / "goldaccess.sqlite3")
        self._db.row_factory = aiosqlite.Row
        await self._initialize_database()

        timeout = aiohttp.ClientTimeout(total=25, connect=10, sock_read=20)
        self._session = aiohttp.ClientSession(timeout=timeout)
        self._ready.set()
        self.subscription_sync.start()

    async def cog_unload(self) -> None:
        self.subscription_sync.cancel()
        self._ready.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _initialize_database(self) -> None:
        db = self._require_db()
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                guild_id INTEGER NOT NULL,
                discord_user_id INTEGER NOT NULL,
                provider_user_id TEXT NOT NULL,
                access_type TEXT NOT NULL CHECK(access_type IN ('paid', 'trial')),
                status TEXT NOT NULL,
                provider_expires_at INTEGER,
                last_verified_at INTEGER,
                last_attempt_at INTEGER,
                last_error TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (guild_id, discord_user_id, provider_user_id)
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_goldaccess_sync
            ON subscriptions(guild_id, status, last_attempt_at)
            """
        )
        await db.commit()

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("GoldAccess database is not initialized")
        return self._db

    def _require_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("GoldAccess HTTP session is not initialized")
        return self._session

    @staticmethod
    def _now() -> int:
        return int(datetime.now(timezone.utc).timestamp())

    @staticmethod
    def _provider_success(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().casefold() in {
            "1",
            "true",
            "yes",
            "ok",
            "success",
            "enabled",
            "active",
        }

    @staticmethod
    def _provider_message(item: Mapping[str, Any]) -> str:
        return str(
            item.get("message")
            or item.get("messasge")
            or item.get("error")
            or "Provider rejected the request."
        )

    @staticmethod
    def _first_item(payload: Any) -> Mapping[str, Any]:
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return payload[0]
        if isinstance(payload, dict):
            for key in ("data", "result", "device", "account"):
                nested = payload.get(key)
                if isinstance(nested, list) and nested and isinstance(nested[0], dict):
                    return nested[0]
                if isinstance(nested, dict):
                    return nested
            return payload
        raise ProviderUncertain("Provider returned an unexpected response structure.")

    async def _get_api_key(self) -> str:
        env_key = os.getenv("GOLDPANEL_API_KEY", "").strip()
        if env_key:
            return env_key

        tokens = await self.bot.get_shared_api_tokens("goldpanel")
        api_key = str(tokens.get("api_key", "")).strip()
        if not api_key:
            raise ProviderConfigurationError(
                "Gold Panel API key is not configured. Set GOLDPANEL_API_KEY or use Red shared API tokens."
            )
        return api_key

    async def _api_request(self, params: Mapping[str, str]) -> Any:
        api_url = str(await self.config.api_url()).strip()
        if not api_url.startswith("https://"):
            raise ProviderConfigurationError("Provider API URL must use HTTPS.")

        request_params = dict(params)
        request_params["api_key"] = await self._get_api_key()

        try:
            async with self._require_session().get(
                api_url,
                params=request_params,
            ) as response:
                body = await response.text()
                if response.status < 200 or response.status >= 300:
                    raise ProviderUncertain(
                        f"Provider returned HTTP {response.status}; account state is uncertain."
                    )
                try:
                    return json.loads(body)
                except json.JSONDecodeError as exc:
                    raise ProviderUncertain(
                        "Provider returned invalid JSON; account state is uncertain."
                    ) from exc
        except ProviderError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise ProviderUncertain(
                "Provider request failed or timed out; account state is uncertain."
            ) from exc

    @staticmethod
    def _parse_expiration(value: Any) -> int:
        if value is None:
            raise ProviderUncertain("Provider account response did not include an expiration value.")

        if isinstance(value, (int, float)):
            timestamp = int(value)
            if timestamp > 10_000_000_000:
                timestamp //= 1000
            return timestamp

        text = str(value).strip()
        if not text:
            raise ProviderUncertain("Provider account expiration value was empty.")
        if text.isdigit():
            timestamp = int(text)
            if timestamp > 10_000_000_000:
                timestamp //= 1000
            return timestamp

        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.astimezone(timezone.utc).timestamp())
        except ValueError:
            pass

        formats = (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%d-%m-%Y %H:%M:%S",
            "%d-%m-%Y",
            "%m/%d/%Y %H:%M:%S",
            "%m/%d/%Y",
        )
        for fmt in formats:
            try:
                parsed = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
                return int(parsed.timestamp())
            except ValueError:
                continue

        raise ProviderUncertain(
            f"Provider returned an unrecognized expiration value: {text[:80]}"
        )

    @staticmethod
    def _parse_enabled(item: Mapping[str, Any]) -> tuple[bool, str]:
        raw_candidates = (
            item.get("device_status"),
            item.get("account_status"),
            item.get("line_status"),
            item.get("enabled"),
            item.get("active"),
            item.get("is_active"),
        )
        for raw in raw_candidates:
            if raw is None:
                continue
            text = str(raw).strip().casefold()
            if text in {"0", "false", "no", "disabled", "disable", "inactive", "expired", "blocked"}:
                return False, text
            if text in {"1", "true", "yes", "enabled", "enable", "active"}:
                return True, text

        # Some Gold Panel responses use status only for request success. Treat it
        # as account state only when it clearly says disabled/inactive.
        raw_status = str(item.get("status", "")).strip().casefold()
        if raw_status in {"disabled", "disable", "inactive", "expired", "blocked"}:
            return False, raw_status
        return True, raw_status or "unspecified"

    async def _get_provider_account(self, provider_user_id: str) -> ProviderAccount:
        action = str(await self.config.device_info_action()).strip() or "device_info"
        id_parameter = (
            str(await self.config.device_info_id_parameter()).strip() or "id"
        )
        payload = await self._api_request(
            {
                "action": action,
                id_parameter: provider_user_id,
            }
        )
        item = self._first_item(payload)

        if item.get("status") is not None:
            status_text = str(item.get("status")).strip().casefold()
            if status_text in {"error", "failed", "fail", "false", "0"}:
                raise ProviderRejected(self._provider_message(item))

        resolved_id = str(
            item.get("user_id")
            or item.get("id")
            or item.get("device_id")
            or provider_user_id
        ).strip()
        notes = str(item.get("note") or item.get("notes") or "").strip()
        expiration = self._parse_expiration(
            item.get("expire")
            or item.get("expires")
            or item.get("expires_at")
            or item.get("expiration")
            or item.get("exp_date")
        )
        enabled, raw_status = self._parse_enabled(item)
        return ProviderAccount(
            provider_user_id=resolved_id,
            notes=notes,
            expires_at=expiration,
            enabled=enabled,
            raw_status=raw_status,
        )

    async def _create_provider_account(
        self,
        *,
        discord_user_id: int,
        guild_id: int,
        months: int,
        package_id: str,
        country: str,
    ) -> ProvisionedAccount:
        if months not in {1, 3, 6, 12}:
            raise ProviderConfigurationError("Subscription months must be 1, 3, 6, or 12.")
        notes = (
            f"discord_user_id={discord_user_id};guild_id={guild_id};source=red-goldaccess"
        )
        payload = await self._api_request(
            {
                "action": "new",
                "type": "m3u",
                "sub": str(months),
                "pack": package_id,
                "country": country,
                "notes": notes,
            }
        )
        item = self._first_item(payload)
        if not self._provider_success(item.get("status")):
            raise ProviderRejected(self._provider_message(item))

        provider_user_id = str(item.get("user_id") or item.get("id") or "").strip()
        playlist_url = str(item.get("url") or item.get("playlist_url") or "").strip()
        if not provider_user_id:
            raise ProviderUncertain(
                "Provider reported success without returning a provider user ID."
            )
        return ProvisionedAccount(
            provider_user_id=provider_user_id,
            playlist_url=playlist_url,
        )

    @staticmethod
    def _notes_match_user(notes: str, discord_user_id: int, guild_id: int) -> bool:
        user_pattern = re.compile(
            rf"(?:^|[;,\s])(?:discord|discord_id|discord_user_id)\s*=\s*{discord_user_id}(?:$|[;,\s])",
            flags=re.IGNORECASE,
        )
        if not user_pattern.search(notes):
            return False

        guild_pattern = re.compile(
            r"(?:^|[;,\s])guild_id\s*=\s*(\d+)(?:$|[;,\s])",
            flags=re.IGNORECASE,
        )
        guild_match = guild_pattern.search(notes)
        return guild_match is None or int(guild_match.group(1)) == guild_id

    async def _upsert_subscription(
        self,
        *,
        guild_id: int,
        discord_user_id: int,
        provider_user_id: str,
        access_type: str,
        status: str,
        expires_at: Optional[int],
        verified_at: Optional[int],
        error: Optional[str] = None,
    ) -> None:
        now = self._now()
        db = self._require_db()
        await db.execute(
            """
            INSERT INTO subscriptions (
                guild_id,
                discord_user_id,
                provider_user_id,
                access_type,
                status,
                provider_expires_at,
                last_verified_at,
                last_attempt_at,
                last_error,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, discord_user_id, provider_user_id) DO UPDATE SET
                access_type = excluded.access_type,
                status = excluded.status,
                provider_expires_at = excluded.provider_expires_at,
                last_verified_at = excluded.last_verified_at,
                last_attempt_at = excluded.last_attempt_at,
                last_error = excluded.last_error,
                updated_at = excluded.updated_at
            """,
            (
                guild_id,
                discord_user_id,
                provider_user_id,
                access_type,
                status,
                expires_at,
                verified_at,
                now,
                error[:1000] if error else None,
                now,
                now,
            ),
        )
        await db.commit()

    async def _fetch_member_rows(
        self,
        guild_id: int,
        discord_user_id: int,
    ) -> list[aiosqlite.Row]:
        db = self._require_db()
        async with db.execute(
            """
            SELECT * FROM subscriptions
            WHERE guild_id = ? AND discord_user_id = ?
            ORDER BY created_at DESC
            """,
            (guild_id, discord_user_id),
        ) as cursor:
            return list(await cursor.fetchall())

    async def _fetch_guild_rows(self, guild_id: int) -> list[aiosqlite.Row]:
        db = self._require_db()
        async with db.execute(
            """
            SELECT * FROM subscriptions
            WHERE guild_id = ?
            ORDER BY last_attempt_at ASC
            """,
            (guild_id,),
        ) as cursor:
            return list(await cursor.fetchall())

    async def _set_row_result(
        self,
        row: aiosqlite.Row,
        *,
        status: str,
        expires_at: Optional[int] = None,
        verified: bool = False,
        error: Optional[str] = None,
    ) -> None:
        now = self._now()
        db = self._require_db()
        await db.execute(
            """
            UPDATE subscriptions
            SET status = ?,
                provider_expires_at = COALESCE(?, provider_expires_at),
                last_verified_at = CASE WHEN ? THEN ? ELSE last_verified_at END,
                last_attempt_at = ?,
                last_error = ?,
                updated_at = ?
            WHERE guild_id = ?
              AND discord_user_id = ?
              AND provider_user_id = ?
            """,
            (
                status,
                expires_at,
                1 if verified else 0,
                now,
                now,
                error[:1000] if error else None,
                now,
                int(row["guild_id"]),
                int(row["discord_user_id"]),
                str(row["provider_user_id"]),
            ),
        )
        await db.commit()

    async def _get_roles(
        self,
        guild: discord.Guild,
    ) -> tuple[
        Optional[discord.Role],
        Optional[discord.Role],
        Optional[discord.Role],
    ]:
        guild_config = self.config.guild(guild)
        paid_id = await guild_config.paid_role_id()
        trial_id = await guild_config.trial_role_id()
        exempt_id = await guild_config.exempt_role_id()
        paid_role = guild.get_role(int(paid_id)) if paid_id else None
        trial_role = guild.get_role(int(trial_id)) if trial_id else None
        exempt_role = guild.get_role(int(exempt_id)) if exempt_id else None
        return paid_role, trial_role, exempt_role

    async def _ensure_roles(
        self,
        guild: discord.Guild,
    ) -> tuple[discord.Role, discord.Role, discord.Role]:
        paid_role, trial_role, exempt_role = await self._get_roles(guild)
        if paid_role is None:
            paid_role = discord.utils.get(guild.roles, name=ROLE_PAID)
        if paid_role is None:
            paid_role = discord.utils.get(guild.roles, name=LEGACY_ROLE_PAID)
        if trial_role is None:
            trial_role = discord.utils.get(guild.roles, name=ROLE_TRIAL)
        if exempt_role is None:
            exempt_role = discord.utils.get(guild.roles, name=ROLE_EXEMPT)

        reason = "GoldAccess subscription role setup"
        if paid_role is None:
            paid_role = await guild.create_role(
                name=ROLE_PAID,
                permissions=discord.Permissions.none(),
                mentionable=False,
                hoist=True,
                reason=reason,
            )
        elif paid_role.name == LEGACY_ROLE_PAID:
            await paid_role.edit(name=ROLE_PAID, reason="GoldAccess paid role rename")

        if trial_role is None:
            trial_role = await guild.create_role(
                name=ROLE_TRIAL,
                permissions=discord.Permissions.none(),
                mentionable=False,
                hoist=True,
                reason=reason,
            )
        if exempt_role is None:
            exempt_role = await guild.create_role(
                name=ROLE_EXEMPT,
                permissions=discord.Permissions.none(),
                mentionable=False,
                hoist=False,
                reason=reason,
            )

        guild_config = self.config.guild(guild)
        await guild_config.paid_role_id.set(paid_role.id)
        await guild_config.trial_role_id.set(trial_role.id)
        await guild_config.exempt_role_id.set(exempt_role.id)
        return paid_role, trial_role, exempt_role

    async def _set_paid_only_category_permissions(
        self,
        category: discord.CategoryChannel,
        paid_role: discord.Role,
        trial_role: discord.Role,
    ) -> None:
        """Restrict a protected category to paid customers only."""
        reason = "GoldAccess paid-subscription gate"
        await category.set_permissions(
            category.guild.default_role,
            view_channel=False,
            reason=reason,
        )
        await category.set_permissions(
            paid_role,
            view_channel=True,
            read_message_history=True,
            send_messages=True,
            connect=True,
            speak=True,
            reason=reason,
        )
        # Explicitly deny the trial role on protected categories so any legacy
        # trial allow-overwrite is removed. Public categories remain unchanged.
        await category.set_permissions(
            trial_role,
            view_channel=False,
            read_message_history=False,
            send_messages=False,
            connect=False,
            speak=False,
            reason=reason,
        )

    async def _migrate_paid_only_gate(
        self,
        guild: discord.Guild,
        *,
        force: bool = False,
    ) -> tuple[int, int]:
        guild_config = self.config.guild(guild)
        if not force and bool(await guild_config.paid_only_gate_migrated()):
            return 0, 0

        paid_role, trial_role, _ = await self._get_roles(guild)
        if paid_role is None or trial_role is None:
            return 0, 0

        updated = 0
        failed = 0
        for category_id in await guild_config.protected_category_ids():
            channel = guild.get_channel(int(category_id))
            if not isinstance(channel, discord.CategoryChannel):
                continue
            try:
                await self._set_paid_only_category_permissions(
                    channel,
                    paid_role,
                    trial_role,
                )
                updated += 1
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
                log.exception(
                    "Unable to migrate protected category %s in guild %s to paid-only access",
                    channel.id,
                    guild.id,
                )

        if failed == 0:
            await guild_config.paid_only_gate_migrated.set(True)
        return updated, failed

    async def _apply_roles_for_member(self, member: discord.Member) -> None:
        rows = await self._fetch_member_rows(member.guild.id, member.id)
        now = self._now()
        grace_seconds = max(0, int(await self.config.verification_grace_minutes())) * 60

        has_paid = False
        has_trial = False
        for row in rows:
            status = str(row["status"])
            expires_at = int(row["provider_expires_at"] or 0)
            last_verified = int(row["last_verified_at"] or 0)

            row_is_active = status == "active" and expires_at > now
            row_is_in_grace = (
                status == "verification_error"
                and expires_at > now
                and last_verified > 0
                and now - last_verified <= grace_seconds
            )
            if not (row_is_active or row_is_in_grace):
                continue
            if str(row["access_type"]) == "trial":
                has_trial = True
            else:
                has_paid = True

        # Paid access takes precedence. A member with both an active trial and
        # an active paid account should be represented as a paid customer only.
        if has_paid:
            has_trial = False

        paid_role, trial_role, exempt_role = await self._get_roles(member.guild)
        if paid_role is None or trial_role is None:
            return

        access_exempt = exempt_role is not None and exempt_role in member.roles
        to_add: list[discord.Role] = []
        to_remove: list[discord.Role] = []
        if has_paid and paid_role not in member.roles:
            to_add.append(paid_role)
        if not has_paid and paid_role in member.roles and not access_exempt:
            to_remove.append(paid_role)
        if has_trial and trial_role not in member.roles:
            to_add.append(trial_role)
        if not has_trial and trial_role in member.roles and not access_exempt:
            to_remove.append(trial_role)

        try:
            if to_add:
                await member.add_roles(*to_add, reason="Active IPTV subscription verified")
            if to_remove:
                await member.remove_roles(*to_remove, reason="IPTV subscription is not active")
        except discord.Forbidden:
            await self._log(
                member.guild,
                "Role synchronization failed",
                f"I cannot manage roles for {member.mention}. Move my bot role above `{ROLE_PAID}`, `{ROLE_TRIAL}`, and `{ROLE_EXEMPT}`.",
                discord.Color.red(),
            )
            return

        if not has_paid and not has_trial and not access_exempt:
            kick_enabled = bool(await self.config.guild(member.guild).kick_when_inactive())
            if kick_enabled and member != member.guild.owner:
                try:
                    await member.kick(reason="No active IPTV subscription")
                except discord.Forbidden:
                    await self._log(
                        member.guild,
                        "Inactive member could not be removed",
                        f"I could not kick {member.mention}. Check my permissions and role position.",
                        discord.Color.red(),
                    )

    async def _verify_row(self, guild: discord.Guild, row: aiosqlite.Row) -> None:
        member = guild.get_member(int(row["discord_user_id"]))
        try:
            account = await self._get_provider_account(str(row["provider_user_id"]))
            if not self._notes_match_user(
                account.notes,
                int(row["discord_user_id"]),
                guild.id,
            ):
                await self._set_row_result(
                    row,
                    status="identity_mismatch",
                    expires_at=account.expires_at,
                    verified=True,
                    error="Provider notes no longer match the linked Discord user.",
                )
                access_exempt = False
                if member is not None:
                    _, _, exempt_role = await self._get_roles(guild)
                    access_exempt = exempt_role is not None and exempt_role in member.roles
                    await self._apply_roles_for_member(member)
                access_result = (
                    "Access roles were retained because the member has the access exemption role."
                    if access_exempt
                    else "Access roles were removed because the provider notes no longer contain the linked Discord user ID."
                )
                await self._log(
                    guild,
                    "Subscription identity mismatch",
                    (
                        f"Member: <@{int(row['discord_user_id'])}>\n"
                        f"Provider user ID: `{row['provider_user_id']}`\n"
                        f"{access_result}"
                    ),
                    discord.Color.red(),
                )
                return

            status = "active" if account.active else "inactive"
            await self._set_row_result(
                row,
                status=status,
                expires_at=account.expires_at,
                verified=True,
            )
            access_exempt = False
            if member is not None:
                _, _, exempt_role = await self._get_roles(guild)
                access_exempt = exempt_role is not None and exempt_role in member.roles
                await self._apply_roles_for_member(member)
            if status == "inactive":
                await self._log(
                    guild,
                    (
                        "Subscription inactive, access retained"
                        if access_exempt
                        else "Subscription access removed"
                    ),
                    (
                        f"Member: <@{int(row['discord_user_id'])}>\n"
                        f"Provider user ID: `{row['provider_user_id']}`\n"
                        f"Provider expiration: <t:{account.expires_at}:F>\n"
                        + (
                            "The member has the access exemption role, so GoldAccess did not remove roles or kick them."
                            if access_exempt
                            else "The member no longer has verified subscription access."
                        )
                    ),
                    discord.Color.orange(),
                )
        except ProviderError as exc:
            await self._set_row_result(
                row,
                status="verification_error",
                error=str(exc),
            )
            if member is not None:
                await self._apply_roles_for_member(member)
            await self._log(
                guild,
                "Subscription verification failed",
                (
                    f"Member: <@{int(row['discord_user_id'])}>\n"
                    f"Provider user ID: `{row['provider_user_id']}`\n"
                    f"Reason: {str(exc)[:500]}"
                ),
                discord.Color.red(),
            )

    async def _sync_guild(self, guild: discord.Guild) -> tuple[int, int]:
        if not bool(await self.config.guild(guild).enabled()):
            return 0, 0
        rows = await self._fetch_guild_rows(guild.id)
        successful = 0
        failed = 0
        for row in rows:
            before = str(row["status"])
            await self._verify_row(guild, row)
            refreshed = await self._fetch_member_rows(
                guild.id,
                int(row["discord_user_id"]),
            )
            match = next(
                (
                    item
                    for item in refreshed
                    if str(item["provider_user_id"]) == str(row["provider_user_id"])
                ),
                None,
            )
            if match is not None and str(match["status"]) == "verification_error":
                failed += 1
            else:
                successful += 1
            if before != "inactive" and match is not None and str(match["status"]) == "inactive":
                pass
        return successful, failed

    async def _log(
        self,
        guild: discord.Guild,
        title: str,
        description: str,
        color: discord.Color,
    ) -> None:
        channel_id = await self.config.guild(guild).logs_channel_id()
        if channel_id is None:
            return
        channel = guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            return
        embed = discord.Embed(title=title, description=description, color=color)
        embed.timestamp = datetime.now(timezone.utc)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            log.warning("Unable to send GoldAccess log message in guild %s", guild.id)

    @tasks.loop(minutes=10.0)
    async def subscription_sync(self) -> None:
        configured = max(1, int(await self.config.sync_minutes()))
        if abs(self.subscription_sync.minutes - configured) > 0.1:
            self.subscription_sync.change_interval(minutes=float(configured))

        async with self._sync_lock:
            for guild in list(self.bot.guilds):
                try:
                    await self._migrate_paid_only_gate(guild)
                    await self._sync_guild(guild)
                except Exception:
                    log.exception("GoldAccess guild synchronization failed for %s", guild.id)

    @subscription_sync.before_loop
    async def before_subscription_sync(self) -> None:
        await self.bot.wait_until_red_ready()
        await self._ready.wait()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        await self._ready.wait()
        if not bool(await self.config.guild(member.guild).enabled()):
            return
        rows = await self._fetch_member_rows(member.guild.id, member.id)
        for row in rows:
            await self._verify_row(member.guild, row)
        await self._apply_roles_for_member(member)

    async def _is_admin(self, interaction: discord.Interaction) -> bool:
        if await self.bot.is_owner(interaction.user):
            return True
        return isinstance(interaction.user, discord.Member) and (
            interaction.user.guild_permissions.administrator
            or interaction.user.guild_permissions.manage_guild
        )

    async def _require_admin(self, interaction: discord.Interaction) -> bool:
        if await self._is_admin(interaction):
            return True
        await self._respond(
            interaction,
            "You need **Manage Server** or **Administrator** to use this command.",
        )
        return False

    @staticmethod
    async def _respond(
        interaction: discord.Interaction,
        content: Optional[str] = None,
        *,
        embed: Optional[discord.Embed] = None,
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content=content, embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(
                content=content,
                embed=embed,
                ephemeral=True,
            )

    @access.command(name="verify", description="Re-check an already linked provider account")
    @app_commands.guild_only()
    @app_commands.describe(provider_user_id="Gold Panel account/user ID")
    async def access_verify(
        self,
        interaction: discord.Interaction,
        provider_user_id: str,
    ) -> None:
        await self._ready.wait()
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            return
        if not bool(await self.config.guild(guild).enabled()):
            await self._respond(interaction, "Subscription access verification is disabled.")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        provider_user_id = provider_user_id.strip()
        if not provider_user_id:
            await self._respond(interaction, "Provider user ID cannot be empty.")
            return

        try:
            account = await self._get_provider_account(provider_user_id)
        except ProviderError as exc:
            await self._respond(interaction, f"Provider verification failed: {exc}")
            return

        if not self._notes_match_user(account.notes, member.id, guild.id):
            await self._respond(
                interaction,
                (
                    "This provider account is not linked to your Discord identity. "
                    f"Its account notes must contain `discord_user_id={member.id}`."
                ),
            )
            return
        if not account.active:
            await self._respond(
                interaction,
                f"That subscription is not active. Provider expiration: <t:{account.expires_at}:F>.",
            )
            return

        linked_rows = await self._fetch_member_rows(guild.id, member.id)
        linked_row = next(
            (
                row
                for row in linked_rows
                if str(row["provider_user_id"])
                in {provider_user_id, account.provider_user_id}
            ),
            None,
        )
        if linked_row is None:
            await self._respond(
                interaction,
                (
                    "This provider account has not been classified as paid or trial. "
                    "Ask an administrator to import it with `/accessadmin importaccount`. "
                    "This prevents trial accounts from claiming paid Discord access."
                ),
            )
            return

        access_type = str(linked_row["access_type"])
        await self._upsert_subscription(
            guild_id=guild.id,
            discord_user_id=member.id,
            provider_user_id=account.provider_user_id,
            access_type=access_type,
            status="active",
            expires_at=account.expires_at,
            verified_at=self._now(),
        )
        await self._apply_roles_for_member(member)
        await self._log(
            guild,
            "Subscription verified",
            (
                f"Member: {member.mention} (`{member.id}`)\n"
                f"Provider user ID: `{account.provider_user_id}`\n"
                f"Access type: `{access_type}`\n"
                f"Expires: <t:{account.expires_at}:F>"
            ),
            discord.Color.green(),
        )
        result = (
            "Paid customer access is active."
            if access_type == "paid"
            else "This account is a trial, so you remain in the public channels."
        )
        await self._respond(
            interaction,
            f"Your IPTV account is verified through <t:{account.expires_at}:F>. {result}",
        )

    @access.command(name="status", description="Show your linked IPTV access status")
    @app_commands.guild_only()
    async def access_status(self, interaction: discord.Interaction) -> None:
        await self._ready.wait()
        guild = interaction.guild
        if guild is None:
            return
        rows = await self._fetch_member_rows(guild.id, interaction.user.id)
        _, _, exempt_role = await self._get_roles(guild)
        access_exempt = (
            isinstance(interaction.user, discord.Member)
            and exempt_role is not None
            and exempt_role in interaction.user.roles
        )
        if not rows:
            await self._respond(
                interaction,
                "No IPTV provider account is linked to your Discord account.",
            )
            return

        lines: list[str] = []
        for row in rows:
            expires = int(row["provider_expires_at"] or 0)
            expires_text = f"<t:{expires}:F>" if expires else "unknown"
            lines.append(
                f"• `{row['provider_user_id']}` | **{row['access_type']}** | "
                f"`{row['status']}` | expires {expires_text}"
            )
        lines.append(
            f"Access-removal exemption: **{'enabled' if access_exempt else 'disabled'}**"
        )
        await self._respond(interaction, "\n".join(lines))

    @accessadmin.command(name="setup", description="Create the default IPTV subscriber roles")
    @app_commands.guild_only()
    @app_commands.describe(logs_channel="Private channel for access synchronization logs")
    async def accessadmin_setup(
        self,
        interaction: discord.Interaction,
        logs_channel: discord.TextChannel,
    ) -> None:
        if not await self._require_admin(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            paid_role, trial_role, exempt_role = await self._ensure_roles(guild)
        except discord.Forbidden:
            await self._respond(
                interaction,
                "I cannot create roles. Grant the bot **Manage Roles** and try again.",
            )
            return

        guild_config = self.config.guild(guild)
        await guild_config.logs_channel_id.set(logs_channel.id)
        updated, failed = await self._migrate_paid_only_gate(guild, force=True)
        migration_text = (
            f" Updated **{updated}** existing protected categor{'y' if updated == 1 else 'ies'} to paid-only access."
            if updated
            else ""
        )
        if failed:
            migration_text += (
                f" I could not update **{failed}** protected categor{'y' if failed == 1 else 'ies'}; "
                "check **Manage Channels** and category permissions."
            )
        await self._respond(
            interaction,
            (
                f"Created or adopted {paid_role.mention}, {trial_role.mention}, and {exempt_role.mention}."
                f"{migration_text} Move the bot role above all three roles, protect customer categories "
                "with `/accessadmin protect`, configure the API key, then enable synchronization. "
                "Only the paid role receives protected-category access; trial users remain in public channels."
            ),
        )

    @accessadmin.command(name="protect", description="Restrict a category to active paid IPTV customers")
    @app_commands.guild_only()
    @app_commands.describe(category="Category that only active IPTV users should see")
    async def accessadmin_protect(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
    ) -> None:
        if not await self._require_admin(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        paid_role, trial_role, _ = await self._ensure_roles(guild)
        try:
            await self._set_paid_only_category_permissions(
                category,
                paid_role,
                trial_role,
            )
        except (discord.Forbidden, discord.HTTPException):
            await self._respond(
                interaction,
                "I cannot edit that category. Grant **Manage Channels** and verify role hierarchy.",
            )
            return

        guild_config = self.config.guild(guild)
        ids = set(await guild_config.protected_category_ids())
        ids.add(category.id)
        await guild_config.protected_category_ids.set(sorted(ids))
        await guild_config.paid_only_gate_migrated.set(True)
        await self._respond(
            interaction,
            f"{category.name} is now visible to active paid customers only. Trial users remain in public channels.",
        )

    @accessadmin.command(name="provision", description="Create a paid IPTV account and grant subscriber access")
    @app_commands.guild_only()
    @app_commands.describe(
        member="Discord member receiving the subscription",
        months="Subscription length: 1, 3, 6, or 12 months",
        package_id="Gold Panel package/bouquet ID",
        country="Two-letter country code or ALL",
    )
    @app_commands.choices(
        months=[
            app_commands.Choice(name="1 month", value=1),
            app_commands.Choice(name="3 months", value=3),
            app_commands.Choice(name="6 months", value=6),
            app_commands.Choice(name="12 months", value=12),
        ]
    )
    async def accessadmin_provision(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        months: app_commands.Choice[int],
        package_id: str,
        country: str = "CA",
    ) -> None:
        if not await self._require_admin(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        country = country.strip().upper()
        if country != "ALL" and (len(country) != 2 or not country.isalpha()):
            await self._respond(interaction, "Country must be a two-letter code or `ALL`.")
            return
        package_id = package_id.strip()
        if not package_id:
            await self._respond(interaction, "Package ID cannot be empty.")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            provisioned = await self._create_provider_account(
                discord_user_id=member.id,
                guild_id=guild.id,
                months=months.value,
                package_id=package_id,
                country=country,
            )
            account = await self._get_provider_account(provisioned.provider_user_id)
        except ProviderError as exc:
            await self._respond(interaction, f"Provisioning failed: {exc}")
            return

        if not self._notes_match_user(account.notes, member.id, guild.id):
            await self._respond(
                interaction,
                (
                    "The account was created, but the provider did not return the expected Discord ID in its notes. "
                    f"Check provider user ID `{provisioned.provider_user_id}` manually before granting access."
                ),
            )
            return

        await self._upsert_subscription(
            guild_id=guild.id,
            discord_user_id=member.id,
            provider_user_id=account.provider_user_id,
            access_type="paid",
            status="active" if account.active else "inactive",
            expires_at=account.expires_at,
            verified_at=self._now(),
        )
        await self._apply_roles_for_member(member)

        credential_text = ""
        if provisioned.playlist_url:
            # Prevent provider-controlled text from closing the Discord code block.
            # Keep all escaping outside f-string expressions for Python 3.11 compatibility.
            safe_playlist_url = provisioned.playlist_url.replace("```", "``\u200b`")
            credential_text = (
                "\n\nCredentials were returned by the provider. "
                "Send them to the customer through a private ticket or DM:\n"
                "```text\n"
                f"{safe_playlist_url}\n"
                "```"
            )
        await self._respond(
            interaction,
            (
                f"Created provider user `{account.provider_user_id}` for {member.mention}. "
                f"The provider notes contain `discord_user_id={member.id}`. "
                f"Expiration: <t:{account.expires_at}:F>."
                f"{credential_text}"
            ),
        )

    @accessadmin.command(name="importaccount", description="Import an existing provider account after verifying its notes")
    @app_commands.guild_only()
    @app_commands.describe(
        member="Discord member linked in the provider notes",
        provider_user_id="Gold Panel account/user ID",
        access_type="Paid subscription or 24-hour trial",
    )
    @app_commands.choices(
        access_type=[
            app_commands.Choice(name="Paid subscription", value="paid"),
            app_commands.Choice(name="Trial", value="trial"),
        ]
    )
    async def accessadmin_importaccount(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        provider_user_id: str,
        access_type: app_commands.Choice[str],
    ) -> None:
        if not await self._require_admin(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            account = await self._get_provider_account(provider_user_id.strip())
        except ProviderError as exc:
            await self._respond(interaction, f"Provider verification failed: {exc}")
            return

        if not self._notes_match_user(account.notes, member.id, guild.id):
            await self._respond(
                interaction,
                (
                    "Import rejected. Add this exact token to the provider account notes first: "
                    f"`discord_user_id={member.id}`"
                ),
            )
            return

        await self._upsert_subscription(
            guild_id=guild.id,
            discord_user_id=member.id,
            provider_user_id=account.provider_user_id,
            access_type=access_type.value,
            status="active" if account.active else "inactive",
            expires_at=account.expires_at,
            verified_at=self._now(),
        )
        await self._apply_roles_for_member(member)
        await self._respond(
            interaction,
            (
                f"Imported `{account.provider_user_id}` for {member.mention}. "
                f"Status: **{'active' if account.active else 'inactive'}**. "
                f"Expiration: <t:{account.expires_at}:F>."
            ),
        )

    @accessadmin.command(name="sync", description="Synchronize one member or the entire server now")
    @app_commands.guild_only()
    async def accessadmin_sync(
        self,
        interaction: discord.Interaction,
        member: Optional[discord.Member] = None,
    ) -> None:
        if not await self._require_admin(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self._sync_lock:
            if member is not None:
                rows = await self._fetch_member_rows(guild.id, member.id)
                for row in rows:
                    await self._verify_row(guild, row)
                await self._apply_roles_for_member(member)
                await self._respond(
                    interaction,
                    f"Synchronized {len(rows)} linked account(s) for {member.mention}.",
                )
                return

            successful, failed = await self._sync_guild(guild)
            await self._respond(
                interaction,
                f"Synchronization complete. Verified: **{successful}**. Provider errors: **{failed}**.",
            )

    @accessadmin.command(name="enable", description="Enable or disable automatic subscription synchronization")
    @app_commands.guild_only()
    async def accessadmin_enable(
        self,
        interaction: discord.Interaction,
        enabled: bool,
    ) -> None:
        if not await self._require_admin(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        if enabled:
            try:
                await self._get_api_key()
            except ProviderConfigurationError as exc:
                await self._respond(interaction, f"Cannot enable: {exc}")
                return
            paid_role, trial_role, exempt_role = await self._get_roles(guild)
            if paid_role is None or trial_role is None or exempt_role is None:
                await self._respond(
                    interaction,
                    "Run `/accessadmin setup` before enabling synchronization.",
                )
                return
        await self.config.guild(guild).enabled.set(enabled)
        await self._respond(
            interaction,
            f"Subscription synchronization is now **{'enabled' if enabled else 'disabled'}**.",
        )

    @accessadmin.command(
        name="exemption",
        description="Add or remove the role that prevents automatic access removal",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        member="Member whose automatic access removal should be bypassed",
        enabled="Whether the member should be exempt from role removal and kick mode",
    )
    async def accessadmin_exemption(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        enabled: bool,
    ) -> None:
        if not await self._require_admin(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            _, _, exempt_role = await self._ensure_roles(guild)
            if enabled:
                await member.add_roles(
                    exempt_role,
                    reason=f"GoldAccess exemption enabled by {interaction.user}",
                )
                await self._respond(
                    interaction,
                    (
                        f"{member.mention} now has {exempt_role.mention}. GoldAccess will continue "
                        "checking linked subscriptions, but it will not remove their paid/trial roles "
                        "or kick them when access becomes inactive."
                    ),
                )
                return

            await member.remove_roles(
                exempt_role,
                reason=f"GoldAccess exemption disabled by {interaction.user}",
            )
            await self._apply_roles_for_member(member)
            await self._respond(
                interaction,
                (
                    f"Removed {exempt_role.mention} from {member.mention}. Their roles now match "
                    "their current verified subscription state."
                ),
            )
        except discord.Forbidden:
            await self._respond(
                interaction,
                "I cannot manage the exemption role. Move the bot role above it and grant **Manage Roles**.",
            )

    @accessadmin.command(name="kickmode", description="Choose whether inactive users are kicked from the server")
    @app_commands.guild_only()
    async def accessadmin_kickmode(
        self,
        interaction: discord.Interaction,
        enabled: bool,
    ) -> None:
        if not await self._require_admin(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        await self.config.guild(guild).kick_when_inactive.set(enabled)
        await self._respond(
            interaction,
            (
                "Inactive users will be kicked after role removal."
                if enabled
                else "Inactive users will remain in the public lobby but lose subscriber roles."
            ),
        )

    @accessadmin.command(name="providerconfig", description="Configure the provider device-info request")
    @app_commands.guild_only()
    @app_commands.describe(
        info_action="Provider action used to inspect one account",
        id_parameter="Query parameter containing the provider account ID",
    )
    async def accessadmin_providerconfig(
        self,
        interaction: discord.Interaction,
        info_action: str = "device_info",
        id_parameter: str = "id",
    ) -> None:
        if not await self._require_admin(interaction):
            return
        if not re.fullmatch(r"[A-Za-z0-9_]+", info_action):
            await self._respond(interaction, "Info action contains invalid characters.")
            return
        if not re.fullmatch(r"[A-Za-z0-9_]+", id_parameter):
            await self._respond(interaction, "ID parameter contains invalid characters.")
            return
        await self.config.device_info_action.set(info_action)
        await self.config.device_info_id_parameter.set(id_parameter)
        await self._respond(
            interaction,
            f"Provider account lookup now uses `action={info_action}&{id_parameter}=ACCOUNT_ID`.",
        )

    @accessadmin.command(name="settings", description="Show GoldAccess configuration")
    @app_commands.guild_only()
    async def accessadmin_settings(self, interaction: discord.Interaction) -> None:
        if not await self._require_admin(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        guild_config = self.config.guild(guild)
        paid_role, trial_role, exempt_role = await self._get_roles(guild)
        category_ids = await guild_config.protected_category_ids()
        categories = [guild.get_channel(int(item)) for item in category_ids]
        category_names = ", ".join(
            channel.name for channel in categories if isinstance(channel, discord.CategoryChannel)
        ) or "none"
        await self._respond(
            interaction,
            (
                f"Enabled: **{bool(await guild_config.enabled())}**\n"
                f"Paid role: {paid_role.mention if paid_role else 'missing'}\n"
                f"Trial role: {trial_role.mention if trial_role else 'missing'}\n"
                f"Access exemption role: {exempt_role.mention if exempt_role else 'missing'}\n"
                f"Protected categories: **{category_names}**\n"
                f"Kick inactive users: **{bool(await guild_config.kick_when_inactive())}**\n"
                f"Sync interval: **{int(await self.config.sync_minutes())} minutes**\n"
                f"Verification grace: **{int(await self.config.verification_grace_minutes())} minutes**\n"
                f"Device lookup: `action={await self.config.device_info_action()}&{await self.config.device_info_id_parameter()}=ACCOUNT_ID`"
            ),
        )
