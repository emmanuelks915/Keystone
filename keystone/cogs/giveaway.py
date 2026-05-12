# cogs/giveaway.py — Keystone Giveaway System
# Full DB-backed giveaways with OC/account entries, inventory/currency/XP/role/shop-item prizes,
# claim tracking, and reroll reversal logic.
#
# Requires SQL: keystone_giveaway_schema.sql
# Requires existing Keystone services when available:
# - services.oc_service.get_active_character
# - services.inventory_service.apply_delta / get_item
# - services.xp_service.XPService
# - services.currency_service.get_primary_currency / ensure_wallet / optional apply_transaction

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from services.oc_service import get_active_character

try:
    from services import inventory_service  # type: ignore
except Exception:  # pragma: no cover - optional during import/test
    inventory_service = None  # type: ignore

try:
    from services.xp_service import XPService  # type: ignore
except Exception:  # pragma: no cover - optional during import/test
    XPService = None  # type: ignore

try:
    from services.currency_service import get_primary_currency, ensure_wallet  # type: ignore
except Exception:  # pragma: no cover - optional during import/test
    get_primary_currency = None  # type: ignore
    ensure_wallet = None  # type: ignore

try:
    from services.currency_service import apply_transaction as currency_apply_transaction  # type: ignore
except Exception:  # pragma: no cover - older Keystone builds may not expose this
    currency_apply_transaction = None  # type: ignore


DEFAULT_CLAIM_WINDOW_HOURS = int(os.getenv("GIVEAWAY_CLAIM_WINDOW_HOURS", "48") or "48")
GIVEAWAY_CHECK_SECONDS = int(os.getenv("GIVEAWAY_CHECK_SECONDS", "60") or "60")
MAX_ENTRY_EMBED_LINES = 35

PRIZE_TYPE_CHOICES = [
    app_commands.Choice(name="Inventory Item", value="inventory_item"),
    app_commands.Choice(name="Currency", value="currency"),
    app_commands.Choice(name="XP", value="xp"),
    app_commands.Choice(name="Discord Role", value="role"),
    app_commands.Choice(name="Shop Item Package", value="shop_item"),
]

ENTRY_MODE_CHOICES = [
    app_commands.Choice(name="Per OC / Character", value="character"),
    app_commands.Choice(name="Per Discord Account", value="account"),
]

