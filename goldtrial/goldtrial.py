from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, MutableMapping, Optional
from urllib.parse import parse_qs, urlparse

import aiohttp
import aiosqlite
import discord
from discord.ext import tasks
from redbot.core import Config, app_commands, commands
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path

log = logging.getLogger("red.goldtrial")

DEFAULT_API_URL = "https://8k.cms-only.ru/api/api.php"
ONE_DAY_SUBSCRIPTION_ID = 8
TRIAL_DURATION_HOURS = 24
CAPACITY_STATUSES = (
    "provisioning",
    "active",
    "disabling",
    "disable_failed",
    "unknown",
    "revoke_pending",
    "revoke_failed",
)
FINAL_STATUSES = ("expired", "revoked")


class ProviderError(RuntimeError):
    """Base provider exception with a safe, non-secret message."""


class ProviderRejected(ProviderError):
    """The provider returned an explicit failure response."""


class ProviderAmbiguousError(ProviderError):
    """The request result is unknown and retrying could create a duplicate."""


class ProviderConfigurationError(ProviderError):
    """The local provider configuration is incomplete."""


@dataclass(frozen=True)
class ProvisionedTrial:
    provider_user_id: str
    playlist_url: str
    message: str


@dataclass(frozen=True)
class Availability:
    maximum: int
    consumed: int
    manual_reserved: int
    available: int
    active: int
    pending_cleanup: int
    unknown: int


class GoldTrial(commands.Cog):
    """Provision one lifetime 24-hour Gold Panel trial per Discord user."""

    trial = app_commands.Group(name="trial", description="Claim and manage your IPTV trial")
    trialadmin = app_commands.Group(
        name="trialadmin", description="Configure and manage IPTV trials"
    )

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(
            self,
            identifier=785031904217,
            force_registration=True,
        )
        self.config.register_global(
            api_url=DEFAULT_API_URL,
            package_id="",
            country="ALL",
            # Legacy keys are retained for compatibility with existing Red Config data.
            # Provisioning always uses the fixed one-day provider subscription ID below.
            subscription_months=1,
            duration_hours=TRIAL_DURATION_HOURS,
            max_concurrent_trials=10,
            manual_reserved_slots=0,
            enabled=False,
            user_hash_secret="",
        )
        self.config.register_guild(
            trial_category_id=None,
            logs_channel_id=None,
            ticket_channel_prefix="trial-",
            require_user_id_in_channel_name=True,
        )

        self._db: Optional[aiosqlite.Connection] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._state_lock = asyncio.Lock()
        self._ready = asyncio.Event()

    async def cog_load(self) -> None:
        data_path = cog_data_path(self)
        data_path.mkdir(parents=True, exist_ok=True)

        self._db = await aiosqlite.connect(data_path / "goldtrial.sqlite3")
        self._db.row_factory = aiosqlite.Row
        await self._initialize_database()
        await self._ensure_hash_secret()

        # The provider has been observed taking longer than 15 seconds to finish
        # account creation. Keep a conservative timeout so a successful creation
        # is not incorrectly classified as unknown.
        timeout = aiohttp.ClientTimeout(total=90, connect=15, sock_read=75)
        self._session = aiohttp.ClientSession(timeout=timeout)
        self._ready.set()
        self.expiration_loop.start()

    async def cog_unload(self) -> None:
        self.expiration_loop.cancel()
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
            CREATE TABLE IF NOT EXISTS trial_claims (
                user_key TEXT PRIMARY KEY,
                discord_user_id INTEGER,
                guild_id INTEGER,
                ticket_channel_id INTEGER,
                provider_user_id TEXT,
                playlist_url TEXT,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                disabled_at INTEGER,
                last_error TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trial_claims_status_expires
            ON trial_claims(status, expires_at)
            """
        )
        await db.commit()

    async def _ensure_hash_secret(self) -> None:
        current = await self.config.user_hash_secret()
        if current:
            return
        await self.config.user_hash_secret.set(secrets.token_hex(32))

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("GoldTrial database is not initialized")
        return self._db

    def _require_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("GoldTrial HTTP session is not initialized")
        return self._session

    async def _user_key(self, user_id: int) -> str:
        secret = await self.config.user_hash_secret()
        if not secret:
            raise RuntimeError("GoldTrial user hash secret is not initialized")
        return hmac.new(
            secret.encode("utf-8"),
            str(user_id).encode("ascii"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _utc_now_epoch() -> int:
        return int(datetime.now(timezone.utc).timestamp())

    @staticmethod
    def _provider_success(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"true", "1", "yes", "ok", "success"}

    @staticmethod
    def _provider_message(item: Mapping[str, Any]) -> str:
        return str(
            item.get("message")
            or item.get("messasge")
            or item.get("error")
            or "Provider rejected the request."
        )

    @staticmethod
    def _safe_code(value: str) -> str:
        return value.replace("`", "\\`")

    async def _get_api_key(self) -> str:
        env_key = os.getenv("GOLDPANEL_API_KEY", "").strip()
        if env_key:
            return env_key

        tokens = await self.bot.get_shared_api_tokens("goldpanel")
        api_key = str(tokens.get("api_key", "")).strip()
        if not api_key:
            raise ProviderConfigurationError(
                "The Gold Panel API key has not been configured."
            )
        return api_key

    async def _api_request(self, params: Mapping[str, str]) -> Any:
        api_url = str(await self.config.api_url()).strip()
        if not api_url.startswith("https://"):
            raise ProviderConfigurationError("The provider API URL must use HTTPS.")

        api_key = await self._get_api_key()
        request_params = dict(params)
        request_params["api_key"] = api_key

        session = self._require_session()
        try:
            async with session.get(api_url, params=request_params) as response:
                body = await response.text()
                if response.status < 200 or response.status >= 300:
                    raise ProviderAmbiguousError(
                        f"Provider returned HTTP {response.status}; the result is uncertain."
                    )
                try:
                    return json.loads(body)
                except json.JSONDecodeError as exc:
                    raise ProviderAmbiguousError(
                        "Provider returned an invalid JSON response; the result is uncertain."
                    ) from exc
        except ProviderError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise ProviderAmbiguousError(
                "The provider request timed out or failed; the result is uncertain."
            ) from exc

    @staticmethod
    def _first_response_item(payload: Any) -> Mapping[str, Any]:
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return payload[0]
        if isinstance(payload, dict):
            return payload
        raise ProviderAmbiguousError(
            "Provider returned an unexpected response structure; the result is uncertain."
        )

    async def _create_provider_trial(
        self,
        *,
        discord_user_id: int,
        guild_id: int,
        ticket_channel_id: int,
    ) -> ProvisionedTrial:
        package_id = str(await self.config.package_id()).strip()
        if not package_id:
            raise ProviderConfigurationError("The trial package ID has not been configured.")

        country = str(await self.config.country()).strip().upper() or "ALL"

        notes = (
            f"discord={discord_user_id};guild={guild_id};ticket={ticket_channel_id}"
        )
        payload = await self._api_request(
            {
                "action": "new",
                "type": "m3u",
                # Current Gold Panel maps subscription ID 8 to exactly one day.
                # This is intentionally fixed and cannot be changed by configuration.
                "sub": str(ONE_DAY_SUBSCRIPTION_ID),
                "pack": package_id,
                "country": country,
                "notes": notes,
            }
        )
        item = self._first_response_item(payload)
        if not self._provider_success(item.get("status")):
            raise ProviderRejected(self._provider_message(item))

        provider_user_id = str(item.get("user_id", "")).strip()
        playlist_url = str(item.get("url", "")).strip()

        if not playlist_url:
            username = str(item.get("username", "")).strip()
            password = str(item.get("password", "")).strip()
            host = str(item.get("domain") or item.get("host") or "").strip()
            if username and password and host:
                host = host.rstrip("/")
                if not host.startswith(("http://", "https://")):
                    host = f"http://{host}"
                playlist_url = (
                    f"{host}/get.php?username={username}&password={password}"
                    "&type=m3u_plus&output=ts"
                )

        if not provider_user_id or not playlist_url:
            raise ProviderAmbiguousError(
                "Provider reported success without returning all account details."
            )

        return ProvisionedTrial(
            provider_user_id=provider_user_id,
            playlist_url=playlist_url,
            message=self._provider_message(item),
        )

    async def _disable_provider_user(self, provider_user_id: str) -> None:
        payload = await self._api_request(
            {
                "action": "device_status",
                "status": "disable",
                "id": provider_user_id,
            }
        )
        item = self._first_response_item(payload)
        if not self._provider_success(item.get("status")):
            raise ProviderRejected(self._provider_message(item))

    async def _fetch_claim(self, user_id: int) -> Optional[aiosqlite.Row]:
        user_key = await self._user_key(user_id)
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM trial_claims WHERE user_key = ?", (user_key,)
        ) as cursor:
            return await cursor.fetchone()

    async def _insert_provisioning_claim(
        self,
        *,
        user_id: int,
        guild_id: int,
        channel_id: int,
        created_at: int,
        expires_at: int,
    ) -> None:
        user_key = await self._user_key(user_id)
        db = self._require_db()
        await db.execute(
            """
            INSERT INTO trial_claims (
                user_key,
                discord_user_id,
                guild_id,
                ticket_channel_id,
                status,
                created_at,
                expires_at
            ) VALUES (?, ?, ?, ?, 'provisioning', ?, ?)
            """,
            (user_key, user_id, guild_id, channel_id, created_at, expires_at),
        )
        await db.commit()

    async def _delete_claim(self, user_id: int) -> None:
        user_key = await self._user_key(user_id)
        db = self._require_db()
        await db.execute("DELETE FROM trial_claims WHERE user_key = ?", (user_key,))
        await db.commit()

    async def _mark_active(
        self,
        *,
        user_id: int,
        provider_user_id: str,
        playlist_url: str,
    ) -> None:
        user_key = await self._user_key(user_id)
        db = self._require_db()
        await db.execute(
            """
            UPDATE trial_claims
            SET status = 'active', provider_user_id = ?, playlist_url = ?, last_error = NULL
            WHERE user_key = ?
            """,
            (provider_user_id, playlist_url, user_key),
        )
        await db.commit()

    async def _mark_unknown(self, *, user_id: int, error: str) -> None:
        user_key = await self._user_key(user_id)
        db = self._require_db()
        await db.execute(
            """
            UPDATE trial_claims
            SET status = 'unknown', last_error = ?
            WHERE user_key = ?
            """,
            (error[:1000], user_key),
        )
        await db.commit()

    async def _count_statuses(self) -> dict[str, int]:
        db = self._require_db()
        async with db.execute(
            "SELECT status, COUNT(*) AS count FROM trial_claims GROUP BY status"
        ) as cursor:
            rows = await cursor.fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    async def _availability(self) -> Availability:
        status_counts = await self._count_statuses()
        maximum = max(1, int(await self.config.max_concurrent_trials()))
        manual_reserved = max(0, int(await self.config.manual_reserved_slots()))
        consumed = sum(status_counts.get(status, 0) for status in CAPACITY_STATUSES)
        available = max(0, maximum - manual_reserved - consumed)
        active = status_counts.get("active", 0)
        unknown = status_counts.get("unknown", 0)
        pending_cleanup = sum(
            status_counts.get(status, 0)
            for status in (
                "disabling",
                "disable_failed",
                "revoke_pending",
                "revoke_failed",
            )
        )
        return Availability(
            maximum=maximum,
            consumed=consumed,
            manual_reserved=manual_reserved,
            available=available,
            active=active,
            pending_cleanup=pending_cleanup,
            unknown=unknown,
        )

    async def _recover_stale_provisioning(self) -> None:
        cutoff = self._utc_now_epoch() - 600
        db = self._require_db()
        await db.execute(
            """
            UPDATE trial_claims
            SET status = 'unknown',
                last_error = 'Bot stopped during provisioning; manual reconciliation required.'
            WHERE status = 'provisioning' AND created_at <= ?
            """,
            (cutoff,),
        )
        await db.commit()

    async def _expire_due_trials(self) -> None:
        """Finalize locally expired one-day trials and release their capacity slots.

        Gold Panel subscription ID 8 is a provider-managed one-day line. The provider
        should expire it automatically. We still attempt an explicit disable as a
        best-effort cleanup, but a failed cleanup does not keep a locally expired trial
        counted against the 10-slot capacity forever.
        """
        now = self._utc_now_epoch()
        db = self._require_db()
        async with db.execute(
            """
            SELECT * FROM trial_claims
            WHERE status IN ('active', 'disabling', 'disable_failed')
              AND expires_at <= ?
            ORDER BY expires_at ASC
            """,
            (now,),
        ) as cursor:
            rows = await cursor.fetchall()

        for row in rows:
            provider_user_id = str(row["provider_user_id"] or "").strip()
            cleanup_error: Optional[str] = None

            if provider_user_id:
                await db.execute(
                    "UPDATE trial_claims SET status = 'disabling' WHERE user_key = ?",
                    (row["user_key"],),
                )
                await db.commit()
                try:
                    await self._disable_provider_user(provider_user_id)
                except ProviderError as exc:
                    cleanup_error = str(exc)[:1000]

            await db.execute(
                """
                UPDATE trial_claims
                SET status = 'expired',
                    disabled_at = ?,
                    playlist_url = NULL,
                    discord_user_id = NULL,
                    ticket_channel_id = NULL,
                    last_error = ?
                WHERE user_key = ?
                """,
                (
                    now,
                    (
                        f"Best-effort provider disable failed after automatic one-day "
                        f"expiry: {cleanup_error}"
                        if cleanup_error
                        else None
                    ),
                    row["user_key"],
                ),
            )
            await db.commit()

            if cleanup_error:
                await self._log_event(
                    guild_id=row["guild_id"],
                    title="Trial expired with cleanup warning",
                    description=(
                        f"Provider user ID: `{provider_user_id or 'missing'}`\n"
                        f"Expired: <t:{now}:F>\n"
                        "The local capacity slot was released because the provider "
                        "one-day line should already be expired."
                    ),
                    color=discord.Color.orange(),
                )
            else:
                await self._log_event(
                    guild_id=row["guild_id"],
                    title="Trial expired",
                    description=(
                        f"Provider user ID: `{provider_user_id or 'missing'}`\n"
                        f"Expired: <t:{now}:F>"
                    ),
                    color=discord.Color.orange(),
                )

    @tasks.loop(minutes=1.0)
    async def expiration_loop(self) -> None:
        async with self._state_lock:
            await self._recover_stale_provisioning()
            await self._expire_due_trials()

    @expiration_loop.before_loop
    async def before_expiration_loop(self) -> None:
        await self.bot.wait_until_red_ready()
        await self._ready.wait()

    @expiration_loop.error
    async def expiration_loop_error(self, error: BaseException) -> None:
        log.error(
            "GoldTrial expiration loop failed",
            exc_info=(type(error), error, error.__traceback__),
        )

    async def _log_event(
        self,
        *,
        guild_id: Optional[int],
        title: str,
        description: str,
        color: discord.Color,
    ) -> None:
        if guild_id is None:
            return
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            return
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
            log.warning("Unable to send GoldTrial log message in guild %s", guild.id)

    async def _is_bot_admin(self, interaction: discord.Interaction) -> bool:
        if await self.bot.is_owner(interaction.user):
            return True
        return isinstance(interaction.user, discord.Member) and (
            interaction.user.guild_permissions.administrator
            or interaction.user.guild_permissions.manage_guild
        )

    async def _require_admin(self, interaction: discord.Interaction) -> bool:
        if await self._is_bot_admin(interaction):
            return True
        await self._respond(
            interaction,
            "You need **Manage Server** or **Administrator** to use this command.",
        )
        return False

    async def _validate_trial_ticket(
        self, interaction: discord.Interaction
    ) -> Optional[str]:
        guild = interaction.guild
        member = interaction.user
        channel = interaction.channel
        if guild is None or not isinstance(member, discord.Member):
            return "This command can only be used in a server."
        if not isinstance(channel, discord.TextChannel):
            return "Run this command inside your dedicated trial ticket channel."

        guild_config = self.config.guild(guild)
        category_id = await guild_config.trial_category_id()
        if category_id is None:
            return "The trial ticket category has not been configured."
        if channel.category_id != int(category_id):
            return "Run this command inside a ticket in the configured trial category."

        prefix = str(await guild_config.ticket_channel_prefix()).strip().lower()
        if prefix and not channel.name.lower().startswith(prefix):
            return f"This channel is not recognized as a `{prefix}` trial ticket."

        strict_name = bool(await guild_config.require_user_id_in_channel_name())
        if strict_name and str(member.id) not in channel.name:
            return (
                "This ticket is not assigned to your Discord user ID. "
                "Ask an administrator to verify the ticket channel naming setting."
            )

        overwrite = channel.overwrites_for(member)
        if overwrite.view_channel is not True:
            return "Only the owner of this trial ticket can claim a trial."

        return None

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
                content=content, embed=embed, ephemeral=True
            )

    @staticmethod
    def _credentials_embed(playlist_url: str, expires_at: int) -> discord.Embed:
        parsed = urlparse(playlist_url)
        query = parse_qs(parsed.query)
        username = query.get("username", [""])[0]
        password = query.get("password", [""])[0]
        server_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else ""

        embed = discord.Embed(
            title="Your 24-hour trial is ready",
            description=(
                "Keep these credentials private. Your trial will be disabled automatically "
                f"at <t:{expires_at}:F> (<t:{expires_at}:R>)."
            ),
            color=discord.Color.green(),
        )
        if server_url:
            embed.add_field(
                name="Server URL", value=f"`{GoldTrial._safe_code(server_url)}`", inline=False
            )
        if username:
            embed.add_field(
                name="Username", value=f"`{GoldTrial._safe_code(username)}`", inline=True
            )
        if password:
            embed.add_field(
                name="Password", value=f"`{GoldTrial._safe_code(password)}`", inline=True
            )
        embed.add_field(
            name="M3U URL",
            value=f"```text\n{GoldTrial._safe_code(playlist_url)}\n```",
            inline=False,
        )
        embed.set_footer(text="Each Discord user can receive only one lifetime trial.")
        return embed

    @trial.command(name="claim", description="Claim your one-time 24-hour trial")
    @app_commands.guild_only()
    async def trial_claim(self, interaction: discord.Interaction) -> None:
        ticket_error = await self._validate_trial_ticket(interaction)
        if ticket_error:
            await self._respond(interaction, ticket_error)
            return

        if not bool(await self.config.enabled()):
            await self._respond(interaction, "Trial provisioning is currently disabled.")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        user_id = interaction.user.id
        guild = interaction.guild
        channel = interaction.channel
        assert guild is not None
        assert isinstance(channel, discord.TextChannel)

        async with self._state_lock:
            existing = await self._fetch_claim(user_id)
            if existing is not None:
                status = str(existing["status"])
                if status == "active":
                    await self._respond(
                        interaction,
                        "You have already received your lifetime trial. Use `/trial credentials` "
                        "to view it while it is active.",
                    )
                elif status == "unknown":
                    await self._respond(
                        interaction,
                        "Your previous provisioning attempt has an uncertain provider result. "
                        "A support administrator must reconcile it before any action can be taken.",
                    )
                else:
                    await self._respond(
                        interaction,
                        "You have already used your one lifetime trial and cannot receive another.",
                    )
                return

            try:
                await self._get_api_key()
                package_id = str(await self.config.package_id()).strip()
                if not package_id:
                    raise ProviderConfigurationError(
                        "The trial package ID has not been configured."
                    )
            except ProviderConfigurationError as exc:
                await self._respond(interaction, f"Trial provisioning is not ready: {exc}")
                return

            await self._recover_stale_provisioning()
            await self._expire_due_trials()
            availability = await self._availability()
            if availability.available < 1:
                await self._respond(
                    interaction,
                    "All trial slots are currently in use. Your lifetime eligibility has not "
                    "been consumed, so you can try again after a slot becomes available.",
                )
                return

            created_at = self._utc_now_epoch()
            expires_at = int(
                (
                    datetime.fromtimestamp(created_at, timezone.utc)
                    + timedelta(hours=TRIAL_DURATION_HOURS)
                ).timestamp()
            )

            try:
                await self._insert_provisioning_claim(
                    user_id=user_id,
                    guild_id=guild.id,
                    channel_id=channel.id,
                    created_at=created_at,
                    expires_at=expires_at,
                )
            except aiosqlite.IntegrityError:
                await self._respond(
                    interaction,
                    "A trial claim already exists for your Discord account.",
                )
                return

            try:
                trial = await self._create_provider_trial(
                    discord_user_id=user_id,
                    guild_id=guild.id,
                    ticket_channel_id=channel.id,
                )
            except ProviderRejected as exc:
                await self._delete_claim(user_id)
                await self._log_event(
                    guild_id=guild.id,
                    title="Trial rejected by provider",
                    description=(
                        f"User: {interaction.user.mention} (`{user_id}`)\n"
                        f"Reason: {str(exc)[:500]}\n"
                        "Lifetime eligibility was not consumed."
                    ),
                    color=discord.Color.red(),
                )
                await self._respond(
                    interaction,
                    "The provider rejected the trial request. Your lifetime eligibility was not "
                    "consumed. Please contact support.",
                )
                return
            except ProviderConfigurationError as exc:
                await self._delete_claim(user_id)
                await self._respond(interaction, f"Trial provisioning is not ready: {exc}")
                return
            except ProviderAmbiguousError as exc:
                await self._mark_unknown(user_id=user_id, error=str(exc))
                await self._log_event(
                    guild_id=guild.id,
                    title="Trial result requires reconciliation",
                    description=(
                        f"User: {interaction.user.mention} (`{user_id}`)\n"
                        f"Ticket: {channel.mention}\n"
                        "Do not retry automatically because the provider may have created a line."
                    ),
                    color=discord.Color.red(),
                )
                await self._respond(
                    interaction,
                    "The provider result is uncertain. Support has been notified. Do not submit "
                    "another claim because the provider may already have created the account.",
                )
                return

            await self._mark_active(
                user_id=user_id,
                provider_user_id=trial.provider_user_id,
                playlist_url=trial.playlist_url,
            )
            await self._log_event(
                guild_id=guild.id,
                title="Trial provisioned",
                description=(
                    f"User: {interaction.user.mention} (`{user_id}`)\n"
                    f"Ticket: {channel.mention}\n"
                    f"Provider user ID: `{trial.provider_user_id}`\n"
                    f"Expires: <t:{expires_at}:F>"
                ),
                color=discord.Color.green(),
            )
            await self._respond(
                interaction,
                embed=self._credentials_embed(trial.playlist_url, expires_at),
            )

    @trial.command(name="status", description="Check your trial eligibility and status")
    @app_commands.guild_only()
    async def trial_status(self, interaction: discord.Interaction) -> None:
        await self._ready.wait()
        row = await self._fetch_claim(interaction.user.id)
        if row is None:
            availability = await self._availability()
            capacity_text = (
                "A trial slot is currently available."
                if availability.available > 0
                else "All trial slots are currently in use."
            )
            await self._respond(
                interaction,
                f"You have not used your lifetime trial. {capacity_text}",
            )
            return

        status = str(row["status"])
        if status == "active":
            expires_at = int(row["expires_at"])
            await self._respond(
                interaction,
                "Your lifetime trial is active and expires "
                f"<t:{expires_at}:F> (<t:{expires_at}:R>).",
            )
        elif status == "unknown":
            await self._respond(
                interaction,
                "Your trial claim is awaiting manual provider reconciliation. Contact support.",
            )
        elif status in {"provisioning", "disabling", "revoke_pending"}:
            await self._respond(
                interaction,
                f"Your trial is currently being processed. Status: `{status}`.",
            )
        elif status in {"disable_failed", "revoke_failed"}:
            await self._respond(
                interaction,
                "Your trial can no longer be used, but provider cleanup is still being retried. "
                "You are not eligible for another trial.",
            )
        else:
            await self._respond(
                interaction,
                "You have already used your one lifetime trial and cannot receive another.",
            )

    @trial.command(name="credentials", description="View your active trial credentials")
    @app_commands.guild_only()
    async def trial_credentials(self, interaction: discord.Interaction) -> None:
        await self._ready.wait()
        row = await self._fetch_claim(interaction.user.id)
        if row is None or str(row["status"]) != "active" or not row["playlist_url"]:
            await self._respond(interaction, "You do not have an active trial to display.")
            return
        await self._respond(
            interaction,
            embed=self._credentials_embed(
                str(row["playlist_url"]), int(row["expires_at"])
            ),
        )

    @trialadmin.command(name="setup", description="Configure the provider package and ticket channels")
    @app_commands.guild_only()
    @app_commands.describe(
        package_id="Gold Panel bouquet/package ID",
        trial_category="Category containing open trial ticket channels",
        logs_channel="Private channel for provisioning logs",
        country="Two-letter country code or ALL",
    )
    async def trialadmin_setup(
        self,
        interaction: discord.Interaction,
        package_id: str,
        trial_category: discord.CategoryChannel,
        logs_channel: discord.TextChannel,
        country: str = "ALL",
    ) -> None:
        if not await self._require_admin(interaction):
            return
        country = country.strip().upper()
        if country != "ALL" and (len(country) != 2 or not country.isalpha()):
            await self._respond(
                interaction, "Country must be a two-letter code such as `CA`, or `ALL`."
            )
            return
        package_id = package_id.strip()
        if not package_id:
            await self._respond(interaction, "Package ID cannot be empty.")
            return

        await self.config.package_id.set(package_id)
        await self.config.country.set(country)
        guild_config = self.config.guild(interaction.guild)
        await guild_config.trial_category_id.set(trial_category.id)
        await guild_config.logs_channel_id.set(logs_channel.id)
        await self._respond(
            interaction,
            "GoldTrial setup saved. Trials are fixed to the provider one-day subscription "
            f"ID `{ONE_DAY_SUBSCRIPTION_ID}`. Configure the API key, verify ticket naming, "
            "then run `/trialadmin enable enabled:True`.",
        )

    @trialadmin.command(name="enable", description="Enable or disable automatic trial provisioning")
    @app_commands.guild_only()
    async def trialadmin_enable(
        self, interaction: discord.Interaction, enabled: bool
    ) -> None:
        if not await self._require_admin(interaction):
            return
        if enabled:
            try:
                await self._get_api_key()
            except ProviderConfigurationError as exc:
                await self._respond(interaction, f"Cannot enable provisioning: {exc}")
                return
            if not str(await self.config.package_id()).strip():
                await self._respond(
                    interaction, "Cannot enable provisioning until a package ID is configured."
                )
                return
        await self.config.enabled.set(enabled)
        await self._respond(
            interaction,
            (
                f"Automatic trial provisioning is now **{'enabled' if enabled else 'disabled'}**."
                + (
                    f" New accounts use provider subscription ID {ONE_DAY_SUBSCRIPTION_ID} "
                    f"for exactly {TRIAL_DURATION_HOURS} hours."
                    if enabled
                    else ""
                )
            ),
        )

    @trialadmin.command(name="capacity", description="Set the global trial slot capacity")
    @app_commands.guild_only()
    @app_commands.describe(
        maximum="Maximum simultaneous trials allowed",
        manual_reserved="Slots reserved for trials created outside this bot",
    )
    async def trialadmin_capacity(
        self,
        interaction: discord.Interaction,
        maximum: app_commands.Range[int, 1, 100],
        manual_reserved: app_commands.Range[int, 0, 100] = 0,
    ) -> None:
        if not await self._require_admin(interaction):
            return
        if manual_reserved > maximum:
            await self._respond(
                interaction, "Manual reserved slots cannot exceed maximum capacity."
            )
            return
        await self.config.max_concurrent_trials.set(int(maximum))
        await self.config.manual_reserved_slots.set(int(manual_reserved))
        await self._respond(
            interaction,
            f"Capacity set to **{maximum}**, with **{manual_reserved}** manually reserved.",
        )

    @trialadmin.command(name="ticketnames", description="Configure trial ticket channel validation")
    @app_commands.guild_only()
    @app_commands.describe(
        prefix="Required ticket channel prefix",
        require_user_id="Require the claimant Discord ID in the channel name",
    )
    async def trialadmin_ticketnames(
        self,
        interaction: discord.Interaction,
        prefix: str = "trial-",
        require_user_id: bool = True,
    ) -> None:
        if not await self._require_admin(interaction):
            return
        prefix = prefix.strip().lower()
        if not prefix:
            await self._respond(interaction, "Ticket prefix cannot be empty.")
            return
        guild_config = self.config.guild(interaction.guild)
        await guild_config.ticket_channel_prefix.set(prefix)
        await guild_config.require_user_id_in_channel_name.set(require_user_id)
        await self._respond(
            interaction,
            f"Trial tickets must start with `{prefix}`. Discord user ID check is "
            f"**{'enabled' if require_user_id else 'disabled'}**.",
        )

    @trialadmin.command(name="availability", description="Show current global trial availability")
    @app_commands.guild_only()
    async def trialadmin_availability(self, interaction: discord.Interaction) -> None:
        if not await self._require_admin(interaction):
            return
        async with self._state_lock:
            await self._expire_due_trials()
            availability = await self._availability()
        embed = discord.Embed(title="Trial availability", color=discord.Color.blue())
        embed.add_field(name="Maximum", value=str(availability.maximum), inline=True)
        embed.add_field(name="Available", value=str(availability.available), inline=True)
        embed.add_field(name="Consumed", value=str(availability.consumed), inline=True)
        embed.add_field(name="Active", value=str(availability.active), inline=True)
        embed.add_field(
            name="Manual reserved", value=str(availability.manual_reserved), inline=True
        )
        embed.add_field(
            name="Cleanup pending", value=str(availability.pending_cleanup), inline=True
        )
        embed.add_field(name="Unknown", value=str(availability.unknown), inline=True)
        embed.set_footer(
            text="A slot is released 24 hours after successful creation. Provider disable is best-effort at expiry."
        )
        await self._respond(interaction, embed=embed)

    @trialadmin.command(name="lookup", description="Look up a Discord user's lifetime trial record")
    @app_commands.guild_only()
    async def trialadmin_lookup(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        if not await self._require_admin(interaction):
            return
        row = await self._fetch_claim(user.id)
        if row is None:
            await self._respond(interaction, "No lifetime trial record exists for that user.")
            return
        embed = discord.Embed(title="Trial record", color=discord.Color.blue())
        embed.add_field(name="User", value=f"{user.mention} (`{user.id}`)", inline=False)
        embed.add_field(name="Status", value=f"`{row['status']}`", inline=True)
        embed.add_field(
            name="Provider user ID",
            value=f"`{row['provider_user_id'] or 'Not available'}`",
            inline=True,
        )
        embed.add_field(
            name="Created", value=f"<t:{int(row['created_at'])}:F>", inline=False
        )
        embed.add_field(
            name="Expires", value=f"<t:{int(row['expires_at'])}:F>", inline=False
        )
        if row["last_error"]:
            embed.add_field(
                name="Last error", value=str(row["last_error"])[:1000], inline=False
            )
        await self._respond(interaction, embed=embed)

    @trialadmin.command(name="revoke", description="Disable a trial immediately and preserve lifetime usage")
    @app_commands.guild_only()
    async def trialadmin_revoke(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        if not await self._require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self._state_lock:
            row = await self._fetch_claim(user.id)
            if row is None:
                await self._respond(interaction, "No lifetime trial record exists for that user.")
                return
            status = str(row["status"])
            if status in FINAL_STATUSES:
                await self._respond(
                    interaction, f"That trial is already finalized with status `{status}`."
                )
                return
            provider_user_id = str(row["provider_user_id"] or "").strip()
            if not provider_user_id:
                await self._respond(
                    interaction,
                    "The provider user ID is unknown. Use `/trialadmin resolveunknown` after "
                    "manually checking the panel.",
                )
                return

            db = self._require_db()
            await db.execute(
                "UPDATE trial_claims SET status = 'revoke_pending' WHERE user_key = ?",
                (row["user_key"],),
            )
            await db.commit()
            try:
                await self._disable_provider_user(provider_user_id)
            except ProviderError as exc:
                await db.execute(
                    """
                    UPDATE trial_claims
                    SET status = 'revoke_failed', last_error = ?
                    WHERE user_key = ?
                    """,
                    (str(exc)[:1000], row["user_key"]),
                )
                await db.commit()
                await self._respond(
                    interaction,
                    "Provider disable failed. The slot remains reserved and requires retry.",
                )
                return

            now = self._utc_now_epoch()
            await db.execute(
                """
                UPDATE trial_claims
                SET status = 'revoked', disabled_at = ?, playlist_url = NULL,
                    discord_user_id = NULL, ticket_channel_id = NULL, last_error = NULL
                WHERE user_key = ?
                """,
                (now, row["user_key"]),
            )
            await db.commit()
            await self._log_event(
                guild_id=row["guild_id"],
                title="Trial revoked",
                description=(
                    f"User: {user.mention} (`{user.id}`)\n"
                    f"Provider user ID: `{provider_user_id}`"
                ),
                color=discord.Color.orange(),
            )
            await self._respond(
                interaction,
                "The provider account was disabled. The user remains permanently ineligible "
                "for another trial.",
            )

    @trialadmin.command(
        name="resolveunknown",
        description="Resolve an uncertain provisioning result after checking the provider panel",
    )
    @app_commands.guild_only()
    @app_commands.choices(
        outcome=[
            app_commands.Choice(name="No account was created", value="not_created"),
            app_commands.Choice(
                name="Account existed and has been disabled", value="created_disabled"
            ),
        ]
    )
    async def trialadmin_resolveunknown(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        outcome: app_commands.Choice[str],
    ) -> None:
        if not await self._require_admin(interaction):
            return
        async with self._state_lock:
            row = await self._fetch_claim(user.id)
            if row is None or str(row["status"]) != "unknown":
                await self._respond(
                    interaction, "That user does not have an unknown trial record."
                )
                return
            if outcome.value == "not_created":
                await self._delete_claim(user.id)
                await self._respond(
                    interaction,
                    "The uncertain record was removed. The user is eligible to try again.",
                )
                return

            db = self._require_db()
            now = self._utc_now_epoch()
            await db.execute(
                """
                UPDATE trial_claims
                SET status = 'expired', disabled_at = ?, playlist_url = NULL,
                    discord_user_id = NULL, ticket_channel_id = NULL, last_error = NULL
                WHERE user_key = ?
                """,
                (now, row["user_key"]),
            )
            await db.commit()
            await self._respond(
                interaction,
                "The record was finalized. The user remains permanently ineligible for another "
                "trial.",
            )

    @trialadmin.command(name="settings", description="Show the current GoldTrial configuration")
    @app_commands.guild_only()
    async def trialadmin_settings(self, interaction: discord.Interaction) -> None:
        if not await self._require_admin(interaction):
            return
        guild_config = self.config.guild(interaction.guild)
        category_id = await guild_config.trial_category_id()
        logs_id = await guild_config.logs_channel_id()
        embed = discord.Embed(title="GoldTrial settings", color=discord.Color.blue())
        embed.add_field(
            name="Enabled", value=str(bool(await self.config.enabled())), inline=True
        )
        embed.add_field(
            name="Package ID", value=str(await self.config.package_id()) or "Not set", inline=True
        )
        embed.add_field(name="Country", value=str(await self.config.country()), inline=True)
        embed.add_field(
            name="Duration", value=f"{TRIAL_DURATION_HOURS} hours (fixed)", inline=True
        )
        embed.add_field(
            name="Provider subscription",
            value=f"ID {ONE_DAY_SUBSCRIPTION_ID} (1 Day, fixed)",
            inline=True,
        )
        embed.add_field(
            name="Maximum trials",
            value=str(await self.config.max_concurrent_trials()),
            inline=True,
        )
        embed.add_field(
            name="Manual reserved",
            value=str(await self.config.manual_reserved_slots()),
            inline=True,
        )
        embed.add_field(
            name="Trial category",
            value=f"<#{category_id}>" if category_id else "Not set",
            inline=False,
        )
        embed.add_field(
            name="Logs channel",
            value=f"<#{logs_id}>" if logs_id else "Not set",
            inline=False,
        )
        embed.add_field(
            name="Ticket prefix",
            value=f"`{await guild_config.ticket_channel_prefix()}`",
            inline=True,
        )
        embed.add_field(
            name="Require user ID in name",
            value=str(bool(await guild_config.require_user_id_in_channel_name())),
            inline=True,
        )
        await self._respond(interaction, embed=embed)

    async def red_get_data_for_user(
        self, *, user_id: int
    ) -> MutableMapping[str, io.BytesIO]:
        await self._ready.wait()
        row = await self._fetch_claim(user_id)
        if row is None:
            return {}
        data = {
            key: row[key]
            for key in row.keys()
            if key not in {"user_key", "playlist_url"}
        }
        data["playlist_url_stored"] = bool(row["playlist_url"])
        payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
        return {"goldtrial.json": io.BytesIO(payload)}

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        await self._ready.wait()
        row = await self._fetch_claim(user_id)
        if row is None:
            return

        # The one-trial eligibility marker and provider ID are operational data.
        # Personal Discord references and retrievable credentials are disassociated.
        db = self._require_db()
        await db.execute(
            """
            UPDATE trial_claims
            SET discord_user_id = NULL,
                guild_id = NULL,
                ticket_channel_id = NULL,
                playlist_url = NULL,
                last_error = NULL
            WHERE user_key = ?
            """,
            (row["user_key"],),
        )
        await db.commit()