OC_PRIZE_TYPES = {"inventory_item", "currency", "xp", "shop_item"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dev_ids() -> set[int]:
    raw = (os.getenv("DEV_USER_IDS") or "").strip()
    if not raw:
        return set()
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


def _parse_duration(raw: str) -> timedelta:
    """Parse 30m, 2h, 1d, or 1w."""
    s = (raw or "").strip().lower()
    m = re.fullmatch(r"(\d+)([mhdw])", s)
    if not m:
        raise ValueError("Use duration like `30m`, `2h`, `1d`, or `1w`.")
    amount = int(m.group(1))
    unit = m.group(2)
    if amount <= 0:
        raise ValueError("Duration must be greater than zero.")
    mult = {"m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return timedelta(seconds=amount * mult)


def _dt(raw: Any) -> Optional[datetime]:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    txt = str(raw)
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(txt)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _short_id(value: str | None) -> str:
    if not value:
        return "unknown"
    return str(value).replace("-", "")[:8].upper()


def _truncate(s: Any, limit: int = 900) -> str:
    txt = str(s or "").strip()
    if not txt:
        return "—"
    return txt if len(txt) <= limit else txt[: limit - 1] + "…"


class GiveawayEntryView(discord.ui.View):
    def __init__(self, cog: "GiveawayCog", giveaway_id: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.giveaway_id = str(giveaway_id)
        self.add_item(
            discord.ui.Button(
                label="Enter Giveaway",
                emoji="🎟️",
                style=discord.ButtonStyle.success,
                custom_id=f"giveaway:enter:{self.giveaway_id}",
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Leave",
                emoji="🚪",
                style=discord.ButtonStyle.secondary,
                custom_id=f"giveaway:leave:{self.giveaway_id}",
            )
        )


class GiveawayClaimView(discord.ui.View):
    def __init__(self, cog: "GiveawayCog", giveaway_id: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.giveaway_id = str(giveaway_id)
        self.add_item(
            discord.ui.Button(
                label="Claim Prize",
                emoji="🎁",
                style=discord.ButtonStyle.primary,
                custom_id=f"giveaway:claim:{self.giveaway_id}",
            )
        )


class GiveawayCog(commands.GroupCog, group_name="giveaway", group_description="Giveaway tools"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._views_registered: set[str] = set()
        self.giveaway_check.change_interval(seconds=GIVEAWAY_CHECK_SECONDS)
        self.giveaway_check.start()
        super().__init__()

    # ─────────────────────────────────────────────────────────────
    # Lifecycle / Supabase / permissions
    # ─────────────────────────────────────────────────────────────
    async def cog_load(self):
        await self._register_persistent_views()
        self.bot.add_listener(self._on_interaction, "on_interaction")

    async def cog_unload(self):
        self.giveaway_check.cancel()
        try:
            self.bot.remove_listener(self._on_interaction, "on_interaction")
        except Exception:
            pass

    def sb(self):
        sb = getattr(self.bot, "supabase", None)
        if sb is None:
            raise RuntimeError("Supabase is not configured on the bot.")
        return sb

    def _staff_ok(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        if isinstance(interaction.user, discord.Member):
            if interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.manage_guild:
                return True
            staff_roles: set[int] = getattr(self.bot, "staff_role_ids", set())
            if any(r.id in staff_roles for r in interaction.user.roles):
                return True
        return int(interaction.user.id) in _parse_dev_ids()

    async def _private(self, interaction: discord.Interaction, msg: str):
        if interaction.response.is_done():
            return await interaction.followup.send(msg, ephemeral=True)
        return await interaction.response.send_message(msg, ephemeral=True)

    async def _public(
        self,
        interaction: discord.Interaction,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        ephemeral: bool = False,
    ):
        kwargs: dict[str, Any] = {"ephemeral": ephemeral}
        if content is not None:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed
        if view is not None:
            kwargs["view"] = view
        if interaction.response.is_done():
            return await interaction.followup.send(**kwargs)
        return await interaction.response.send_message(**kwargs)

    def _rows(self, res: Any) -> list[dict]:
        return getattr(res, "data", None) or []

    def _row(self, res: Any) -> Optional[dict]:
        rows = self._rows(res)
        return rows[0] if rows else None

    async def _register_persistent_views(self):
        try:
            sb = self.sb()
            active = (
                sb.table("giveaways")
                .select("giveaway_id,status")
                .in_("status", ["ACTIVE", "ENDED"])
                .limit(500)
                .execute()
            )
            for row in self._rows(active):
                gid = str(row["giveaway_id"])
                if gid in self._views_registered:
                    continue
                self.bot.add_view(GiveawayEntryView(self, gid))
                self.bot.add_view(GiveawayClaimView(self, gid))
                self._views_registered.add(gid)
        except Exception:
            traceback.print_exc()

    def _register_views_for(self, giveaway_id: str):
        gid = str(giveaway_id)
        if gid in self._views_registered:
            return
        self.bot.add_view(GiveawayEntryView(self, gid))
        self.bot.add_view(GiveawayClaimView(self, gid))
        self._views_registered.add(gid)

    # ─────────────────────────────────────────────────────────────
    # DB helpers
    # ─────────────────────────────────────────────────────────────
    def _get_giveaway(self, giveaway_id: str) -> Optional[dict]:
        return self._row(
            self.sb()
            .table("giveaways")
            .select("*")
            .eq("giveaway_id", str(giveaway_id))
            .limit(1)
            .execute()
        )

    def _find_giveaway(self, raw: str, guild_id: int) -> Optional[dict]:
        """Resolve a giveaway by full UUID, short UUID prefix, displayed short code, or message ID.

        Supabase/PostgREST does not reliably support ILIKE directly on uuid columns,
        so short-code lookup is done Python-side against recent guild giveaways.
        """
        lookup = (raw or "").strip()
        if not lookup:
            return None

        sb = self.sb()
        base = sb.table("giveaways").select("*").eq("guild_id", int(guild_id))

        # Message ID lookup.
        if lookup.isdigit():
            row = self._row(base.eq("message_id", int(lookup)).limit(1).execute())
            if row:
                return row

        # Full UUID lookup.
        if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", lookup):
            row = self._row(base.eq("giveaway_id", lookup.lower()).limit(1).execute())
            if row:
                return row

        # Short displayed ID lookup, e.g. B7C1AD4E.
        # Normalize away hyphens/spaces and compare to UUID prefixes.
        norm = re.sub(r"[^0-9a-fA-F]", "", lookup).lower()
        if not norm:
            return None

        try:
            rows = self._rows(
                sb.table("giveaways")
                .select("*")
                .eq("guild_id", int(guild_id))
                .order("created_at", desc=True)
                .limit(200)
                .execute()
            )
        except Exception:
            traceback.print_exc()
            return None

        matches: list[dict] = []
        for row in rows:
            gid_norm = re.sub(r"[^0-9a-fA-F]", "", str(row.get("giveaway_id") or "")).lower()
            if gid_norm.startswith(norm):
                matches.append(row)

        if not matches:
            return None

        # If staff only typed a very short prefix and multiple giveaways match, return
        # the newest one rather than failing; /giveaway status can confirm it.
        return matches[0]

    def _audit(
        self,
        *,
        guild_id: int,
        action: str,
        giveaway_id: str | None = None,
        winner_id: str | None = None,
        actor_discord_id: int | None = None,
        target_discord_id: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        try:
            self.sb().table("giveaway_audit_logs").insert(
                {
                    "giveaway_id": str(giveaway_id) if giveaway_id else None,
                    "winner_id": str(winner_id) if winner_id else None,
                    "guild_id": int(guild_id),
                    "actor_discord_id": int(actor_discord_id) if actor_discord_id else None,
                    "target_discord_id": int(target_discord_id) if target_discord_id else None,
                    "action": action,
                    "details": details or {},
                    "created_at": _utcnow().isoformat(),
                }
            ).execute()
        except Exception:
            traceback.print_exc()

    # ─────────────────────────────────────────────────────────────
    # Autocomplete helpers
    # ─────────────────────────────────────────────────────────────
    async def _prize_ref_autocomplete(self, interaction: discord.Interaction, current: str):
        if not interaction.guild:
            return []
        sb = self.sb()
        gid = int(interaction.guild.id)
        q = (current or "").strip().lower()
        prize_type = str(getattr(interaction.namespace, "prize_type", "") or "")

        try:
            if prize_type == "inventory_item":
                res = (
                    sb.table("items")
                    .select("item_id,name,item_class,is_active")
                    .eq("guild_id", gid)
                    .eq("is_active", True)
                    .order("name", desc=False)
                    .limit(50)
                    .execute()
                )
                out = []
                for r in self._rows(res):
                    name = str(r.get("name") or "Item")
                    if q and q not in name.lower():
                        continue
                    item_class = str(r.get("item_class") or "misc")
                    out.append(app_commands.Choice(name=f"{name[:65]} • {item_class[:20]}", value=str(r["item_id"])))
                return out[:25]

            if prize_type == "currency":
                res = (
                    sb.table("currencies")
                    .select("currency_id,name,ticker,emoji")
                    .eq("guild_id", gid)
                    .order("name", desc=False)
                    .limit(50)
                    .execute()
                )
                out = [app_commands.Choice(name="Primary Currency", value="primary")]
                for r in self._rows(res):
                    name = str(r.get("name") or "Currency")
                    ticker = str(r.get("ticker") or "")
                    if q and q not in f"{name} {ticker}".lower():
                        continue
                    out.append(app_commands.Choice(name=f"{name[:60]} • {ticker[:12]}", value=str(r["currency_id"])))
                return out[:25]

            if prize_type == "shop_item":
                res = (
                    sb.table("shop_items")
                    .select("item_id,name,price,is_active")
                    .eq("guild_id", gid)
                    .eq("is_active", True)
                    .order("created_at", desc=True)
                    .limit(50)
                    .execute()
                )
                out = []
                for r in self._rows(res):
                    name = str(r.get("name") or "Shop Item")
                    if q and q not in name.lower():
                        continue
                    out.append(app_commands.Choice(name=f"{name[:70]} • shop item", value=str(r["item_id"])))
                return out[:25]
        except Exception:
            traceback.print_exc()
        return []

    async def _giveaway_autocomplete(self, interaction: discord.Interaction, current: str):
        """Autocomplete recent giveaways by short code, prize, or status."""
        if not interaction.guild:
            return []
        q = (current or "").strip().lower()
        try:
            rows = self._rows(
                self.sb()
                .table("giveaways")
                .select("giveaway_id,prize_name,prize_amount,prize_type,status,created_at,ends_at")
                .eq("guild_id", int(interaction.guild.id))
                .order("created_at", desc=True)
                .limit(50)
                .execute()
            )
        except Exception:
            traceback.print_exc()
            return []

        out: list[app_commands.Choice[str]] = []
        for g in rows:
            short = _short_id(str(g.get("giveaway_id") or ""))
            prize = self._prize_label(g)
            status = str(g.get("status") or "?").upper()
            hay = f"{short} {prize} {status}".lower()
            if q and q not in hay:
                continue
            # Keep the displayed label useful but under Discord's 100-char limit.
            label = f"{short} • {status} • {prize}"
            out.append(app_commands.Choice(name=label[:100], value=short))
        return out[:25]

    # ─────────────────────────────────────────────────────────────
    # Display helpers
    # ─────────────────────────────────────────────────────────────
    def _prize_label(self, g: dict) -> str:
        ptype = str(g.get("prize_type") or "")
        amount = int(g.get("prize_amount") or 1)
        name = str(g.get("prize_name") or "Prize")
        if ptype == "xp":
            return f"{amount} XP"
        if ptype == "currency":
            return f"{amount} {name}"
        if amount == 1:
            return name
        return f"{name} x{amount}"

    def _build_giveaway_embed(self, g: dict, *, ended: bool = False) -> discord.Embed:
        end_at = _dt(g.get("ends_at")) or _utcnow()
        status = str(g.get("status") or "ACTIVE")
        color = discord.Color.gold() if status == "ACTIVE" else discord.Color.green()
        embed = discord.Embed(
            title=str(g.get("title") or f"🎉 {self._prize_label(g)} Giveaway"),
            color=color,
            timestamp=end_at,
        )
        if status == "ACTIVE":
            embed.description = (
                f"Prize: **{self._prize_label(g)}**\n"
                f"Entry mode: `{g.get('entry_mode')}`\n"
                f"Winners: `{int(g.get('winner_count') or 1)}`\n"
                f"Ends: <t:{int(end_at.timestamp())}:R>"
            )
        else:
            embed.description = f"Prize: **{self._prize_label(g)}**\nStatus: `{status}`"
        embed.add_field(name="Hosted by", value=f"<@{int(g.get('host_discord_id') or 0)}>", inline=True)
        embed.add_field(name="Giveaway ID", value=f"`{_short_id(str(g.get('giveaway_id')))}...`", inline=True)
        embed.set_footer(text="Use the buttons below to enter/leave or claim if you win.")
        return embed

    async def _update_entry_message(self, guild: discord.Guild, g: dict):
        if str(g.get("status")) != "ACTIVE":
            return
        channel = guild.get_channel(int(g["channel_id"]))
        if channel is None:
            try:
                channel = await guild.fetch_channel(int(g["channel_id"]))
            except Exception:
                return
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        rows = self._rows(
            self.sb()
            .table("giveaway_entries")
            .select("user_discord_id,character_name,entry_status")
            .eq("giveaway_id", str(g["giveaway_id"]))
            .eq("entry_status", "ACTIVE")
            .order("created_at", desc=False)
            .limit(250)
            .execute()
        )
        embed = discord.Embed(title=f"🎟️ Entries — {self._prize_label(g)}", color=discord.Color.blurple())
        if not rows:
            embed.description = "No entries yet. Click **Enter Giveaway** on the giveaway post."
        else:
            lines = []
            for r in rows[:MAX_ENTRY_EMBED_LINES]:
                cname = str(r.get("character_name") or "").strip()
                if cname:
                    lines.append(f"• **{cname}** — <@{int(r['user_discord_id'])}>")
                else:
                    lines.append(f"• <@{int(r['user_discord_id'])}>")
            more = len(rows) - len(lines)
            embed.description = "\n".join(lines)
            embed.add_field(name="Total Entries", value=f"`{len(rows)}`", inline=True)
            if more > 0:
                embed.add_field(name="Hidden", value=f"`+{more}` more", inline=True)
        embed.timestamp = discord.utils.utcnow()

        entry_id = g.get("entry_message_id")
        try:
            if entry_id:
                msg = await channel.fetch_message(int(entry_id))
                await msg.edit(embed=embed)
                return
        except Exception:
            pass

        try:
            msg = await channel.send(embed=embed)
            self.sb().table("giveaways").update({"entry_message_id": int(msg.id)}).eq("giveaway_id", str(g["giveaway_id"])).execute()
        except Exception:
            traceback.print_exc()

    # ─────────────────────────────────────────────────────────────
    # Interaction handler
    # ─────────────────────────────────────────────────────────────
    async def _on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        data = interaction.data or {}
        custom_id = str(data.get("custom_id") or "")
        if not custom_id.startswith("giveaway:"):
            return

        parts = custom_id.split(":", 2)
        if len(parts) != 3:
            return
        action, giveaway_id = parts[1], parts[2]

        try:
            if action == "enter":
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
                await self._enter_giveaway(interaction, giveaway_id)
            elif action == "leave":
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
                await self._leave_giveaway(interaction, giveaway_id)
            elif action == "claim":
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
                await self._claim_prize(interaction, giveaway_id)
        except Exception:
            traceback.print_exc()
            try:
                await self._private(interaction, "Server error handling giveaway button.")
            except Exception:
                pass

    # ─────────────────────────────────────────────────────────────
    # Entry logic
    # ─────────────────────────────────────────────────────────────
    async def _enter_giveaway(self, interaction: discord.Interaction, giveaway_id: str):
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        sb = self.sb()
        g = self._get_giveaway(giveaway_id)
        if not g or int(g.get("guild_id") or 0) != int(interaction.guild.id):
            return await self._private(interaction, "Giveaway not found.")
        if str(g.get("status")) != "ACTIVE":
            return await self._private(interaction, "This giveaway is no longer active.")
        end_at = _dt(g.get("ends_at")) or _utcnow()
        if _utcnow() >= end_at:
            return await self._private(interaction, "This giveaway has ended.")

        entry_mode = str(g.get("entry_mode") or "character")
        user_id = int(interaction.user.id)
        character_id: str | None = None
        character_name: str | None = None

        if entry_mode == "character":
            active = get_active_character(sb, user_id)
            if not active or not active.get("character_id"):
                return await self._private(interaction, "No active OC set. Use `/oc select <name>` before entering.")
            character_id = str(active["character_id"])
            character_name = str(active.get("name") or "Unknown OC")

        # Per-account giveaways only allow one active entry per user.
        if entry_mode == "account":
            existing = self._row(
                sb.table("giveaway_entries")
                .select("entry_id,entry_status")
                .eq("giveaway_id", str(giveaway_id))
                .eq("user_discord_id", user_id)
                .is_("character_id", "null")
                .limit(1)
                .execute()
            )
            if existing:
                sb.table("giveaway_entries").update({"entry_status": "ACTIVE", "updated_at": _utcnow().isoformat()}).eq("entry_id", str(existing["entry_id"])).execute()
            else:
                sb.table("giveaway_entries").insert(
                    {
                        "giveaway_id": str(giveaway_id),
                        "guild_id": int(interaction.guild.id),
                        "user_discord_id": user_id,
                        "character_id": None,
                        "character_name": None,
                        "entry_source": "button",
                        "entry_status": "ACTIVE",
                    }
                ).execute()
            await self._update_entry_message(interaction.guild, g)
            return await self._private(interaction, "✅ You entered the giveaway.")

        # Character mode: one active entry per OC.
        existing = self._row(
            sb.table("giveaway_entries")
            .select("entry_id,entry_status")
            .eq("giveaway_id", str(giveaway_id))
            .eq("user_discord_id", user_id)
            .eq("character_id", character_id)
            .limit(1)
            .execute()
        )
        if existing:
            sb.table("giveaway_entries").update(
                {
                    "entry_status": "ACTIVE",
                    "character_name": character_name,
                    "updated_at": _utcnow().isoformat(),
                }
            ).eq("entry_id", str(existing["entry_id"])).execute()
        else:
            sb.table("giveaway_entries").insert(
                {
                    "giveaway_id": str(giveaway_id),
                    "guild_id": int(interaction.guild.id),
                    "user_discord_id": user_id,
                    "character_id": character_id,
                    "character_name": character_name,
                    "entry_source": "button",
                    "entry_status": "ACTIVE",
                }
            ).execute()

        await self._update_entry_message(interaction.guild, g)
        return await self._private(interaction, f"✅ Entered as **{character_name}**.")

    async def _leave_giveaway(self, interaction: discord.Interaction, giveaway_id: str):
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        sb = self.sb()
        g = self._get_giveaway(giveaway_id)
        if not g or int(g.get("guild_id") or 0) != int(interaction.guild.id):
            return await self._private(interaction, "Giveaway not found.")
        if str(g.get("status")) != "ACTIVE":
            return await self._private(interaction, "This giveaway is no longer active.")

        user_id = int(interaction.user.id)
        if str(g.get("entry_mode")) == "character":
            active = get_active_character(sb, user_id)
            if not active or not active.get("character_id"):
                return await self._private(interaction, "No active OC set. I don’t know which OC to remove.")
            q = sb.table("giveaway_entries").update({"entry_status": "LEFT"}).eq("giveaway_id", str(giveaway_id)).eq("user_discord_id", user_id).eq("character_id", str(active["character_id"]))
            label = f"**{active.get('name','your OC')}**"
        else:
            q = sb.table("giveaway_entries").update({"entry_status": "LEFT"}).eq("giveaway_id", str(giveaway_id)).eq("user_discord_id", user_id).is_("character_id", "null")
            label = "the giveaway"
        q.execute()
        await self._update_entry_message(interaction.guild, g)
        return await self._private(interaction, f"🚪 Removed {label} from the giveaway.")

    # ─────────────────────────────────────────────────────────────
    # Prize validation / fulfillment / reversal
    # ─────────────────────────────────────────────────────────────
    def _get_currency(self, sb, guild_id: int, ref: str | None) -> dict:
        if (not ref or str(ref).lower() == "primary") and get_primary_currency is not None:
            cur = get_primary_currency(sb, guild_id)
            if cur:
                return cur
        if not ref or str(ref).lower() == "primary":
            raise RuntimeError("No primary currency helper configured.")
        cur = self._row(sb.table("currencies").select("*").eq("guild_id", int(guild_id)).eq("currency_id", str(ref)).limit(1).execute())
        if not cur:
            raise RuntimeError("Currency not found.")
        return cur

    def _wallet_balance(self, sb, *, character_id: str, currency_id: str) -> int:
        row = self._row(
            sb.table("wallets")
            .select("balance")
            .eq("character_id", str(character_id))
            .eq("currency_id", str(currency_id))
            .limit(1)
            .execute()
        )
        return int((row or {}).get("balance") or 0)

    def _currency_delta(
        self,
        sb,
        *,
        guild_id: int,
        character_id: str,
        currency_id: str,
        delta: int,
        actor_discord_id: int,
        reason: str,
    ) -> None:
        if delta == 0:
            return
        if ensure_wallet is not None:
            ensure_wallet(sb, character_id, currency_id)

        amount = abs(int(delta))
        if currency_apply_transaction is not None:
            tx_type = "MINT" if delta > 0 else "BURN"
            kwargs = {
                "guild_id": int(guild_id),
                "currency_id": str(currency_id),
                "from_character_id": str(character_id) if delta < 0 else None,
                "to_character_id": str(character_id) if delta > 0 else None,
                "amount": amount,
                "actor_discord_id": int(actor_discord_id),
                "tx_type": tx_type,
                "reason": reason,
            }
            currency_apply_transaction(sb, **kwargs)
            return

        # Fallback for builds that do not expose currency_service.apply_transaction.
        current = self._wallet_balance(sb, character_id=str(character_id), currency_id=str(currency_id))
        new_balance = current + int(delta)
        if new_balance < 0:
            raise RuntimeError("INSUFFICIENT_FUNDS")
        sb.table("wallets").update({"balance": int(new_balance)}).eq("character_id", str(character_id)).eq("currency_id", str(currency_id)).execute()

        # Best-effort transaction log. If your transactions schema differs, fulfillment still succeeds.
        try:
            sb.table("transactions").insert(
                {
                    "guild_id": int(guild_id),
                    "currency_id": str(currency_id),
                    "from_character_id": str(character_id) if delta < 0 else None,
                    "to_character_id": str(character_id) if delta > 0 else None,
                    "amount": amount,
                    "actor_discord_id": int(actor_discord_id),
                    "tx_type": "MINT" if delta > 0 else "BURN",
                    "reason": reason,
                    "created_at": _utcnow().isoformat(),
                }
            ).execute()
        except Exception:
            pass

    def _inventory_delta(
        self,
        sb,
        *,
        guild_id: int,
        character_id: str,
        item_id: str,
        delta: int,
        actor_discord_id: int,
        note: str,
    ) -> None:
        if inventory_service is None or not hasattr(inventory_service, "apply_delta"):
            raise RuntimeError("Inventory service missing apply_delta.")
        inventory_service.apply_delta(
            sb,
            guild_id=int(guild_id),
            character_id=str(character_id),
            item_id=str(item_id),
            delta=int(delta),
            actor_discord_id=int(actor_discord_id),
            context="GIVEAWAY",
            note=note,
        )

    def _xp_award(self, sb, *, guild_id: int, character_id: str, amount: int, actor_discord_id: int, giveaway_id: str, prize_name: str) -> None:
        if XPService is None:
            raise RuntimeError("XPService is not available.")
        xp = XPService(sb)
        xp.award_xp(
            guild_id=int(guild_id),
            character_id=str(character_id),
            amount=int(amount),
            source="staff",
            title=f"Giveaway: {prize_name}",
            actor_discord_id=int(actor_discord_id),
            external_ref=str(giveaway_id),
            notes="Giveaway prize fulfillment",
        )

    def _xp_reverse(self, sb, *, guild_id: int, character_id: str, amount: int, actor_discord_id: int, giveaway_id: str, prize_name: str) -> None:
        """Reverse an XP giveaway award.

        Keystone's current XP schema uses:
        - public.oc_xp_wallets
        - public.oc_xp_transactions

        The giveaway cog should not require changes to the XP cog for rerolls; it just
        performs a conservative wallet reversal here and writes a best-effort audit row.
        """
        amount = int(amount)
        if amount <= 0:
            return

        wallet = self._row(
            sb.table("oc_xp_wallets")
            .select("*")
            .eq("guild_id", int(guild_id))
            .eq("character_id", str(character_id))
            .limit(1)
            .execute()
        )
        if not wallet:
            raise RuntimeError("XP_WALLET_NOT_FOUND")

        available = int(wallet.get("available_xp") or 0)
        if available < amount:
            raise RuntimeError("INSUFFICIENT_XP")

        patch = {"available_xp": available - amount}

        # This is a true reversal of an award, not a player purchase, so reduce
        # total_earned_xp when that column exists instead of inflating total_spent_xp.
        if wallet.get("total_earned_xp") is not None:
            patch["total_earned_xp"] = max(0, int(wallet.get("total_earned_xp") or 0) - amount)

        sb.table("oc_xp_wallets").update(patch).eq("guild_id", int(guild_id)).eq("character_id", str(character_id)).execute()

        # Best-effort transaction row for history. If the exact transaction schema/checks
        # differ, do not block the reversal after the wallet has already been corrected.
        try:
            sb.table("oc_xp_transactions").insert(
                {
                    "guild_id": int(guild_id),
                    "character_id": str(character_id),
                    "direction": "spend",
                    "amount": amount,
                    "source": "staff",
                    "reference_type": "reward",
                    "reference_key": f"giveaway:{_short_id(giveaway_id)}",
                    "reason": f"Giveaway prize reversed after reroll: {prize_name}",
                    "actor_discord_id": int(actor_discord_id),
                    "metadata": {"giveaway_id": str(giveaway_id), "prize_name": str(prize_name)},
                    "created_at": _utcnow().isoformat(),
                }
            ).execute()
        except Exception as e:
            print(f"[giveaway xp reverse] wallet reversed but history insert failed: {e}")

    async def _role_delta(self, guild: discord.Guild, *, user_id: int, role_id: int, add: bool, reason: str) -> None:
        member = guild.get_member(int(user_id))
        if member is None:
            member = await guild.fetch_member(int(user_id))
        role = guild.get_role(int(role_id))
        if not member or not role:
            raise RuntimeError("Member or role not found.")
        if add:
            await member.add_roles(role, reason=reason)
        else:
            await member.remove_roles(role, reason=reason)

    def _shop_item_row(self, sb, guild_id: int, item_id: str) -> dict:
        row = self._row(
            sb.table("shop_items")
            .select("*")
            .eq("guild_id", int(guild_id))
            .eq("item_id", str(item_id))
            .limit(1)
            .execute()
        )
        if not row:
            raise RuntimeError("Shop item not found.")
        return row

    def _adjust_shop_stock(self, sb, *, guild_id: int, shop_item_id: str, delta: int) -> None:
        it = self._shop_item_row(sb, guild_id, shop_item_id)
        stock = it.get("stock")
        if stock is None:
            return
        new_stock = int(stock) + int(delta)
        if new_stock < 0:
            raise RuntimeError("INSUFFICIENT_SHOP_STOCK")
        sb.table("shop_items").update({"stock": int(new_stock)}).eq("guild_id", int(guild_id)).eq("item_id", str(shop_item_id)).execute()

    async def _fulfill_winner(self, guild: discord.Guild, g: dict, winner: dict, *, actor_discord_id: int) -> None:
        sb = self.sb()
        gid = str(g["giveaway_id"])
        winner_id = str(winner["winner_id"])

        # Fresh DB guard so repeated/double-clicked rerolls do not reverse the same
        # fulfilled prize twice.
        fresh = self._row(
            sb.table("giveaway_winners")
            .select("winner_id,claim_status,fulfillment_status")
            .eq("winner_id", winner_id)
            .limit(1)
            .execute()
        )
        if fresh and str(fresh.get("fulfillment_status") or "") == "REVERSED":
            return

        ptype = str(g["prize_type"])
        ref = str(g.get("prize_ref_id") or "").strip() or None
        amount = int(g.get("prize_amount") or 1)
        char_id = str(winner.get("character_id") or "") or None
        user_id = int(winner["user_discord_id"])
        note = f"giveaway={_short_id(gid)} winner={_short_id(winner_id)} prize={self._prize_label(g)}"

        try:
            if ptype == "inventory_item":
                if not char_id:
                    raise RuntimeError("No character_id on winner.")
                if not ref:
                    raise RuntimeError("No inventory item prize_ref_id.")
                self._inventory_delta(sb, guild_id=int(g["guild_id"]), character_id=char_id, item_id=ref, delta=amount, actor_discord_id=actor_discord_id, note=note)

            elif ptype == "currency":
                if not char_id:
                    raise RuntimeError("No character_id on winner.")
                cur = self._get_currency(sb, int(g["guild_id"]), ref)
                self._currency_delta(
                    sb,
                    guild_id=int(g["guild_id"]),
                    character_id=char_id,
                    currency_id=str(cur["currency_id"]),
                    delta=amount,
                    actor_discord_id=actor_discord_id,
                    reason=note,
                )

            elif ptype == "xp":
                if not char_id:
                    raise RuntimeError("No character_id on winner.")
                self._xp_award(sb, guild_id=int(g["guild_id"]), character_id=char_id, amount=amount, actor_discord_id=actor_discord_id, giveaway_id=gid, prize_name=str(g.get("prize_name") or "Giveaway"))

            elif ptype == "role":
                role_id = int(ref or 0)
                if role_id <= 0:
                    raise RuntimeError("No role configured.")
                await self._role_delta(guild, user_id=user_id, role_id=role_id, add=True, reason=note)

            elif ptype == "shop_item":
                if not char_id:
                    raise RuntimeError("No character_id on winner.")
                if not ref:
                    raise RuntimeError("No shop item prize_ref_id.")
                shop_item = self._shop_item_row(sb, int(g["guild_id"]), ref)
                self._adjust_shop_stock(sb, guild_id=int(g["guild_id"]), shop_item_id=ref, delta=-amount)
                granted_any = False
                grants_item_id = str(shop_item.get("grants_item_id") or "").strip() or None
                grants_qty = int(shop_item.get("grants_qty") or 0)
                if grants_item_id and grants_qty > 0:
                    self._inventory_delta(
                        sb,
                        guild_id=int(g["guild_id"]),
                        character_id=char_id,
                        item_id=grants_item_id,
                        delta=grants_qty * amount,
                        actor_discord_id=actor_discord_id,
                        note=note + f" shop_item={_short_id(ref)}",
                    )
                    granted_any = True
                role_id = shop_item.get("role_id")
                if role_id:
                    await self._role_delta(guild, user_id=user_id, role_id=int(role_id), add=True, reason=note)
                    granted_any = True
                if not granted_any:
                    raise RuntimeError("Shop item has no grants_item_id/grants_qty or role_id to fulfill.")

            else:
                raise RuntimeError(f"Unsupported prize type: {ptype}")

            sb.table("giveaway_winners").update(
                {
                    "fulfillment_status": "FULFILLED",
                    "fulfilled_at": _utcnow().isoformat(),
                    "fulfillment_error": None,
                }
            ).eq("winner_id", winner_id).execute()
            self._audit(guild_id=int(g["guild_id"]), giveaway_id=gid, winner_id=winner_id, actor_discord_id=actor_discord_id, target_discord_id=user_id, action="FULFILL", details={"prize_type": ptype, "amount": amount, "ref": ref})

        except Exception as e:
            err = str(e)
            traceback.print_exc()
            sb.table("giveaway_winners").update(
                {"fulfillment_status": "FAILED", "fulfillment_error": err[:1000]}
            ).eq("winner_id", winner_id).execute()
            self._audit(guild_id=int(g["guild_id"]), giveaway_id=gid, winner_id=winner_id, actor_discord_id=actor_discord_id, target_discord_id=user_id, action="FULFILL_FAILED", details={"error": err})
            raise

    async def _reverse_winner(self, guild: discord.Guild, g: dict, winner: dict, *, actor_discord_id: int) -> None:
        sb = self.sb()
        gid = str(g["giveaway_id"])
        winner_id = str(winner["winner_id"])
        ptype = str(g["prize_type"])
        ref = str(g.get("prize_ref_id") or "").strip() or None
        amount = int(g.get("prize_amount") or 1)
        char_id = str(winner.get("character_id") or "") or None
        user_id = int(winner["user_discord_id"])
        note = f"reversal giveaway={_short_id(gid)} winner={_short_id(winner_id)} prize={self._prize_label(g)}"

        try:
            if str(winner.get("fulfillment_status")) not in ("FULFILLED", "REVERSAL_FAILED"):
                sb.table("giveaway_winners").update({"claim_status": "REROLLED"}).eq("winner_id", winner_id).execute()
                return

            if ptype == "inventory_item":
                if not char_id or not ref:
                    raise RuntimeError("Missing character/item for reversal.")
                self._inventory_delta(sb, guild_id=int(g["guild_id"]), character_id=char_id, item_id=ref, delta=-amount, actor_discord_id=actor_discord_id, note=note)

            elif ptype == "currency":
                if not char_id:
                    raise RuntimeError("Missing character for reversal.")
                cur = self._get_currency(sb, int(g["guild_id"]), ref)
                self._currency_delta(
                    sb,
                    guild_id=int(g["guild_id"]),
                    character_id=char_id,
                    currency_id=str(cur["currency_id"]),
                    delta=-amount,
                    actor_discord_id=actor_discord_id,
                    reason=note,
                )

            elif ptype == "xp":
                if not char_id:
                    raise RuntimeError("Missing character for reversal.")
                self._xp_reverse(sb, guild_id=int(g["guild_id"]), character_id=char_id, amount=amount, actor_discord_id=actor_discord_id, giveaway_id=gid, prize_name=str(g.get("prize_name") or "Giveaway"))

            elif ptype == "role":
                role_id = int(ref or 0)
                if role_id <= 0:
                    raise RuntimeError("No role configured.")
                await self._role_delta(guild, user_id=user_id, role_id=role_id, add=False, reason=note)

            elif ptype == "shop_item":
                if not char_id or not ref:
                    raise RuntimeError("Missing character/shop item for reversal.")
                shop_item = self._shop_item_row(sb, int(g["guild_id"]), ref)
                grants_item_id = str(shop_item.get("grants_item_id") or "").strip() or None
                grants_qty = int(shop_item.get("grants_qty") or 0)
                if grants_item_id and grants_qty > 0:
                    self._inventory_delta(
                        sb,
                        guild_id=int(g["guild_id"]),
                        character_id=char_id,
                        item_id=grants_item_id,
                        delta=-(grants_qty * amount),
                        actor_discord_id=actor_discord_id,
                        note=note + f" shop_item={_short_id(ref)}",
                    )
                role_id = shop_item.get("role_id")
                if role_id:
                    await self._role_delta(guild, user_id=user_id, role_id=int(role_id), add=False, reason=note)
                self._adjust_shop_stock(sb, guild_id=int(g["guild_id"]), shop_item_id=ref, delta=amount)

            else:
                raise RuntimeError(f"Unsupported prize type: {ptype}")

            sb.table("giveaway_winners").update(
                {
                    "claim_status": "REROLLED",
                    "fulfillment_status": "REVERSED",
                    "reversed_at": _utcnow().isoformat(),
                    "reversal_error": None,
                }
            ).eq("winner_id", winner_id).execute()
            self._audit(guild_id=int(g["guild_id"]), giveaway_id=gid, winner_id=winner_id, actor_discord_id=actor_discord_id, target_discord_id=user_id, action="REVERSE", details={"prize_type": ptype, "amount": amount, "ref": ref})

        except Exception as e:
            err = str(e)
            traceback.print_exc()
            sb.table("giveaway_winners").update(
                {"fulfillment_status": "REVERSAL_FAILED", "reversal_error": err[:1000]}
            ).eq("winner_id", winner_id).execute()
            self._audit(guild_id=int(g["guild_id"]), giveaway_id=gid, winner_id=winner_id, actor_discord_id=actor_discord_id, target_discord_id=user_id, action="REVERSAL_FAILED", details={"error": err})
            raise

    # ─────────────────────────────────────────────────────────────
    # Draw / finalize / claim
    # ─────────────────────────────────────────────────────────────
    def _eligible_entries(self, g: dict, *, exclude_user_ids: set[int] | None = None, exclude_entry_ids: set[str] | None = None) -> list[dict]:
        rows = self._rows(
            self.sb()
            .table("giveaway_entries")
            .select("*")
            .eq("giveaway_id", str(g["giveaway_id"]))
            .eq("entry_status", "ACTIVE")
            .execute()
        )
        exclude_user_ids = exclude_user_ids or set()
        exclude_entry_ids = exclude_entry_ids or set()
        return [r for r in rows if int(r["user_discord_id"]) not in exclude_user_ids and str(r["entry_id"]) not in exclude_entry_ids]

    def _draw_entries(self, g: dict, entries: list[dict], winner_count: int) -> list[dict]:
        if not entries:
            return []
        if bool(g.get("allow_multiple_wins_per_account")):
            return random.sample(entries, min(int(winner_count), len(entries)))

        # Weighted by OC entry, but cannot win twice per Discord account in this draw.
        shuffled = entries[:]
        random.shuffle(shuffled)
        picked: list[dict] = []
        seen_users: set[int] = set()
        for row in shuffled:
            uid = int(row["user_discord_id"])
            if uid in seen_users:
                continue
            picked.append(row)
            seen_users.add(uid)
            if len(picked) >= int(winner_count):
                break
        return picked

    async def _finalize_giveaway(self, guild: discord.Guild, g: dict, *, actor_discord_id: int | None = None, reroll_draw_number: int | None = None, exclude_previous: bool = False) -> list[dict]:
        sb = self.sb()
        gid = str(g["giveaway_id"])
        actor_id = int(actor_discord_id or g.get("host_discord_id") or 0)

        exclude_users: set[int] = set()
        exclude_entries: set[str] = set()
        if exclude_previous:
            prev = self._rows(sb.table("giveaway_winners").select("user_discord_id,entry_id").eq("giveaway_id", gid).execute())
            exclude_users = {int(r["user_discord_id"]) for r in prev}
            exclude_entries = {str(r["entry_id"]) for r in prev if r.get("entry_id")}

        entries = self._eligible_entries(g, exclude_user_ids=exclude_users, exclude_entry_ids=exclude_entries)
        winners = self._draw_entries(g, entries, int(g.get("winner_count") or 1))
        claim_deadline = _utcnow() + timedelta(hours=int(g.get("claim_window_hours") or DEFAULT_CLAIM_WINDOW_HOURS))
        draw_number = int(reroll_draw_number or 1)

        inserted: list[dict] = []
        for entry in winners:
            row = {
                "giveaway_id": gid,
                "entry_id": str(entry.get("entry_id")) if entry.get("entry_id") else None,
                "guild_id": int(g["guild_id"]),
                "user_discord_id": int(entry["user_discord_id"]),
                "character_id": str(entry["character_id"]) if entry.get("character_id") else None,
                "character_name": entry.get("character_name"),
                "draw_number": draw_number,
                "claim_status": "PENDING",
                "fulfillment_status": "NOT_FULFILLED",
                "claim_deadline_at": claim_deadline.isoformat(),
            }
            res = sb.table("giveaway_winners").insert(row).execute()
            inserted.extend(self._rows(res))

        update = {
            "status": "ENDED",
            "ended_at": _utcnow().isoformat(),
            "claim_deadline_at": claim_deadline.isoformat(),
        }
        sb.table("giveaways").update(update).eq("giveaway_id", gid).execute()
        g.update(update)

        self._audit(guild_id=int(g["guild_id"]), giveaway_id=gid, actor_discord_id=actor_id, action="DRAW", details={"draw_number": draw_number, "winner_count": len(inserted)})
        await self._announce_winners(guild, g, inserted, draw_number=draw_number)
        return inserted

    async def _announce_winners(self, guild: discord.Guild, g: dict, winners: list[dict], *, draw_number: int):
        channel = guild.get_channel(int(g["channel_id"]))
        if channel is None:
            try:
                channel = await guild.fetch_channel(int(g["channel_id"]))
            except Exception:
                return
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        if winners:
            lines = []
            for w in winners:
                cname = str(w.get("character_name") or "").strip()
                if cname:
                    lines.append(f"• **{cname}** — <@{int(w['user_discord_id'])}>")
                else:
                    lines.append(f"• <@{int(w['user_discord_id'])}>")
            desc = "\n".join(lines)
            desc += f"\n\nClick **Claim Prize** within `{int(g.get('claim_window_hours') or DEFAULT_CLAIM_WINDOW_HOURS)}` hours."
            color = discord.Color.green()
        else:
            desc = "No eligible entries were found."
            color = discord.Color.red()

        title = f"🎉 {self._prize_label(g)} — Winner{'s' if len(winners) != 1 else ''}!"
        if draw_number > 1:
            title = f"🔁 Reroll #{draw_number} — {self._prize_label(g)}"
        embed = discord.Embed(title=title, description=desc, color=color, timestamp=discord.utils.utcnow())
        embed.add_field(name="Giveaway ID", value=f"`{_short_id(str(g['giveaway_id']))}`", inline=True)
        embed.add_field(name="Prize", value=f"**{self._prize_label(g)}**", inline=True)

        # Important: Discord does not reliably send push notifications for mentions
        # placed only inside embeds. Put winner mentions in normal message content too.
        ping_content = None
        if winners:
            seen: set[int] = set()
            mentions: list[str] = []
            for w in winners:
                uid = int(w["user_discord_id"])
                if uid in seen:
                    continue
                seen.add(uid)
                mentions.append(f"<@{uid}>")

            mention_text = " ".join(mentions)
            if draw_number > 1:
                ping_content = f"🔁 Reroll winner{'s' if len(mentions) != 1 else ''}: {mention_text} — please claim your prize!"
            else:
                ping_content = f"🎉 Winner{'s' if len(mentions) != 1 else ''}: {mention_text} — please claim your prize!"

            if len(ping_content) > 1900:
                ping_content = ping_content[:1897] + "..."

        try:
            await channel.send(
                content=ping_content,
                embed=embed,
                view=GiveawayClaimView(self, str(g["giveaway_id"])),
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except Exception:
            traceback.print_exc()

        try:
            msg = await channel.fetch_message(int(g.get("message_id"))) if g.get("message_id") else None
            if msg and msg.embeds:
                main = msg.embeds[0]
                main.color = color
                field_name = "🎊 Winners" if draw_number == 1 else f"🔁 Reroll #{draw_number}"
                main.add_field(name=field_name, value=desc[:1000], inline=False)
                await msg.edit(embed=main, view=GiveawayClaimView(self, str(g["giveaway_id"])))
        except Exception:
            pass

    async def _claim_prize(self, interaction: discord.Interaction, giveaway_id: str):
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        sb = self.sb()
        g = self._get_giveaway(giveaway_id)
        if not g:
            return await self._private(interaction, "Giveaway not found.")

        now = _utcnow()

        # Do NOT only query claim_status=PENDING here.
        # A previous fulfillment failure may have left an older row as CLAIMED + FAILED.
        # Those rows must stay retryable after a bot restart.
        all_rows = self._rows(
            sb.table("giveaway_winners")
            .select("*")
            .eq("giveaway_id", str(giveaway_id))
            .eq("user_discord_id", int(interaction.user.id))
            .order("created_at", desc=True)
            .execute()
        )

        if not all_rows:
            return await self._private(interaction, "You do not have a prize for this giveaway.")

        rows: list[dict] = []
        already_claimed = False
        for r in all_rows:
            claim_status = str(r.get("claim_status") or "")
            fulfillment_status = str(r.get("fulfillment_status") or "")

            # Normal claim path.
            if claim_status == "PENDING":
                rows.append(r)
                continue

            # Recovery path for the bug where claim_status was marked CLAIMED
            # before the XP/item/currency/role fulfillment actually succeeded.
            if claim_status == "CLAIMED" and fulfillment_status == "FAILED":
                rows.append(r)
                continue

            if claim_status == "CLAIMED" and fulfillment_status == "FULFILLED":
                already_claimed = True

        if not rows:
            if already_claimed:
                return await self._private(interaction, "You already claimed this prize.")
            return await self._private(interaction, "You do not have a pending prize to claim for this giveaway.")

        # If one account somehow has multiple eligible winners, prefer their active OC match.
        winner = rows[0]
        active = get_active_character(sb, int(interaction.user.id))
        if active and active.get("character_id"):
            for r in rows:
                if str(r.get("character_id") or "") == str(active["character_id"]):
                    winner = r
                    break

        claim_status = str(winner.get("claim_status") or "")
        fulfillment_status = str(winner.get("fulfillment_status") or "")

        # If fulfillment already succeeded but claim_status was never finalized, do not grant twice.
        if fulfillment_status == "FULFILLED":
            sb.table("giveaway_winners").update(
                {"claim_status": "CLAIMED", "claimed_at": winner.get("claimed_at") or now.isoformat()}
            ).eq("winner_id", str(winner["winner_id"])).execute()
            return await self._private(interaction, f"🎁 Claimed **{self._prize_label(g)}** successfully!")

        # Only expire truly untouched pending claims. Failed fulfillment rows stay retryable,
        # because the player already tried and the bot/system was the reason it failed.
        deadline = _dt(winner.get("claim_deadline_at"))
        if claim_status == "PENDING" and fulfillment_status != "FAILED" and deadline and now > deadline:
            sb.table("giveaway_winners").update({"claim_status": "EXPIRED"}).eq("winner_id", str(winner["winner_id"])).execute()
            return await self._private(interaction, "This claim window has expired. Staff can reroll it.")

        try:
            await self._fulfill_winner(interaction.guild, g, winner, actor_discord_id=int(interaction.user.id))
        except Exception as e:
            # Keep/reopen the claim as retryable. The prize was NOT successfully delivered.
            sb.table("giveaway_winners").update(
                {
                    "claim_status": "PENDING",
                    "claimed_at": None,
                    "fulfillment_status": "FAILED",
                    "fulfillment_error": str(e)[:1000],
                }
            ).eq("winner_id", str(winner["winner_id"])).execute()
            return await self._private(
                interaction,
                f"⚠️ Prize fulfillment failed, so your claim is still pending and can be retried. Staff needs to review it. Error: `{str(e)[:200]}`",
            )

        sb.table("giveaway_winners").update(
            {"claim_status": "CLAIMED", "claimed_at": now.isoformat()}
        ).eq("winner_id", str(winner["winner_id"])).execute()
        winner["claim_status"] = "CLAIMED"
        winner["claimed_at"] = now.isoformat()

        await self._private(interaction, f"🎁 Claimed **{self._prize_label(g)}** successfully!")
        try:
            channel = interaction.channel
            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                cname = str(winner.get("character_name") or "").strip()
                who = f"**{cname}** ({interaction.user.mention})" if cname else interaction.user.mention
                await channel.send(f"✅ {who} claimed **{self._prize_label(g)}**!")
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────
    # Background checker
    # ─────────────────────────────────────────────────────────────
    @tasks.loop(seconds=GIVEAWAY_CHECK_SECONDS)
    async def giveaway_check(self):
        await self.bot.wait_until_ready()
        try:
            sb = self.sb()
            due = self._rows(
                sb.table("giveaways")
                .select("*")
                .eq("status", "ACTIVE")
                .lte("ends_at", _utcnow().isoformat())
                .limit(25)
                .execute()
            )
            for g in due:
                guild = self.bot.get_guild(int(g["guild_id"]))
                if not guild:
                    continue
                try:
                    await self._finalize_giveaway(guild, g)
                    await asyncio.sleep(0.5)
                except Exception:
                    traceback.print_exc()

            # Mark expired pending claims.
            expired = self._rows(
                sb.table("giveaway_winners")
                .select("winner_id,giveaway_id,guild_id")
                .eq("claim_status", "PENDING")
                .lt("claim_deadline_at", _utcnow().isoformat())
                .limit(100)
                .execute()
            )
            for w in expired:
                sb.table("giveaway_winners").update({"claim_status": "EXPIRED"}).eq("winner_id", str(w["winner_id"])).execute()
        except Exception:
            traceback.print_exc()

    # ─────────────────────────────────────────────────────────────
    # Staff commands
    # ─────────────────────────────────────────────────────────────
    @app_commands.command(name="start", description="Staff: Start a full prize giveaway")
    @app_commands.choices(prize_type=PRIZE_TYPE_CHOICES, entry_mode=ENTRY_MODE_CHOICES)
    @app_commands.autocomplete(prize_ref=_prize_ref_autocomplete)
    @app_commands.describe(
        prize_type="What kind of prize this giveaway grants",
        prize_name="Display name for the prize",
        duration="How long it runs: 30m, 2h, 1d, 1w",
        amount="Prize amount/quantity per winner",
        winners="How many winners to draw",
        entry_mode="Per OC or per Discord account",
        prize_ref="Item/currency/shop-item ID. Use autocomplete where supported. For currency, blank/primary uses primary currency.",
        role="Required only when prize_type is Discord Role",
        channel="Where to post the giveaway",
        allow_multi_win_per_account="Allow the same Discord account to win multiple times if multiple OCs enter",
        claim_window_hours="How long winners have to claim",
    )
    async def start(
        self,
        interaction: discord.Interaction,
        prize_type: app_commands.Choice[str],
        prize_name: str,
        duration: str,
        amount: app_commands.Range[int, 1, 1_000_000] = 1,
        winners: app_commands.Range[int, 1, 20] = 1,
        entry_mode: str = "character",
        prize_ref: Optional[str] = None,
        role: Optional[discord.Role] = None,
        channel: Optional[discord.TextChannel] = None,
        allow_multi_win_per_account: bool = False,
        claim_window_hours: app_commands.Range[int, 1, 720] = DEFAULT_CLAIM_WINDOW_HOURS,
    ):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        ptype = prize_type.value
        mode = str(entry_mode or "character")

        try:
            delta = _parse_duration(duration)
        except ValueError as e:
            return await self._private(interaction, f"❌ {e}")

        if ptype in OC_PRIZE_TYPES and mode != "character":
            return await self._private(interaction, "Inventory, currency, XP, and shop-item giveaways must use `Per OC / Character` entry mode so the bot knows who receives the prize.")

        ref = (prize_ref or "").strip() or None
        try:
            # Validate prize config up front.
            if ptype == "inventory_item":
                if not ref:
                    return await self._private(interaction, "❌ Pick an inventory item in `prize_ref`.")
                item = self._row(sb.table("items").select("item_id,name").eq("guild_id", guild_id).eq("item_id", ref).limit(1).execute())
                if not item:
                    return await self._private(interaction, "❌ Inventory item not found.")
                prize_name = prize_name or str(item.get("name") or "Inventory Item")

            elif ptype == "currency":
                cur = self._get_currency(sb, guild_id, ref or "primary")
                ref = str(cur["currency_id"])
                if not prize_name.strip():
                    prize_name = str(cur.get("ticker") or cur.get("name") or "Currency")

            elif ptype == "xp":
                ref = None
                if amount <= 0:
                    return await self._private(interaction, "XP amount must be greater than 0.")
                if not prize_name.strip():
                    prize_name = "XP"

            elif ptype == "role":
                if role is None:
                    return await self._private(interaction, "❌ Choose a Discord role for role giveaways.")
                ref = str(role.id)
                if not prize_name.strip():
                    prize_name = role.name

            elif ptype == "shop_item":
                if not ref:
                    return await self._private(interaction, "❌ Pick a shop item in `prize_ref`.")
                shop_item = self._shop_item_row(sb, guild_id, ref)
                if not prize_name.strip():
                    prize_name = str(shop_item.get("name") or "Shop Item")
                # Ensure the shop item can actually fulfill something.
                if not shop_item.get("grants_item_id") and not shop_item.get("role_id"):
                    return await self._private(interaction, "❌ That shop item has no inventory grant or role grant configured, so the giveaway cannot fulfill it automatically.")
        except Exception as e:
            traceback.print_exc()
            return await self._private(interaction, f"❌ Prize validation failed: `{str(e)[:200]}`")

        target_channel = channel or interaction.channel
        if not isinstance(target_channel, discord.TextChannel):
            return await self._private(interaction, "Please choose a text channel.")

        ends_at = _utcnow() + delta
        title = f"🎉 {prize_name} Giveaway"
        try:
            ins = sb.table("giveaways").insert(
                {
                    "guild_id": guild_id,
                    "channel_id": int(target_channel.id),
                    "host_discord_id": int(interaction.user.id),
                    "title": title,
                    "prize_name": prize_name.strip(),
                    "prize_type": ptype,
                    "prize_ref_id": ref,
                    "prize_amount": int(amount),
                    "winner_count": int(winners),
                    "entry_mode": mode,
                    "allow_multiple_wins_per_account": bool(allow_multi_win_per_account),
                    "status": "ACTIVE",
                    "starts_at": _utcnow().isoformat(),
                    "ends_at": ends_at.isoformat(),
                    "claim_window_hours": int(claim_window_hours),
                }
            ).execute()
            g = self._rows(ins)[0]
            self._register_views_for(str(g["giveaway_id"]))

            embed = self._build_giveaway_embed(g)
            msg = await target_channel.send(embed=embed, view=GiveawayEntryView(self, str(g["giveaway_id"])))
            sb.table("giveaways").update({"message_id": int(msg.id)}).eq("giveaway_id", str(g["giveaway_id"])).execute()
            g["message_id"] = int(msg.id)
            await self._update_entry_message(interaction.guild, g)

            self._audit(guild_id=guild_id, giveaway_id=str(g["giveaway_id"]), actor_discord_id=int(interaction.user.id), action="START", details={"prize_type": ptype, "amount": int(amount), "ref": ref})
            return await self._private(interaction, f"✅ Giveaway started in {target_channel.mention}. ID: `{_short_id(str(g['giveaway_id']))}`")
        except Exception:
            traceback.print_exc()
            return await self._private(interaction, "Server error starting giveaway.")

    @app_commands.command(name="stop", description="Staff: End a giveaway now and draw winners")
    @app_commands.describe(giveaway="Giveaway UUID, short ID, or giveaway message ID")
    @app_commands.autocomplete(giveaway=_giveaway_autocomplete)
    async def stop(self, interaction: discord.Interaction, giveaway: str):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        g = self._find_giveaway(giveaway, int(interaction.guild.id))
        if not g:
            return await self._private(interaction, "Giveaway not found.")
        if str(g.get("status")) != "ACTIVE":
            return await self._private(interaction, "That giveaway already ended/cancelled.")
        try:
            winners = await self._finalize_giveaway(interaction.guild, g, actor_discord_id=int(interaction.user.id))
            return await self._private(interaction, f"🛑 Giveaway ended. Winners drawn: `{len(winners)}`")
        except Exception as e:
            traceback.print_exc()
            return await self._private(interaction, f"Server error ending giveaway: `{str(e)[:200]}`")

    @app_commands.command(name="cancel", description="Staff: Cancel a giveaway without drawing winners")
    @app_commands.describe(giveaway="Giveaway UUID, short ID, or message ID")
    @app_commands.autocomplete(giveaway=_giveaway_autocomplete)
    async def cancel(self, interaction: discord.Interaction, giveaway: str):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        g = self._find_giveaway(giveaway, int(interaction.guild.id))
        if not g:
            return await self._private(interaction, "Giveaway not found.")
        self.sb().table("giveaways").update({"status": "CANCELLED", "ended_at": _utcnow().isoformat()}).eq("giveaway_id", str(g["giveaway_id"])).execute()
        self._audit(guild_id=int(interaction.guild.id), giveaway_id=str(g["giveaway_id"]), actor_discord_id=int(interaction.user.id), action="CANCEL")
        return await self._private(interaction, "🗑️ Giveaway cancelled.")

    @app_commands.command(name="entries", description="View active entries for a giveaway")
    @app_commands.describe(giveaway="Giveaway UUID, short ID, or message ID")
    @app_commands.autocomplete(giveaway=_giveaway_autocomplete)
    async def entries(self, interaction: discord.Interaction, giveaway: str):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        g = self._find_giveaway(giveaway, int(interaction.guild.id))
        if not g:
            return await self._private(interaction, "Giveaway not found.")
        rows = self._rows(
            self.sb().table("giveaway_entries").select("*").eq("giveaway_id", str(g["giveaway_id"])).eq("entry_status", "ACTIVE").order("created_at", desc=False).execute()
        )
        embed = discord.Embed(title=f"🎟️ Entries — {self._prize_label(g)}", color=discord.Color.blurple())
        if not rows:
            embed.description = "No active entries."
        else:
            lines = []
            for r in rows[:40]:
                cname = str(r.get("character_name") or "").strip()
                lines.append(f"• {cname + ' — ' if cname else ''}<@{int(r['user_discord_id'])}>")
            embed.description = "\n".join(lines)
            if len(rows) > 40:
                embed.add_field(name="More", value=f"+{len(rows)-40} more entries", inline=False)
            embed.add_field(name="Total", value=f"`{len(rows)}`", inline=True)
        return await self._public(interaction, embed=embed, ephemeral=True)

    @app_commands.command(name="add", description="Staff: Manually add a member's active OC/account to a giveaway")
    @app_commands.describe(giveaway="Giveaway UUID, short ID, or message ID", member="Member to add")
    @app_commands.autocomplete(giveaway=_giveaway_autocomplete)
    async def add(self, interaction: discord.Interaction, giveaway: str, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        g = self._find_giveaway(giveaway, int(interaction.guild.id))
        if not g or str(g.get("status")) != "ACTIVE":
            return await self._private(interaction, "Active giveaway not found.")
        sb = self.sb()
        char_id = None
        char_name = None
        if str(g.get("entry_mode")) == "character":
            active = get_active_character(sb, int(member.id))
            if not active or not active.get("character_id"):
                return await self._private(interaction, f"{member.mention} has no active OC set.")
            char_id = str(active["character_id"])
            char_name = str(active.get("name") or "Unknown OC")
        try:
            sb.table("giveaway_entries").upsert(
                {
                    "giveaway_id": str(g["giveaway_id"]),
                    "guild_id": int(interaction.guild.id),
                    "user_discord_id": int(member.id),
                    "character_id": char_id,
                    "character_name": char_name,
                    "entry_source": "manual",
                    "entry_status": "ACTIVE",
                    "updated_at": _utcnow().isoformat(),
                },
                on_conflict="giveaway_id,user_discord_id,character_id",
            ).execute()
            self._audit(guild_id=int(interaction.guild.id), giveaway_id=str(g["giveaway_id"]), actor_discord_id=int(interaction.user.id), target_discord_id=int(member.id), action="ADD_ENTRY")
            await self._update_entry_message(interaction.guild, g)
            label = f" as **{char_name}**" if char_name else ""
            return await self._private(interaction, f"✅ Added {member.mention}{label}.")
        except Exception as e:
            traceback.print_exc()
            return await self._private(interaction, f"Server error adding entry: `{str(e)[:200]}`")

    @app_commands.command(name="remove", description="Staff: Exclude a member's active OC/account from a giveaway")
    @app_commands.describe(giveaway="Giveaway UUID, short ID, or message ID", member="Member to remove", reason="Optional reason")
    @app_commands.autocomplete(giveaway=_giveaway_autocomplete)
    async def remove(self, interaction: discord.Interaction, giveaway: str, member: discord.Member, reason: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        g = self._find_giveaway(giveaway, int(interaction.guild.id))
        if not g:
            return await self._private(interaction, "Giveaway not found.")
        sb = self.sb()
        q = sb.table("giveaway_entries").update(
            {
                "entry_status": "EXCLUDED",
                "excluded_by": int(interaction.user.id),
                "excluded_reason": (reason or "Removed by staff")[:500],
            }
        ).eq("giveaway_id", str(g["giveaway_id"])).eq("user_discord_id", int(member.id))
        if str(g.get("entry_mode")) == "character":
            active = get_active_character(sb, int(member.id))
            if active and active.get("character_id"):
                q = q.eq("character_id", str(active["character_id"]))
        q.execute()
        self._audit(guild_id=int(interaction.guild.id), giveaway_id=str(g["giveaway_id"]), actor_discord_id=int(interaction.user.id), target_discord_id=int(member.id), action="REMOVE_ENTRY", details={"reason": reason})
        await self._update_entry_message(interaction.guild, g)
        return await self._private(interaction, f"✅ Removed/excluded {member.mention}.")

    @app_commands.command(name="reroll", description="Staff: Reroll a giveaway and reverse claimed prizes first when needed")
    @app_commands.describe(
        giveaway="Giveaway UUID, short ID, or message ID",
        winners="Optional override for number of new winners",
        exclude_previous="Exclude every previous winner from the new draw",
        reverse_claimed="Reverse already-claimed prizes before drawing new winners",
    )
    @app_commands.autocomplete(giveaway=_giveaway_autocomplete)
    async def reroll(
        self,
        interaction: discord.Interaction,
        giveaway: str,
        winners: Optional[app_commands.Range[int, 1, 20]] = None,
        exclude_previous: bool = True,
        reverse_claimed: bool = True,
    ):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        sb = self.sb()
        g = self._find_giveaway(giveaway, int(interaction.guild.id))
        if not g:
            return await self._private(interaction, "Giveaway not found.")
        if str(g.get("status")) == "ACTIVE":
            return await self._private(interaction, "This giveaway is still active. Use `/giveaway stop` first, or wait for it to end.")

        if winners is not None:
            g["winner_count"] = int(winners)
            sb.table("giveaways").update({"winner_count": int(winners)}).eq("giveaway_id", str(g["giveaway_id"])).execute()

        # Pull *all* previous winner rows for this giveaway.
        # Earlier builds could leave a prize as CLAIMED/FULFILLED, REROLLED/FULFILLED,
        # or REVERSAL_FAILED after a failed reroll. If a prize was ever fulfilled and
        # has not been reversed, we must try to reverse it before drawing again.
        previous = self._rows(
            sb.table("giveaway_winners")
            .select("*")
            .eq("giveaway_id", str(g["giveaway_id"]))
            .order("created_at", desc=True)
            .execute()
        )

        reversed_count = 0
        failed: list[str] = []
        for w in previous:
            fulfillment_status = str(w.get("fulfillment_status") or "")
            claim_status = str(w.get("claim_status") or "")

            # Already handled rows should stay handled.
            if fulfillment_status == "REVERSED" or claim_status in ("FORFEITED",):
                continue

            # If the prize was fulfilled, or a previous reversal failed after fulfillment,
            # retry the reversal now. This is what fixes XP rerolls after the earlier
            # xp_wallets/oc_xp_wallets mismatch.
            if fulfillment_status in ("FULFILLED", "REVERSAL_FAILED"):
                if not reverse_claimed:
                    continue
                try:
                    await self._reverse_winner(interaction.guild, g, w, actor_discord_id=int(interaction.user.id))
                    reversed_count += 1
                except Exception as e:
                    failed.append(f"<@{int(w['user_discord_id'])}>: {str(e)[:160]}")
            else:
                # Pending/not-fulfilled winners can simply be marked as rerolled.
                sb.table("giveaway_winners").update({"claim_status": "REROLLED"}).eq("winner_id", str(w["winner_id"])).execute()

        if failed:
            return await self._private(
                interaction,
                "⚠️ Reroll stopped because at least one claimed prize could not be reversed.\n"
                + "\n".join(failed[:5])
                + "\nFix manually or run again with `reverse_claimed:false` if staff wants to allow that.",
            )

        existing_draws = self._rows(sb.table("giveaway_winners").select("draw_number").eq("giveaway_id", str(g["giveaway_id"])).execute())
        next_draw = (max([int(x.get("draw_number") or 1) for x in existing_draws] or [1]) + 1)
        try:
            new_winners = await self._finalize_giveaway(interaction.guild, g, actor_discord_id=int(interaction.user.id), reroll_draw_number=next_draw, exclude_previous=exclude_previous)
            return await self._private(interaction, f"🔁 Reroll complete. New winners: `{len(new_winners)}`. Reversed claimed prizes: `{reversed_count}`.")
        except Exception as e:
            traceback.print_exc()
            return await self._private(interaction, f"Server error during reroll: `{str(e)[:200]}`")

    @app_commands.command(name="status", description="Show giveaway winners, claims, and fulfillment status")
    @app_commands.describe(giveaway="Giveaway UUID, short ID, or message ID")
    @app_commands.autocomplete(giveaway=_giveaway_autocomplete)
    async def status(self, interaction: discord.Interaction, giveaway: str):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        g = self._find_giveaway(giveaway, int(interaction.guild.id))
        if not g:
            return await self._private(interaction, "Giveaway not found.")
        wins = self._rows(
            self.sb().table("giveaway_winners").select("*").eq("giveaway_id", str(g["giveaway_id"])).order("draw_number", desc=False).order("created_at", desc=False).execute()
        )
        entries = self._rows(self.sb().table("giveaway_entries").select("entry_id").eq("giveaway_id", str(g["giveaway_id"])).eq("entry_status", "ACTIVE").execute())
        embed = discord.Embed(title=f"🎉 Giveaway Status — {_short_id(str(g['giveaway_id']))}", color=discord.Color.dark_teal())
        embed.add_field(name="Prize", value=f"**{self._prize_label(g)}**", inline=False)
        embed.add_field(name="Status", value=f"`{g.get('status')}`", inline=True)
        embed.add_field(name="Active Entries", value=f"`{len(entries)}`", inline=True)
        if not wins:
            embed.add_field(name="Winners", value="No winners drawn yet.", inline=False)
        else:
            lines = []
            for w in wins[:20]:
                cname = str(w.get("character_name") or "").strip()
                who = f"{cname} / <@{int(w['user_discord_id'])}>" if cname else f"<@{int(w['user_discord_id'])}>"
                lines.append(
                    f"Draw `{w.get('draw_number')}` • {who}\n"
                    f"Claim: `{w.get('claim_status')}` • Fulfillment: `{w.get('fulfillment_status')}`"
                )
            embed.add_field(name="Winners", value="\n\n".join(lines)[:3900], inline=False)
        return await self._public(interaction, embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(GiveawayCog(bot))
