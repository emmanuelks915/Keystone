# cogs/commerce.py — Keystone Commerce (shop + player shops + company wallets)
# PUBLIC BY DEFAULT for accountability (no ephemeral responses).

import os
import io
import csv
import re
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from services.currency_service import get_primary_currency, ensure_wallet
from services.oc_service import get_active_character
from services.economy_service import apply_company_transaction

# Inventory service: be resilient to naming differences
# Preferred: add_item(sb, guild_id, character_id, item_id, qty, actor_discord_id, context, note)
# Fallback: apply_delta(sb, guild_id, character_id, item_id, delta, actor_discord_id, context, note)
try:
    from services.inventory_service import add_item as add_item_to_inventory  # type: ignore
except Exception:
    add_item_to_inventory = None  # type: ignore

try:
    from services.inventory_service import apply_delta as inv_apply_delta  # type: ignore
except Exception:
    inv_apply_delta = None  # type: ignore


# ── Env / constants ─────────────────────────────────────────────────────────────
DEFAULT_SHOP_CHANNEL_ID = int(os.getenv("SHOP_CHANNEL_ID", "1477042447556018377"))
DEFAULT_FORUM_CHANNEL_ID = int(os.getenv("SHOP_FORUM_CHANNEL_ID", "1477042787055698052"))

TREASURY_COMPANY_ID = (os.getenv("TREASURY_COMPANY_ID") or "").strip() or None

RECEIPTS_CHANNEL_ID = int(os.getenv("SHOP_RECEIPTS_CHANNEL_ID", "1477139641898369267"))

# Shared staff approval/review queue. Defaults to your RP XP / general staff approvals channel.
# You can override it with STAFF_APPROVALS_CHANNEL_ID or SHOP_APPROVALS_CHANNEL_ID.
APPROVALS_CHANNEL_ID = int(
    os.getenv(
        "STAFF_APPROVALS_CHANNEL_ID",
        os.getenv("SHOP_APPROVALS_CHANNEL_ID", "1502870738313543770"),
    )
)

# Optional separate owner-visible ticket/review channel. If unset, the bot uses APPROVALS_CHANNEL_ID.
# For true player-visible review tickets, set this to a channel players can see.
SHOP_REVIEW_TICKET_CHANNEL_ID = int(os.getenv("SHOP_REVIEW_TICKET_CHANNEL_ID", str(APPROVALS_CHANNEL_ID)))

LEDGER_CHANNEL_ID = int(os.getenv("SHOP_LEDGER_CHANNEL_ID", "1473718167929880791"))

# Optional: role granted by buying a Shop Owner License / Merchant Writ item.
# Set this in Railway if you want players to self-create shops after buying the license.
SHOP_OWNER_ROLE_ID = int(os.getenv("SHOP_OWNER_ROLE_ID", "0") or "0")

# Category where each approved player shop gets its own forum channel.
# Default: The Market District. Override in .env if you move/rename the category.
PLAYER_SHOPS_CATEGORY_ID = int(os.getenv("PLAYER_SHOPS_CATEGORY_ID", "1503434773857701928") or "0")

BUTTON_BUY_RECEIPT_PUBLIC = True

ITEM_TYPE_CHOICES = ("item", "consumable", "material")
ITEM_TYPE_LABEL = {
    "item": "📦 Item",
    "consumable": "🧪 Consumable",
    "material": "🧱 Material",
}


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


def _is_dev(user_id: int) -> bool:
    return user_id in _parse_dev_ids()


def _has_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False
    if isinstance(interaction.user, discord.Member):
        return interaction.user.guild_permissions.administrator
    return False


class ShopItemView(discord.ui.View):
    def __init__(self, cog: "ShopCog", *, item_id: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.item_id = str(item_id)

        self.add_item(discord.ui.Button(label="Buy x1", style=discord.ButtonStyle.success, custom_id=f"shop:buy:{self.item_id}:1"))
        self.add_item(discord.ui.Button(label="Buy x5", style=discord.ButtonStyle.success, custom_id=f"shop:buy:{self.item_id}:5"))
        self.add_item(discord.ui.Button(label="Buy x10", style=discord.ButtonStyle.success, custom_id=f"shop:buy:{self.item_id}:10"))
        self.add_item(discord.ui.Button(label="Browse", style=discord.ButtonStyle.secondary, custom_id="shop:browse"))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True


class ApprovalView(discord.ui.View):
    def __init__(self, cog: "ShopCog", *, order_id: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.order_id = str(order_id)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="shop:approve_btn")
    async def approve_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.cog._approve_by_button(interaction, order_id=self.order_id)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="shop:deny_btn")
    async def deny_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.cog._deny_by_button(interaction, order_id=self.order_id)


class StaffChangeSummaryModal(discord.ui.Modal, title="Send Staff Edits to Owner"):
    def __init__(self, cog: "ShopCog", *, item_id: str):
        super().__init__()
        self.cog = cog
        self.item_id = str(item_id)

        self.summary = discord.ui.TextInput(
            label="What changed / what should owner review?",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1000,
            placeholder="Example: Staff adjusted CC from 2 to 3 and clarified the special effect limit.",
        )
        self.add_item(self.summary)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.cog._send_item_changes_to_owner(
            interaction,
            item_id=self.item_id,
            summary=str(self.summary.value or "").strip(),
        )


class RejectListingModal(discord.ui.Modal, title="Reject Player Listing"):
    def __init__(self, cog: "ShopCog", *, item_id: str):
        super().__init__()
        self.cog = cog
        self.item_id = str(item_id)

        self.reason = discord.ui.TextInput(
            label="Reason / requested rewrite",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1000,
            placeholder="Explain what needs to change before this can be reviewed again.",
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.cog._reject_player_listing(
            interaction,
            item_id=self.item_id,
            reason=str(self.reason.value or "").strip(),
        )


class OwnerRequestChangesModal(discord.ui.Modal, title="Request More Changes"):
    def __init__(self, cog: "ShopCog", *, item_id: str):
        super().__init__()
        self.cog = cog
        self.item_id = str(item_id)

        self.notes = discord.ui.TextInput(
            label="What should staff change?",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1000,
            placeholder="Example: I am okay with the CC change, but I want the usage text to keep the original flavor.",
        )
        self.add_item(self.notes)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.cog._owner_requests_item_changes(
            interaction,
            item_id=self.item_id,
            notes=str(self.notes.value or "").strip(),
        )


class ListingReviewView(discord.ui.View):
    def __init__(self, cog: "ShopCog", *, item_id: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.item_id = str(item_id)

    @discord.ui.button(label="Approve + Publish", style=discord.ButtonStyle.success, custom_id="shop:listing_review:approve")
    async def approve_listing_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.cog._approve_player_listing_by_button(interaction, item_id=self.item_id)

    @discord.ui.button(label="Send Edits to Owner", style=discord.ButtonStyle.primary, custom_id="shop:listing_review:owner_review")
    async def send_edits_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog._staff_ok(interaction):
            return await self.cog._private(interaction, "❌ Staff only.")
        await interaction.response.send_modal(StaffChangeSummaryModal(self.cog, item_id=self.item_id))

    @discord.ui.button(label="Reject / Needs Rework", style=discord.ButtonStyle.danger, custom_id="shop:listing_review:reject")
    async def reject_listing_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog._staff_ok(interaction):
            return await self.cog._private(interaction, "❌ Staff only.")
        await interaction.response.send_modal(RejectListingModal(self.cog, item_id=self.item_id))


class OwnerChangeReviewView(discord.ui.View):
    def __init__(self, cog: "ShopCog", *, item_id: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.item_id = str(item_id)

    @discord.ui.button(label="Owner Approves Changes", style=discord.ButtonStyle.success, custom_id="shop:owner_review:approve_changes")
    async def owner_approve_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.cog._owner_accepts_staff_changes(interaction, item_id=self.item_id)

    @discord.ui.button(label="Owner Requests Changes", style=discord.ButtonStyle.danger, custom_id="shop:owner_review:request_changes")
    async def owner_request_changes_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(OwnerRequestChangesModal(self.cog, item_id=self.item_id))


class ShopCog(commands.GroupCog, group_name="shop", group_description="Shop tools"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()
        self.bot.add_listener(self._on_interaction, "on_interaction")

    # ── Supabase handle ────────────────────────────────────────────────────────
    def sb(self):
        sb = getattr(self.bot, "supabase", None)
        if sb is None:
            raise RuntimeError("Supabase is not configured on the bot.")
        return sb

    # ── Permission ─────────────────────────────────────────────────────────────
    def _staff_ok(self, interaction: discord.Interaction) -> bool:
        if _has_admin(interaction) or _is_dev(interaction.user.id):
            return True
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        staff_roles: set[int] = getattr(self.bot, "staff_role_ids", set())
        return any(r.id in staff_roles for r in interaction.user.roles)

    # ── Reply helpers ──────────────────────────────────────────────────────────
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
        kwargs = {"content": content, "embed": embed, "ephemeral": ephemeral}
        if view is not None:
            kwargs["view"] = view
        if interaction.response.is_done():
            return await interaction.followup.send(**kwargs)
        return await interaction.response.send_message(**kwargs)

    # ── UX helpers ─────────────────────────────────────────────────────────────
    def _short_order_code(self, order_id: str) -> str:
        oid = (order_id or "").replace("-", "")
        return f"K8-{oid[:8].upper()}" if oid else "K8-UNKNOWN"

    def _parse_order_lookup(self, raw: str) -> str:
        oid = (raw or "").strip()
        if oid.upper().startswith("K8-"):
            oid = oid.split("-", 1)[1].strip()
        return oid

    def _normalize_item_type(self, raw: str | None) -> str:
        s = (raw or "").strip().lower()
        return s if s in ITEM_TYPE_CHOICES else "item"

    async def _get_text_channel(self, guild: discord.Guild, channel_id: int) -> Optional[discord.TextChannel]:
        ch = guild.get_channel(int(channel_id))
        if ch is None:
            try:
                ch = await guild.fetch_channel(int(channel_id))
            except Exception:
                return None
        return ch if isinstance(ch, discord.TextChannel) else None

    async def _post_ledger(self, interaction: discord.Interaction, embed: discord.Embed | None = None):
        if not interaction.guild or embed is None:
            return
        ch = await self._get_text_channel(interaction.guild, int(LEDGER_CHANNEL_ID))
        if ch is None:
            return
        try:
            await ch.send(embed=embed)
        except Exception:
            pass

    async def _post_receipt_to_channel(self, interaction: discord.Interaction, *, embed: discord.Embed, ping_user: bool = True) -> None:
        if not interaction.guild:
            return
        ch = await self._get_text_channel(interaction.guild, int(RECEIPTS_CHANNEL_ID))
        if ch is None:
            return
        content = interaction.user.mention if ping_user else None
        try:
            await ch.send(content=content, embed=embed)
        except Exception:
            pass

    async def _post_approval_card(
        self,
        interaction: discord.Interaction,
        *,
        order_id: str,
        buyer_mention: str,
        item_name: str,
        quantity: int,
        total: int,
        emoji: str,
        ticker: str,
    ) -> None:
        if not interaction.guild:
            return
        ch = await self._get_text_channel(interaction.guild, int(APPROVALS_CHANNEL_ID))
        if ch is None:
            return

        code = self._short_order_code(order_id)
        embed = discord.Embed(title="🛂 Shop Order Needs Approval", color=discord.Color.orange())
        embed.add_field(name="Order", value=f"`{code}` (UUID starts: `{order_id[:8]}`)", inline=False)
        embed.add_field(name="Buyer", value=buyer_mention, inline=True)
        embed.add_field(name="Item", value=f"**{item_name}**", inline=True)
        embed.add_field(name="Qty / Total", value=f"`{quantity}` • {emoji}`{total}` {ticker}", inline=False)
        embed.set_footer(text="Click Approve/Deny — no typing required.")
        embed.timestamp = discord.utils.utcnow()

        try:
            await ch.send(embed=embed, view=ApprovalView(self, order_id=order_id))
        except Exception:
            pass

    async def _approve_by_button(self, interaction: discord.Interaction, *, order_id: str):
        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        return await self.approve(interaction, order_id=order_id)

    async def _deny_by_button(self, interaction: discord.Interaction, *, order_id: str):
        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        return await self.deny(interaction, order_id=order_id, reason="Denied by staff (button)")

    # ── DB: settings ───────────────────────────────────────────────────────────
    def _get_shop_settings(self, sb, guild_id: int) -> dict:
        res = sb.table("shops").select("*").eq("guild_id", int(guild_id)).limit(1).execute()
        rows = getattr(res, "data", None) or []
        if rows:
            return rows[0]
        return {
            "guild_id": int(guild_id),
            "enabled": True,
            "shop_channel_id": int(DEFAULT_SHOP_CHANNEL_ID),
            "forum_channel_id": int(DEFAULT_FORUM_CHANNEL_ID),
            "treasury_cut_bps": 0,
        }

    # ── DB: items (shop items) ────────────────────────────────────────────────
    def _get_item(self, sb, guild_id: int, item_id: str) -> Optional[dict]:
        res = (
            sb.table("shop_items")
            .select("*")
            .eq("guild_id", int(guild_id))
            .eq("item_id", str(item_id))
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        return rows[0] if rows else None

    def _list_items(self, sb, guild_id: int, *, active_only: bool = True, limit: int = 50) -> list[dict]:
        q = sb.table("shop_items").select("*").eq("guild_id", int(guild_id))
        if active_only:
            q = q.eq("is_active", True)
        res = q.order("created_at", desc=True).limit(int(limit)).execute()
        return getattr(res, "data", None) or []

    # ── DB: inventory items (canonical items table) ───────────────────────────
    def _get_inv_item(self, sb, guild_id: int, inv_item_id: str) -> Optional[dict]:
        res = (
            sb.table("items")
            .select("item_id,name,item_class,wu,sheet_url,is_active")
            .eq("guild_id", int(guild_id))
            .eq("item_id", str(inv_item_id))
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        return rows[0] if rows else None

    async def _inv_item_autocomplete(self, interaction: discord.Interaction, current: str):
        if not interaction.guild:
            return []
        sb = self.sb()
        gid = int(interaction.guild.id)
        q = (current or "").strip().lower()

        res = (
            sb.table("items")
            .select("item_id,name,item_class,wu,is_active")
            .eq("guild_id", gid)
            .eq("is_active", True)
            .order("name", desc=False)
            .limit(50)
            .execute()
        )
        rows = getattr(res, "data", None) or []

        out: list[app_commands.Choice[str]] = []
        for it in rows:
            name = str(it.get("name") or "")
            if q and q not in name.lower():
                continue
            item_class = str(it.get("item_class") or "—")
            wu = it.get("wu")
            wu_txt = f"WU {int(wu)}" if wu is not None else "WU —"
            out.append(app_commands.Choice(name=f"{name[:60]} • {item_class[:20]} • {wu_txt}", value=str(it["item_id"])))
        return out[:25]

    # ── Company wallet ensure (safe) ───────────────────────────────────────────
    def _ensure_company_wallet(self, sb, company_id: str, currency_id: str):
        existing = (
            sb.table("company_wallets")
            .select("company_id")
            .eq("company_id", company_id)
            .eq("currency_id", currency_id)
            .limit(1)
            .execute()
        )
        rows = getattr(existing, "data", None) or []
        if rows:
            return
        sb.table("company_wallets").insert({"company_id": company_id, "currency_id": currency_id, "balance": 0}).execute()

    def _get_company_name(self, sb, company_id: str) -> str:
        res = sb.table("companies").select("name").eq("company_id", company_id).limit(1).execute()
        rows = getattr(res, "data", None) or []
        return str(rows[0].get("name") or "Company") if rows else "Company"

    def _resolve_active_treasury_company_id(self, sb, guild_id: int) -> Optional[str]:
        if TREASURY_COMPANY_ID:
            return TREASURY_COMPANY_ID

        for try_active in (True, False):
            try:
                q = sb.table("companies").select("company_id").eq("guild_id", int(guild_id)).eq("is_treasury", True)
                if try_active:
                    q = q.eq("is_active", True)
                res = q.limit(1).execute()
                rows = getattr(res, "data", None) or []
                if rows:
                    return str(rows[0]["company_id"])
            except Exception:
                pass

        try:
            res = (
                sb.table("companies")
                .select("company_id,name")
                .eq("guild_id", int(guild_id))
                .ilike("name", "%treasury%")
                .limit(1)
                .execute()
            )
            rows = getattr(res, "data", None) or []
            if rows:
                return str(rows[0]["company_id"])
        except Exception:
            pass

        return None

    # ── Utils ──────────────────────────────────────────────────────────────────
    def _calc_cut(self, total: int, bps: int) -> int:
        bps = int(bps or 0)
        if total <= 0 or bps <= 0:
            return 0
        return int((total * bps) // 10000)

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _format_weight(self, it: dict) -> str | None:
        w = it.get("weight")
        u = (it.get("weight_unit") or "").strip()
        if w is None:
            return None
        try:
            w_i = int(w)
        except Exception:
            return None
        if w_i < 0:
            return None
        return f"{w_i} {u}" if u else f"{w_i}"

    def _build_item_embed(self, *, it: dict, cur: dict, guild_id: int) -> discord.Embed:
        emoji = cur.get("emoji") or "🪙"
        ticker = cur.get("ticker") or cur.get("name") or "CUR"

        name = str(it.get("name") or "Item")
        desc = str(it.get("description") or "").strip() or "—"
        price = int(it.get("price") or 0)
        stock = it.get("stock")
        stock_txt = "∞" if stock is None else str(int(stock))
        approval = "✅ instant" if not it.get("requires_approval") else "🕒 approval"
        weight_txt = self._format_weight(it)

        it_type = self._normalize_item_type(it.get("item_type"))

        embed = discord.Embed(title=f"🛒 {name}", color=discord.Color.blurple())
        embed.description = desc
        embed.add_field(name="Type", value=f"`{ITEM_TYPE_LABEL.get(it_type, '📦 Item')}`", inline=True)
        embed.add_field(name="Price", value=f"{emoji} `{price}` {ticker}", inline=True)
        embed.add_field(name="Stock", value=f"`{stock_txt}`", inline=True)
        embed.add_field(name="Delivery", value=approval, inline=True)

        vendor_company_id = it.get("vendor_company_id")
        if vendor_company_id:
            try:
                embed.add_field(name="Seller", value=f"🏦 **{self._get_company_name(self.sb(), str(vendor_company_id))}**", inline=False)
            except Exception:
                embed.add_field(name="Seller", value="🏦 Player shop", inline=False)

        if weight_txt:
            embed.add_field(name="Weight", value=f"`{weight_txt}`", inline=True)

        # Optional Railbound item-template fields for player shop listings.
        for label, key in (
            ("Recipe", "recipe_link"),
            ("Unique", "unique_owner"),
            ("Item Class", "item_class"),
            ("CC", "cc"),
            ("Stat Limits", "stat_limits"),
            ("Special Effects", "special_effects"),
            ("Usage Information", "usage_information"),
        ):
            val = it.get(key)
            if val is not None and str(val).strip():
                txt = str(val).strip()
                if len(txt) > 900:
                    txt = txt[:899] + "…"
                embed.add_field(name=label, value=txt, inline=False)

        # Grants display (show inventory item name)
        grants_item_id = it.get("grants_item_id")
        grants_qty = int(it.get("grants_qty") or 0)
        if grants_item_id and grants_qty > 0:
            try:
                inv = self._get_inv_item(self.sb(), guild_id, str(grants_item_id))
                if inv:
                    embed.add_field(name="Grants", value=f"`{grants_qty}` × **{inv.get('name','Item')}**", inline=False)
                else:
                    embed.add_field(name="Grants", value=f"`{grants_qty}` × (missing item)", inline=False)
            except Exception:
                embed.add_field(name="Grants", value=f"`{grants_qty}` × (lookup failed)", inline=False)

        img = (it.get("image_url") or "").strip()
        if img:
            embed.set_image(url=img)

        embed.set_footer(text=f"Shop Item ID: {str(it['item_id'])}")
        embed.timestamp = discord.utils.utcnow()
        return embed

    async def _item_autocomplete(self, interaction: discord.Interaction, current: str):
        if not interaction.guild:
            return []
        sb = self.sb()
        guild_id = int(interaction.guild.id)
        q = (current or "").lower().strip()

        items = self._list_items(sb, guild_id, active_only=False, limit=50)
        out: list[app_commands.Choice[str]] = []
        for it in items:
            name = str(it.get("name") or "")
            if q and q not in name.lower():
                continue
            tag = "✅" if it.get("is_active", True) else "🗑️"
            out.append(app_commands.Choice(name=f"{tag} {name[:90]}", value=str(it["item_id"])))
        return out[:25]

    # ── Inventory grant helper ─────────────────────────────────────────────────
    def _grant_inventory(self, sb, *, guild_id: int, character_id: str, item_id: str, qty: int, actor_discord_id: int, context: str, note: str | None):
        if qty == 0:
            return
        if add_item_to_inventory is not None:
            return add_item_to_inventory(
                sb,
                guild_id=guild_id,
                character_id=character_id,
                item_id=item_id,
                qty=qty,
                actor_discord_id=actor_discord_id,
                context=context,
                note=note,
            )
        if inv_apply_delta is not None:
            return inv_apply_delta(
                sb,
                guild_id=guild_id,
                character_id=character_id,
                item_id=item_id,
                delta=qty,
                actor_discord_id=actor_discord_id,
                context=context,
                note=note,
            )
        raise RuntimeError("Inventory service missing: no add_item or apply_delta available")

    # ── Persistent view registration ───────────────────────────────────────────
    async def cog_load(self):
        try:
            sb = self.sb()
            res = sb.table("shop_items").select("guild_id,item_id,shop_message_id,forum_thread_id,is_active").limit(500).execute()
            rows = getattr(res, "data", None) or []
            for r in rows:
                item_id = str(r["item_id"])
                if r.get("shop_message_id") or r.get("forum_thread_id"):
                    self.bot.add_view(ShopItemView(self, item_id=item_id))
        except Exception:
            traceback.print_exc()

    async def cog_unload(self):
        try:
            self.bot.remove_listener(self._on_interaction, "on_interaction")
        except Exception:
            pass

    # ── Interaction handler ────────────────────────────────────────────────────
    async def _on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        data = interaction.data or {}
        custom_id = str(data.get("custom_id") or "")

        if custom_id == "shop:browse":
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
                await self._browse_ephemeral(interaction)
            except Exception:
                traceback.print_exc()
            return

        if not custom_id.startswith("shop:buy:"):
            return

        try:
            _pfx, _buy, item_id, qty_s = custom_id.split(":", 3)
            qty = int(qty_s)
        except Exception:
            return

        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=not BUTTON_BUY_RECEIPT_PUBLIC)
            await self._buy_internal(interaction, item_id=item_id, quantity=qty, receipt_public=bool(BUTTON_BUY_RECEIPT_PUBLIC))
        except Exception:
            traceback.print_exc()
            try:
                await self._private(interaction, "Server error processing that purchase.")
            except Exception:
                pass

    async def _browse_ephemeral(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        settings = self._get_shop_settings(sb, guild_id)
        if not settings.get("enabled", True):
            return await self._public(interaction, content="🛑 The shop is currently disabled.", ephemeral=True)

        items = self._list_items(sb, guild_id, active_only=True, limit=25)
        if not items:
            return await self._public(interaction, content="No items are listed yet.", ephemeral=True)

        cur = get_primary_currency(sb, guild_id)
        emoji = cur.get("emoji") or "🪙"
        ticker = cur.get("ticker") or cur.get("name") or "CUR"

        embed = discord.Embed(title="🛒 Shop", color=discord.Color.blurple())
        embed.description = "Use `/shop buy` or click **Buy** buttons on a storefront post."

        for it in items[:10]:
            name = str(it.get("name") or "Item")
            price = int(it.get("price") or 0)
            desc = str(it.get("description") or "").strip()
            if len(desc) > 140:
                desc = desc[:140] + "…"

            stock = it.get("stock")
            stock_txt = "∞" if stock is None else str(int(stock))
            approval = "✅ instant" if not it.get("requires_approval") else "🕒 approval"
            weight_txt = self._format_weight(it)
            weight_line = f"\nWeight: `{weight_txt}`" if weight_txt else ""
            it_type = ITEM_TYPE_LABEL.get(self._normalize_item_type(it.get("item_type")), "📦 Item")

            embed.add_field(
                name=f"{name} • {emoji}{price} {ticker}",
                value=f"{desc}\nType: `{it_type}` • Stock: `{stock_txt}` • {approval}{weight_line}\nID: `{str(it['item_id'])[:8]}`",
                inline=False,
            )

        embed.timestamp = discord.utils.utcnow()
        return await self._public(interaction, embed=embed, ephemeral=True)


    # ── Player-run shop bridge helpers ─────────────────────────────────────────
    def _role_rank(self, role: str | None) -> int:
        r = (role or "").upper()
        return {"OWNER": 3, "MANAGER": 2, "TELLER": 1}.get(r, 0)

    def _require_rank(self, have: int, need: int) -> bool:
        return have >= need

    def _get_member_rank(self, sb, company_id: str, discord_id: int) -> int:
        res = (
            sb.table("company_members")
            .select("role")
            .eq("company_id", str(company_id))
            .eq("discord_id", int(discord_id))
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        if not rows:
            return 0
        return self._role_rank(rows[0].get("role"))

    def _has_shop_owner_license(self, interaction: discord.Interaction) -> bool:
        """
        License check for player-created shops.
        Staff/dev can always bypass this for setup/testing.
        Players need the SHOP_OWNER_ROLE_ID role if that env var is configured.
        """
        if self._staff_ok(interaction):
            return True
        if SHOP_OWNER_ROLE_ID <= 0:
            return False
        if not isinstance(interaction.user, discord.Member):
            return False
        return any(r.id == SHOP_OWNER_ROLE_ID for r in interaction.user.roles)

    async def _owned_company_autocomplete(self, interaction: discord.Interaction, current: str):
        if not interaction.guild:
            return []
        sb = self.sb()
        guild_id = int(interaction.guild.id)
        uid = int(interaction.user.id)
        q = (current or "").lower().strip()

        # Staff/dev can see all companies; players only see companies they belong to.
        if self._staff_ok(interaction):
            cres = sb.table("companies").select("company_id,name").eq("guild_id", guild_id).limit(50).execute()
            companies = getattr(cres, "data", None) or []
        else:
            mres = (
                sb.table("company_members")
                .select("company_id,role")
                .eq("discord_id", uid)
                .execute()
            )
            memberships = getattr(mres, "data", None) or []
            company_ids = [str(m["company_id"]) for m in memberships if self._role_rank(m.get("role")) >= 2]
            if not company_ids:
                return []
            cres = sb.table("companies").select("company_id,name").eq("guild_id", guild_id).in_("company_id", company_ids).limit(50).execute()
            companies = getattr(cres, "data", None) or []

        out: list[app_commands.Choice[str]] = []
        for c in companies:
            name = str(c.get("name") or "Company")
            if q and q not in name.lower():
                continue
            out.append(app_commands.Choice(name=name[:100], value=str(c["company_id"])))
        return out[:25]


    def _truncate(self, value: Any, limit: int = 900) -> str:
        s = str(value or "").strip()
        if not s:
            return "—"
        return s if len(s) <= limit else s[: limit - 1] + "…"

    def _optional_item_template_patch(
        self,
        *,
        recipe_link: Optional[str] = None,
        unique_owner: Optional[str] = None,
        item_class: Optional[str] = None,
        cc: Optional[int] = None,
        stat_limits: Optional[str] = None,
        special_effects: Optional[str] = None,
        usage_information: Optional[str] = None,
    ) -> dict[str, Any]:
        patch: dict[str, Any] = {}
        if recipe_link is not None:
            patch["recipe_link"] = recipe_link.strip() or None
        if unique_owner is not None:
            patch["unique_owner"] = unique_owner.strip() or None
        if item_class is not None:
            patch["item_class"] = item_class.strip() or None
        if cc is not None:
            patch["cc"] = int(cc)
        if stat_limits is not None:
            patch["stat_limits"] = stat_limits.strip() or None
        if special_effects is not None:
            patch["special_effects"] = special_effects.strip() or None
        if usage_information is not None:
            patch["usage_information"] = usage_information.strip() or None
        return patch

    async def _get_review_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        # Prefer the optional owner-visible ticket channel, otherwise use the staff approvals queue.
        ch = await self._get_text_channel(guild, int(SHOP_REVIEW_TICKET_CHANNEL_ID or APPROVALS_CHANNEL_ID))
        if ch is not None:
            return ch
        return await self._get_text_channel(guild, int(APPROVALS_CHANNEL_ID))

    def _slug_channel_name(self, raw: str, *, fallback: str = "player-shop") -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", (raw or "").strip().lower()).strip("-")
        slug = slug or fallback
        return slug[:90].strip("-") or fallback

    async def _get_forum_channel(self, guild: discord.Guild, channel_id: int | str | None) -> Optional[discord.ForumChannel]:
        if not channel_id:
            return None
        try:
            ch = guild.get_channel(int(channel_id))
            if ch is None:
                ch = await guild.fetch_channel(int(channel_id))
        except Exception:
            return None
        return ch if isinstance(ch, discord.ForumChannel) else None

    async def _get_category_channel(self, guild: discord.Guild, channel_id: int | str | None) -> Optional[discord.CategoryChannel]:
        if not channel_id:
            return None
        try:
            ch = guild.get_channel(int(channel_id))
            if ch is None:
                ch = await guild.fetch_channel(int(channel_id))
        except Exception:
            return None
        return ch if isinstance(ch, discord.CategoryChannel) else None

    def _get_company_row(self, sb, guild_id: int, company_id: str) -> Optional[dict]:
        try:
            res = (
                sb.table("companies")
                .select("*")
                .eq("guild_id", int(guild_id))
                .eq("company_id", str(company_id))
                .limit(1)
                .execute()
            )
            rows = getattr(res, "data", None) or []
            return rows[0] if rows else None
        except Exception:
            traceback.print_exc()
            return None

    def _build_player_shop_storefront_embed(self, *, company: dict) -> discord.Embed:
        name = str(company.get("name") or "Player Shop")
        desc = str(company.get("shop_description") or "Welcome to this player-run shop.").strip()
        embed = discord.Embed(title=f"🏦 {name}", description=self._truncate(desc, 3800), color=discord.Color.dark_teal())

        owner_character_id = company.get("owner_character_id")
        if owner_character_id:
            embed.add_field(name="IC Owner", value=f"`{str(owner_character_id)[:8]}`", inline=True)

        status = str(company.get("shop_status") or "APPROVED")
        embed.add_field(name="Status", value=f"`{status}`", inline=True)

        logo = str(company.get("shop_logo_url") or "").strip()
        banner = str(company.get("shop_banner_url") or "").strip()
        if logo:
            embed.set_thumbnail(url=logo)
        if banner:
            embed.set_image(url=banner)
        embed.set_footer(text=f"Player Shop ID: {str(company.get('company_id') or '')}")
        embed.timestamp = discord.utils.utcnow()
        return embed

    async def _pin_storefront_thread_best_effort(
        self,
        thread: discord.Thread,
        *,
        starter_message: discord.Message | None = None,
    ) -> None:
        """Best-effort pinning for player shop storefronts.

        Discord forum behavior varies a little by client/API version:
        - pinning the thread/post can keep the storefront at the top of the forum when supported
        - pinning the starter message keeps it in the thread's pinned messages when supported
        Both are safe to attempt and harmless if Discord.py/API does not support one of them.
        """

        # Try to pin the forum post/thread itself, if this discord.py version supports it.
        try:
            await thread.edit(pinned=True, reason="Pin player shop storefront")  # type: ignore[call-arg]
        except TypeError:
            # Older discord.py versions do not expose pinned= for forum threads.
            pass
        except discord.HTTPException:
            pass
        except Exception:
            pass

        # Try to pin the starter message inside the thread as well.
        try:
            msg = starter_message
            if msg is None:
                msg = await thread.fetch_message(thread.id)
            if msg is not None:
                try:
                    await msg.pin(reason="Pin player shop storefront message")
                except discord.HTTPException:
                    pass
        except Exception:
            pass

    async def _sync_player_shop_storefront_thread(self, interaction: discord.Interaction, *, company_id: str) -> Optional[int]:
        if not interaction.guild:
            return None
        sb = self.sb()
        guild_id = int(interaction.guild.id)
        company = self._get_company_row(sb, guild_id, company_id)
        if not company:
            return None

        forum = await self._get_forum_channel(interaction.guild, company.get("shop_forum_channel_id"))
        if forum is None:
            return None

        embed = self._build_player_shop_storefront_embed(company=company)
        thread_id = company.get("shop_storefront_thread_id")

        if thread_id:
            try:
                thread = interaction.guild.get_thread(int(thread_id)) or await interaction.guild.fetch_channel(int(thread_id))
                if isinstance(thread, discord.Thread):
                    starter = await thread.fetch_message(thread.id)
                    await starter.edit(content="🏦 **Storefront**", embed=embed)
                    await self._pin_storefront_thread_best_effort(thread, starter_message=starter)
                    return int(thread.id)
            except Exception:
                pass

        try:
            thread_obj = await forum.create_thread(
                name="storefront",
                content="🏦 **Storefront**",
                embed=embed,
                auto_archive_duration=10080,
            )
            thread = getattr(thread_obj, "thread", None) or thread_obj
            starter_message = getattr(thread_obj, "message", None)
            await self._pin_storefront_thread_best_effort(thread, starter_message=starter_message)
            sb.table("companies").update({"shop_storefront_thread_id": int(thread.id)}).eq("guild_id", guild_id).eq("company_id", str(company_id)).execute()
            return int(thread.id)
        except Exception:
            traceback.print_exc()
            return None

    async def _ensure_player_shop_forum(
        self,
        interaction: discord.Interaction,
        *,
        company_id: str,
        company_name: str | None = None,
    ) -> Optional[discord.ForumChannel]:
        """Create or return a dedicated forum channel for a player-run shop."""
        if not interaction.guild:
            return None
        if PLAYER_SHOPS_CATEGORY_ID <= 0:
            return None

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        company = self._get_company_row(sb, guild_id, company_id) or {"company_id": company_id, "name": company_name or "Player Shop"}

        existing = await self._get_forum_channel(interaction.guild, company.get("shop_forum_channel_id"))
        if existing is not None:
            return existing

        category = await self._get_category_channel(interaction.guild, PLAYER_SHOPS_CATEGORY_ID)
        if category is None:
            return None

        name = str(company.get("name") or company_name or "Player Shop")
        channel_name = self._slug_channel_name(name)
        topic = str(company.get("shop_description") or f"Player-run shop: {name}").strip()[:1024]

        try:
            forum = await interaction.guild.create_forum(
                name=channel_name,
                category=category,
                topic=topic,
                reason=f"Player shop forum for {name} ({company_id})",
            )
        except Exception:
            traceback.print_exc()
            return None

        try:
            sb.table("companies").update({
                "shop_status": "APPROVED",
                "shop_category_id": int(category.id),
                "shop_forum_channel_id": int(forum.id),
                "shop_approved_by": int(interaction.user.id),
                "shop_approved_at": self._now_iso(),
            }).eq("guild_id", guild_id).eq("company_id", str(company_id)).execute()
        except Exception:
            traceback.print_exc()

        try:
            await self._sync_player_shop_storefront_thread(interaction, company_id=str(company_id))
        except Exception:
            traceback.print_exc()

        return forum

    async def _resolve_item_publish_forum(self, interaction: discord.Interaction, *, item: dict) -> Optional[discord.ForumChannel]:
        if not interaction.guild:
            return None
        sb = self.sb()
        guild_id = int(interaction.guild.id)

        company_id = str(item.get("vendor_company_id") or "").strip()
        if company_id:
            company = self._get_company_row(sb, guild_id, company_id)
            if company:
                forum = await self._get_forum_channel(interaction.guild, company.get("shop_forum_channel_id"))
                if forum is not None:
                    return forum
                forum = await self._ensure_player_shop_forum(
                    interaction,
                    company_id=company_id,
                    company_name=str(company.get("name") or "Player Shop"),
                )
                if forum is not None:
                    return forum

        settings = self._get_shop_settings(sb, guild_id)
        forum_id = int(settings.get("forum_channel_id") or DEFAULT_FORUM_CHANNEL_ID)
        return await self._get_forum_channel(interaction.guild, forum_id)

    async def _publish_item_to_forum(self, interaction: discord.Interaction, *, item: dict, embed: discord.Embed, view: discord.ui.View) -> Optional[int]:
        if not interaction.guild:
            return None

        fch = await self._resolve_item_publish_forum(interaction, item=item)
        if fch is None:
            return None

        existing_tid = item.get("forum_thread_id")
        if existing_tid:
            try:
                thread = interaction.guild.get_thread(int(existing_tid)) or await interaction.guild.fetch_channel(int(existing_tid))
                if isinstance(thread, discord.Thread):
                    try:
                        starter = await thread.fetch_message(thread.id)
                        await starter.edit(embed=embed, view=view)
                    except Exception:
                        await thread.send("🔁 Updated item details:", embed=embed, view=view)
                    return int(thread.id)
            except Exception:
                pass

        try:
            thread_obj = await fch.create_thread(
                name=str(item.get("name") or "Item")[:100],
                embed=embed,
                view=view,
            )
            th = getattr(thread_obj, "thread", None) or thread_obj
            return int(th.id)
        except Exception:
            traceback.print_exc()
            return None

    def _build_listing_review_embed(self, *, it: dict, company_name: str, submitter: Any = None) -> discord.Embed:
        embed = discord.Embed(title="🧾 Player Shop Listing Review", color=discord.Color.orange())
        embed.add_field(name="Item", value=f"**{self._truncate(it.get('name'), 120)}** (`{str(it.get('item_id') or '')[:8]}`)", inline=False)
        embed.add_field(name="Shop", value=f"🏦 **{self._truncate(company_name, 120)}**", inline=True)
        embed.add_field(name="Price", value=f"`{int(it.get('price') or 0)}`", inline=True)
        stock = it.get("stock")
        embed.add_field(name="Stock", value="∞" if stock is None else f"`{int(stock)}`", inline=True)
        embed.add_field(name="Description", value=self._truncate(it.get("description"), 900), inline=False)

        template_fields = [
            ("Recipe", it.get("recipe_link")),
            ("Unique", it.get("unique_owner")),
            ("Item Class", it.get("item_class")),
            ("CC", it.get("cc")),
            ("Stat Limits", it.get("stat_limits")),
            ("Special Effects", it.get("special_effects")),
            ("Usage Information", it.get("usage_information")),
        ]
        for label, value in template_fields:
            if value is not None and str(value).strip():
                embed.add_field(name=label, value=self._truncate(value, 900), inline=False)

        img = str(it.get("image_url") or "").strip()
        if img:
            embed.set_image(url=img)

        status = str(it.get("review_status") or "PENDING_STAFF_REVIEW")
        embed.add_field(name="Review Status", value=f"`{status}`", inline=True)
        if submitter is not None:
            embed.add_field(name="Submitted By", value=submitter.mention, inline=True)
        embed.set_footer(text="Staff may edit with /shop edit_item, then send edits to owner or approve + publish.")
        embed.timestamp = discord.utils.utcnow()
        return embed

    async def _post_item_review_thread_update(self, interaction: discord.Interaction, *, item: dict, content: str, embed: discord.Embed | None = None, view: discord.ui.View | None = None):
        if not interaction.guild:
            return
        thread_id = item.get("review_thread_id")
        if not thread_id:
            return
        try:
            ch = interaction.guild.get_thread(int(thread_id)) or await interaction.guild.fetch_channel(int(thread_id))
            if isinstance(ch, (discord.Thread, discord.TextChannel)):
                await ch.send(content=content, embed=embed, view=view)
        except Exception:
            traceback.print_exc()

    def _get_company_member_ids(self, sb, company_id: str, *, min_rank: int = 2) -> list[int]:
        try:
            res = sb.table("company_members").select("discord_id,role").eq("company_id", str(company_id)).execute()
            rows = getattr(res, "data", None) or []
            return [int(r["discord_id"]) for r in rows if self._role_rank(r.get("role")) >= min_rank]
        except Exception:
            return []

    async def _get_company_mentions(self, guild: discord.Guild, sb, company_id: str, *, min_rank: int = 2) -> str:
        ids = self._get_company_member_ids(sb, company_id, min_rank=min_rank)
        mentions: list[str] = []
        for uid in ids[:10]:
            member = guild.get_member(uid)
            if member is None:
                try:
                    member = await guild.fetch_member(uid)
                except Exception:
                    member = None
            mentions.append(member.mention if member else f"<@{uid}>")
        return " ".join(mentions) if mentions else "Shop owner/manager"

    async def _post_listing_review_card(
        self,
        interaction: discord.Interaction,
        *,
        item_id: str,
        company_id: str,
        company_name: str,
    ) -> None:
        if not interaction.guild:
            return
        sb = self.sb()
        item = self._get_item(sb, int(interaction.guild.id), item_id)
        if not item:
            return

        review_ch = await self._get_review_channel(interaction.guild)
        if review_ch is None:
            return

        owner_mentions = await self._get_company_mentions(interaction.guild, sb, company_id, min_rank=2)
        embed = self._build_listing_review_embed(it=item, company_name=company_name, submitter=interaction.user)
        content = (
            f"📌 **New player shop listing needs staff review**\n"
            f"Shop owner/manager: {owner_mentions}\n"
            f"Staff can edit with `/shop edit_item`, then use the buttons below."
        )

        msg = await review_ch.send(content=content, embed=embed, view=ListingReviewView(self, item_id=item_id))

        # Create a lightweight review thread when possible. If permissions block it, the queue message still works.
        try:
            thread_name = f"review-{str(item.get('name') or 'item')[:72]}"
            thread = await msg.create_thread(name=thread_name[:100], auto_archive_duration=10080)
            sb.table("shop_items").update({"review_thread_id": int(thread.id)}).eq("guild_id", int(interaction.guild.id)).eq("item_id", str(item_id)).execute()
            try:
                for uid in self._get_company_member_ids(sb, company_id, min_rank=2):
                    member = interaction.guild.get_member(uid)
                    if member:
                        await thread.add_user(member)
            except Exception:
                pass
            await thread.send(
                content=(
                    f"🧵 Review thread opened for **{self._truncate(item.get('name'), 120)}**.\n"
                    f"Owner/manager: {owner_mentions}\n"
                    "Staff can discuss changes here. If this channel is staff-only, set `SHOP_REVIEW_TICKET_CHANNEL_ID` to an owner-visible channel for true owner review tickets."
                ),
                embed=embed,
                view=ListingReviewView(self, item_id=item_id),
            )
        except Exception:
            traceback.print_exc()

    async def _approve_player_listing_by_button(self, interaction: discord.Interaction, *, item_id: str):
        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        it = self._get_item(sb, guild_id, item_id)
        if not it:
            return await self._private(interaction, "Item not found.")

        patch = {"is_active": True, "review_status": "APPROVED", "reviewed_by": int(interaction.user.id), "reviewed_at": self._now_iso()}
        sb.table("shop_items").update(patch).eq("guild_id", guild_id).eq("item_id", str(it["item_id"])).execute()
        new_it = self._get_item(sb, guild_id, str(it["item_id"])) or it

        # Publish directly to the player shop forum if this is a player listing; otherwise use the default forum.
        try:
            cur = get_primary_currency(sb, guild_id)
            embed = self._build_item_embed(it=new_it, cur=cur, guild_id=guild_id)
            view = ShopItemView(self, item_id=str(new_it["item_id"]))
            self.bot.add_view(view)
            thread_id = await self._publish_item_to_forum(interaction, item=new_it, embed=embed, view=view)
            if thread_id:
                sb.table("shop_items").update({"forum_thread_id": int(thread_id)}).eq("guild_id", guild_id).eq("item_id", str(new_it["item_id"])).execute()
        except Exception:
            traceback.print_exc()

        await self._post_item_review_thread_update(
            interaction,
            item=new_it,
            content=f"✅ {interaction.user.mention} approved and published this listing.",
        )
        return await self._public(interaction, content="✅ Listing approved and published.", ephemeral=True)

    async def _send_item_changes_to_owner(self, interaction: discord.Interaction, *, item_id: str, summary: str):
        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        if not summary:
            return await self._private(interaction, "Please include what changed.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        it = self._get_item(sb, guild_id, item_id)
        if not it:
            return await self._private(interaction, "Item not found.")

        sb.table("shop_items").update({
            "review_status": "OWNER_REVIEW",
            "staff_change_summary": summary[:1000],
            "reviewed_by": int(interaction.user.id),
            "reviewed_at": self._now_iso(),
        }).eq("guild_id", guild_id).eq("item_id", str(it["item_id"])).execute()

        company_id = str(it.get("vendor_company_id") or "")
        owner_mentions = await self._get_company_mentions(interaction.guild, sb, company_id, min_rank=2) if company_id else "Shop owner/manager"
        embed = discord.Embed(title="✏️ Staff Edits Need Owner Review", color=discord.Color.blue())
        embed.add_field(name="Item", value=f"**{self._truncate(it.get('name'), 120)}** (`{str(it.get('item_id'))[:8]}`)", inline=False)
        embed.add_field(name="Staff Changes / Notes", value=self._truncate(summary, 1000), inline=False)
        embed.set_footer(text="Owner/manager can approve the changes or request more changes.")
        embed.timestamp = discord.utils.utcnow()

        await self._post_item_review_thread_update(
            interaction,
            item=it,
            content=f"{owner_mentions} staff has changes for you to review.",
            embed=embed,
            view=OwnerChangeReviewView(self, item_id=item_id),
        )
        return await self._public(interaction, content="✅ Sent staff edits to the shop owner/manager for review.", ephemeral=True)

    async def _owner_accepts_staff_changes(self, interaction: discord.Interaction, *, item_id: str):
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        sb = self.sb()
        guild_id = int(interaction.guild.id)
        it = self._get_item(sb, guild_id, item_id)
        if not it:
            return await self._private(interaction, "Item not found.")
        company_id = str(it.get("vendor_company_id") or "")
        rank = self._get_member_rank(sb, company_id, int(interaction.user.id)) if company_id else 0
        if not self._staff_ok(interaction) and not self._require_rank(rank, 2):
            return await self._private(interaction, "❌ Only this shop’s owner/manager can approve these changes.")

        sb.table("shop_items").update({"review_status": "PENDING_STAFF_REVIEW", "owner_reviewed_at": self._now_iso()}).eq("guild_id", guild_id).eq("item_id", str(it["item_id"])).execute()
        await self._post_item_review_thread_update(
            interaction,
            item=it,
            content=f"✅ {interaction.user.mention} approved staff edits. Staff can now approve + publish or keep reviewing.",
            view=ListingReviewView(self, item_id=item_id),
        )
        return await self._public(interaction, content="✅ You approved the staff edits. Sent back to staff for final approval.", ephemeral=True)

    async def _owner_requests_item_changes(self, interaction: discord.Interaction, *, item_id: str, notes: str):
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        sb = self.sb()
        guild_id = int(interaction.guild.id)
        it = self._get_item(sb, guild_id, item_id)
        if not it:
            return await self._private(interaction, "Item not found.")
        company_id = str(it.get("vendor_company_id") or "")
        rank = self._get_member_rank(sb, company_id, int(interaction.user.id)) if company_id else 0
        if not self._staff_ok(interaction) and not self._require_rank(rank, 2):
            return await self._private(interaction, "❌ Only this shop’s owner/manager can request changes.")

        sb.table("shop_items").update({"review_status": "OWNER_REQUESTED_CHANGES", "owner_change_notes": notes[:1000], "owner_reviewed_at": self._now_iso()}).eq("guild_id", guild_id).eq("item_id", str(it["item_id"])).execute()
        await self._post_item_review_thread_update(
            interaction,
            item=it,
            content=f"🔁 {interaction.user.mention} requested more changes.\n**Owner Notes:** {self._truncate(notes, 1000)}",
            view=ListingReviewView(self, item_id=item_id),
        )
        return await self._public(interaction, content="🔁 Sent your requested changes back to staff.", ephemeral=True)

    async def _reject_player_listing(self, interaction: discord.Interaction, *, item_id: str, reason: str):
        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        sb = self.sb()
        guild_id = int(interaction.guild.id)
        it = self._get_item(sb, guild_id, item_id)
        if not it:
            return await self._private(interaction, "Item not found.")
        sb.table("shop_items").update({"review_status": "CHANGES_REQUESTED", "staff_change_summary": reason[:1000], "reviewed_by": int(interaction.user.id), "reviewed_at": self._now_iso(), "is_active": False}).eq("guild_id", guild_id).eq("item_id", str(it["item_id"])).execute()
        company_id = str(it.get("vendor_company_id") or "")
        owner_mentions = await self._get_company_mentions(interaction.guild, sb, company_id, min_rank=2) if company_id else "Shop owner/manager"
        await self._post_item_review_thread_update(
            interaction,
            item=it,
            content=f"❌ {interaction.user.mention} sent this listing back for rework.\n{owner_mentions} **Reason:** {self._truncate(reason, 1000)}",
        )
        return await self._public(interaction, content="✅ Listing sent back for rework.", ephemeral=True)

    def _can_manage_item(self, sb, item: dict, discord_id: int) -> bool:
        company_id = str(item.get("vendor_company_id") or "")
        if not company_id:
            return False
        return self._require_rank(self._get_member_rank(sb, company_id, discord_id), 2)

    @app_commands.command(name="create_player_shop", description="Create your player-run shop/company after buying a shop license")
    @app_commands.describe(name="Your shop/company name")
    async def create_player_shop(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=False)

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        if not self._has_shop_owner_license(interaction):
            if SHOP_OWNER_ROLE_ID <= 0:
                return await self._private(
                    interaction,
                    "❌ Player shop licenses are not configured yet. Staff needs to set `SHOP_OWNER_ROLE_ID` in Railway, or create this shop for you."
                )
            return await self._private(interaction, "❌ You need a Shop Owner License role before creating a player shop.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        owner_id = int(interaction.user.id)
        clean_name = (name or "").strip()

        if not clean_name:
            return await self._private(interaction, "Shop name can’t be empty.")
        if len(clean_name) > 80:
            return await self._private(interaction, "Shop name is too long. Keep it at 80 characters or less.")

        try:
            active_oc = get_active_character(sb, owner_id)
            if not active_oc or not active_oc.get("character_id"):
                return await self._private(interaction, "No active OC set. Use `/oc select <name>` before creating an IC shop.")
            owner_character_id = str(active_oc["character_id"])

            existing = (
                sb.table("companies")
                .select("company_id,name")
                .eq("guild_id", guild_id)
                .eq("name", clean_name)
                .limit(1)
                .execute()
            )
            if getattr(existing, "data", None):
                return await self._private(interaction, "❌ A company/shop with that exact name already exists.")

            ins = sb.table("companies").insert({
                "guild_id": guild_id,
                "name": clean_name,
                "owner_character_id": owner_character_id,
                "shop_description": None,
                "shop_banner_url": None,
                "shop_logo_url": None,
                "shop_status": "APPROVED",
                "shop_category_id": int(PLAYER_SHOPS_CATEGORY_ID) if PLAYER_SHOPS_CATEGORY_ID > 0 else None,
            }).execute()
            row = (getattr(ins, "data", None) or [None])[0]
            if not row:
                return await self._private(interaction, "Failed to create player shop/company.")

            company_id = str(row["company_id"])
            sb.table("company_members").upsert(
                {"company_id": company_id, "discord_id": owner_id, "role": "OWNER"},
                on_conflict="company_id,discord_id",
            ).execute()

            try:
                cur = get_primary_currency(sb, guild_id)
                if cur.get("currency_id"):
                    self._ensure_company_wallet(sb, company_id, str(cur["currency_id"]))
            except Exception:
                traceback.print_exc()

            shop_forum = None
            try:
                shop_forum = await self._ensure_player_shop_forum(
                    interaction,
                    company_id=company_id,
                    company_name=clean_name,
                )
            except Exception:
                traceback.print_exc()

            led = discord.Embed(title="📒 Commerce Ledger", color=discord.Color.green())
            led.add_field(name="Action", value="CREATE_PLAYER_SHOP", inline=True)
            led.add_field(name="Shop", value=f"🏦 **{clean_name}** (`{company_id[:8]}`)", inline=False)
            led.add_field(name="Owner", value=f"{interaction.user.mention} • **{active_oc.get('name','OC')}**", inline=False)
            led.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, embed=led)

            forum_line = f"\n📌 Storefront forum created: {shop_forum.mention}" if shop_forum else "\n⚠️ Player shop was created, but I could not create its forum channel. Check bot Manage Channels permissions and `PLAYER_SHOPS_CATEGORY_ID`."
            return await self._public(
                interaction,
                content=(
                    f"✅ Created player shop **{clean_name}** for **{active_oc.get('name','OC')}** and made {interaction.user.mention} the **OWNER**."
                    f"{forum_line}\n"
                    f"Next: use `/shop edit_player_shop` to set up the storefront, then `/shop submit_player_item` to submit an item for staff review."
                ),
                ephemeral=False,
            )

        except Exception:
            traceback.print_exc()
            return await self._private(interaction, "Server error creating player shop.")

    @app_commands.command(name="my_player_shops", description="Show the player shops/companies you can manage")
    async def my_player_shops(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        uid = int(interaction.user.id)

        try:
            mres = sb.table("company_members").select("company_id,role").eq("discord_id", uid).execute()
            memberships = getattr(mres, "data", None) or []
            if not memberships:
                return await self._private(interaction, "You don’t manage any player shops yet.")

            company_ids = [str(m["company_id"]) for m in memberships]
            cres = sb.table("companies").select("company_id,name").eq("guild_id", guild_id).in_("company_id", company_ids).execute()
            companies = getattr(cres, "data", None) or []
            by_id = {str(c["company_id"]): c for c in companies}

            embed = discord.Embed(title="🏦 My Player Shops", color=discord.Color.dark_teal())
            for m in memberships[:20]:
                cid = str(m["company_id"])
                c = by_id.get(cid)
                if not c:
                    continue
                embed.add_field(
                    name=str(c.get("name") or "Company"),
                    value=f"Role: `{str(m.get('role') or 'MEMBER')}` • ID: `{cid[:8]}`",
                    inline=False,
                )

            if not embed.fields:
                return await self._private(interaction, "You don’t manage any player shops in this server yet.")
            embed.timestamp = discord.utils.utcnow()
            return await self._public(interaction, embed=embed, ephemeral=True)

        except Exception:
            traceback.print_exc()
            return await self._private(interaction, "Server error fetching your player shops.")


    @app_commands.command(name="edit_player_shop", description="Edit your player shop storefront")
    @app_commands.autocomplete(company=_owned_company_autocomplete)
    @app_commands.describe(
        company="Which player shop/company to edit",
        name="New shop name",
        description="Storefront description",
        banner_image="Optional banner image",
        logo_image="Optional logo/thumbnail image",
        clear_images="Clear the current banner/logo images",
    )
    async def edit_player_shop(
        self,
        interaction: discord.Interaction,
        company: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        banner_image: Optional[discord.Attachment] = None,
        logo_image: Optional[discord.Attachment] = None,
        clear_images: bool = False,
    ):
        await interaction.response.defer(ephemeral=False)

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        uid = int(interaction.user.id)

        try:
            rank = self._get_member_rank(sb, company, uid)
            if not self._staff_ok(interaction) and not self._require_rank(rank, 2):
                return await self._private(interaction, "❌ You must be OWNER or MANAGER of that shop to edit the storefront.")

            cres = sb.table("companies").select("company_id,name").eq("guild_id", guild_id).eq("company_id", str(company)).limit(1).execute()
            rows = getattr(cres, "data", None) or []
            if not rows:
                return await self._private(interaction, "Company/shop not found in this server.")

            patch: dict[str, Any] = {}
            if name is not None and name.strip():
                clean_name = name.strip()
                if len(clean_name) > 80:
                    return await self._private(interaction, "Shop name is too long. Keep it at 80 characters or less.")
                patch["name"] = clean_name
            if description is not None:
                patch["shop_description"] = description.strip() or None
            if clear_images:
                patch["shop_banner_url"] = None
                patch["shop_logo_url"] = None
            else:
                if banner_image is not None:
                    patch["shop_banner_url"] = banner_image.url
                if logo_image is not None:
                    patch["shop_logo_url"] = logo_image.url

            if not patch:
                return await self._private(interaction, "No storefront changes provided.")

            sb.table("companies").update(patch).eq("guild_id", guild_id).eq("company_id", str(company)).execute()

            # Keep the dedicated player shop forum channel/storefront thread in sync when possible.
            try:
                updated_company = self._get_company_row(sb, guild_id, str(company)) or {}
                forum = await self._get_forum_channel(interaction.guild, updated_company.get("shop_forum_channel_id"))
                if forum is not None:
                    edit_kwargs: dict[str, Any] = {}
                    if name is not None and name.strip():
                        edit_kwargs["name"] = self._slug_channel_name(name.strip())
                    if description is not None:
                        edit_kwargs["topic"] = (description.strip() or f"Player-run shop: {updated_company.get('name') or 'Player Shop'}")[:1024]
                    if edit_kwargs:
                        await forum.edit(**edit_kwargs, reason=f"Player shop storefront updated by {interaction.user} ({interaction.user.id})")
                    await self._sync_player_shop_storefront_thread(interaction, company_id=str(company))
                elif PLAYER_SHOPS_CATEGORY_ID > 0:
                    await self._ensure_player_shop_forum(interaction, company_id=str(company), company_name=str(updated_company.get("name") or rows[0].get("name") or "Player Shop"))
            except Exception:
                traceback.print_exc()

            embed = discord.Embed(title="🏦 Player Shop Storefront Updated", color=discord.Color.dark_teal())
            embed.add_field(name="Shop", value=f"**{patch.get('name') or rows[0].get('name') or 'Player Shop'}**", inline=False)
            embed.add_field(name="Updated Fields", value=", ".join(sorted(patch.keys())), inline=False)
            embed.add_field(name="By", value=interaction.user.mention, inline=False)
            embed.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, embed=embed)

            return await self._public(interaction, content="✅ Player shop storefront updated.", ephemeral=False)

        except Exception:
            traceback.print_exc()
            return await self._private(interaction, "Server error editing player shop storefront. If this mentions shop_description/shop_banner_url/shop_logo_url, run the SQL patch.")

    @app_commands.command(name="edit_player_item", description="Edit one of your pending player-shop item drafts")
    @app_commands.autocomplete(item=_item_autocomplete)
    @app_commands.describe(
        item="Which pending item/listing to edit",
        name="New item/listing name",
        price="New price",
        description="New listing description",
        recipe_link="Crafting recipe link, if any",
        unique_owner="Who owns this item if it is unique",
        item_class="Weapon class / armor class / item class",
        cc="CC weight / carrying cost",
        stat_limits="Requirements to activate or equip this item",
        special_effects="Special effects this item has",
        usage_information="How this item is used",
        stock="New stock amount",
        purchase_requires_approval="If true, purchases need staff approval before fulfillment",
        item_type="item | consumable | material",
        image="Optional image attachment",
        clear_image="Clear the current image",
        submit_for_review="Send the revised draft back to staff review",
    )
    async def edit_player_item(
        self,
        interaction: discord.Interaction,
        item: str,
        name: Optional[str] = None,
        price: Optional[int] = None,
        description: Optional[str] = None,
        recipe_link: Optional[str] = None,
        unique_owner: Optional[str] = None,
        item_class: Optional[str] = None,
        cc: Optional[int] = None,
        stat_limits: Optional[str] = None,
        special_effects: Optional[str] = None,
        usage_information: Optional[str] = None,
        stock: Optional[int] = None,
        purchase_requires_approval: Optional[bool] = None,
        item_type: Optional[str] = None,
        image: Optional[discord.Attachment] = None,
        clear_image: bool = False,
        submit_for_review: bool = True,
    ):
        await interaction.response.defer(ephemeral=False)

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        uid = int(interaction.user.id)

        try:
            it = self._get_item(sb, guild_id, item)
            if not it:
                return await self._private(interaction, "Item not found.")
            if it.get("is_active", False):
                return await self._private(interaction, "This listing is already active. Ask staff to remove/unpublish it before editing the player draft.")
            if not self._staff_ok(interaction) and not self._can_manage_item(sb, it, uid):
                return await self._private(interaction, "❌ You must be OWNER or MANAGER of that shop to edit this listing.")

            patch: dict[str, Any] = {}
            if name is not None and name.strip():
                patch["name"] = name.strip()
            if price is not None:
                if price <= 0:
                    return await self._private(interaction, "Price must be > 0.")
                patch["price"] = int(price)
            if description is not None:
                patch["description"] = description.strip()
            if stock is not None:
                if stock < 0:
                    return await self._private(interaction, "Stock can't be negative.")
                patch["stock"] = int(stock)
            if purchase_requires_approval is not None:
                patch["requires_approval"] = bool(purchase_requires_approval)
            if item_type is not None:
                patch["item_type"] = self._normalize_item_type(item_type)
            if clear_image:
                patch["image_url"] = None
            elif image is not None:
                patch["image_url"] = image.url
            if cc is not None and cc < 0:
                return await self._private(interaction, "CC can't be negative.")

            patch.update(self._optional_item_template_patch(
                recipe_link=recipe_link,
                unique_owner=unique_owner,
                item_class=item_class,
                cc=cc,
                stat_limits=stat_limits,
                special_effects=special_effects,
                usage_information=usage_information,
            ))

            if submit_for_review:
                patch["review_status"] = "PENDING_STAFF_REVIEW"
                patch["is_active"] = False

            if not patch:
                return await self._private(interaction, "No item changes provided.")

            sb.table("shop_items").update(patch).eq("guild_id", guild_id).eq("item_id", str(it["item_id"])).execute()
            new_it = self._get_item(sb, guild_id, str(it["item_id"])) or it
            company_id = str(new_it.get("vendor_company_id") or "")
            company_name = self._get_company_name(sb, company_id) if company_id else "Player Shop"

            await self._post_item_review_thread_update(
                interaction,
                item=new_it,
                content=f"🔁 {interaction.user.mention} updated this listing. Updated fields: `{', '.join(sorted(patch.keys()))}`",
            )
            if submit_for_review and company_id:
                await self._post_listing_review_card(interaction, item_id=str(new_it["item_id"]), company_id=company_id, company_name=company_name)

            return await self._public(interaction, content="✅ Player item draft updated and sent back to review.", ephemeral=False)

        except Exception:
            traceback.print_exc()
            return await self._private(interaction, "Server error editing player item. If this mentions one of the item-template columns, run the SQL patch.")

    @app_commands.command(name="submit_player_item", description="Submit an item listing for your player-run shop")
    @app_commands.autocomplete(company=_owned_company_autocomplete)
    @app_commands.describe(
        company="Which player shop/company will receive the sales",
        name="Item/listing name",
        price="Price in the server’s primary currency",
        description="Listing description",
        recipe_link="Crafting recipe link, if any",
        unique_owner="Who owns this item if it is unique",
        item_class="Weapon class / armor class / item class",
        cc="CC weight / carrying cost",
        stat_limits="Requirements to activate or equip this item",
        special_effects="Special effects this item has",
        usage_information="How this item is used",
        stock="Stock amount (leave empty for infinite)",
        purchase_requires_approval="If true, each purchase needs staff approval before fulfillment",
        item_type="item | consumable | material",
        image="Optional item image attachment",
    )
    async def submit_player_item(
        self,
        interaction: discord.Interaction,
        company: str,
        name: str,
        price: int,
        description: str = "",
        recipe_link: Optional[str] = None,
        unique_owner: Optional[str] = None,
        item_class: Optional[str] = None,
        cc: Optional[int] = None,
        stat_limits: Optional[str] = None,
        special_effects: Optional[str] = None,
        usage_information: Optional[str] = None,
        stock: Optional[int] = None,
        purchase_requires_approval: bool = False,
        item_type: str = "item",
        image: Optional[discord.Attachment] = None,
    ):
        await interaction.response.defer(ephemeral=False)

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        if price <= 0:
            return await self._public(interaction, content="Price must be > 0.", ephemeral=False)
        if stock is not None and stock < 0:
            return await self._public(interaction, content="Stock can't be negative.", ephemeral=False)
        if cc is not None and cc < 0:
            return await self._public(interaction, content="CC can't be negative.", ephemeral=False)

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        uid = int(interaction.user.id)

        try:
            rank = self._get_member_rank(sb, company, uid)
            if not self._staff_ok(interaction) and not self._require_rank(rank, 2):
                return await self._private(interaction, "❌ You must be OWNER or MANAGER of that shop to submit listings.")

            company_res = sb.table("companies").select("company_id,name").eq("guild_id", guild_id).eq("company_id", str(company)).limit(1).execute()
            company_rows = getattr(company_res, "data", None) or []
            if not company_rows:
                return await self._private(interaction, "Company/shop not found in this server.")
            company_name = str(company_rows[0].get("name") or "Player Shop")

            cur = get_primary_currency(sb, guild_id)
            currency_id = cur.get("currency_id")
            if not currency_id:
                return await self._public(interaction, content="❌ No primary currency configured for this server.", ephemeral=False)

            sres = sb.table("shops").select("shop_id").eq("guild_id", guild_id).limit(1).execute()
            srows = getattr(sres, "data", None) or []
            shop_id = srows[0].get("shop_id") if srows else None
            if not shop_id:
                return await self._public(interaction, content="❌ Shop is not initialized (missing shops.shop_id).", ephemeral=False)

            it_type = self._normalize_item_type(item_type)
            row = {
                "guild_id": guild_id,
                "shop_id": str(shop_id),
                "name": name.strip(),
                "description": (description or "").strip(),
                "price": int(price),
                "currency_id": str(currency_id),
                "stock": int(stock) if stock is not None else None,
                "role_id": None,
                "requires_approval": bool(purchase_requires_approval),
                "weight": None,
                "weight_unit": None,
                "grants_item_id": None,
                "grants_qty": 0,
                "item_type": it_type,
                "image_url": image.url if image is not None else None,
                "vendor_company_id": str(company),
                "submitted_by_discord_id": uid,
                "submitted_by_character_id": str((get_active_character(sb, uid) or {}).get("character_id") or "") or None,
                "recipe_link": (recipe_link or "").strip() or None,
                "unique_owner": (unique_owner or "").strip() or None,
                "item_class": (item_class or "").strip() or None,
                "cc": int(cc) if cc is not None else None,
                "stat_limits": (stat_limits or "").strip() or None,
                "special_effects": (special_effects or "").strip() or None,
                "usage_information": (usage_information or "").strip() or None,
                "review_status": "PENDING_STAFF_REVIEW",
                # Listing approval: staff must approve/publish before it goes public.
                "is_active": False,
                "created_at": self._now_iso(),
            }

            res = sb.table("shop_items").insert(row).execute()
            data = getattr(res, "data", None) or []
            shop_item_id = str(data[0].get("item_id")) if data else "unknown"

            led = discord.Embed(title="📒 Commerce Ledger", color=discord.Color.orange())
            led.add_field(name="Action", value="SUBMIT_PLAYER_ITEM", inline=True)
            led.add_field(name="Seller", value=f"🏦 **{company_name}** (`{str(company)[:8]}`)", inline=False)
            led.add_field(name="Item", value=f"**{name.strip()}** (`{shop_item_id[:8]}`)", inline=False)
            led.add_field(name="Price", value=f"{cur.get('emoji') or '🪙'} `{price}` {cur.get('ticker') or cur.get('name') or 'CUR'}", inline=True)
            led.add_field(name="Status", value="`PENDING STAFF ACTIVATION`", inline=True)
            led.add_field(name="Submitted By", value=interaction.user.mention, inline=False)
            led.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, embed=led)
            if shop_item_id != "unknown":
                await self._post_listing_review_card(
                    interaction,
                    item_id=shop_item_id,
                    company_id=str(company),
                    company_name=company_name,
                )

            return await self._public(
                interaction,
                content=(
                    f"✅ Submitted **{name.strip()}** for **{company_name}**.\n"
                    f"It was sent to <#{APPROVALS_CHANNEL_ID}> for staff review and is inactive until approved."
                ),
                ephemeral=False,
            )

        except Exception as e:
            # Most likely cause if this is the first player-shop patch: missing shop_items.vendor_company_id.
            print(f"[shop submit_player_item] error: {e}")
            traceback.print_exc()
            return await self._private(
                interaction,
                "Server error submitting player item. If this mentions `vendor_company_id`, run the SQL patch for `shop_items.vendor_company_id`."
            )

    # ───────────────────────────────────────────────────────────────────────────
    # PUBLIC COMMANDS
    # ───────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="browse", description="Browse the shop items")
    async def browse(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            settings = self._get_shop_settings(sb, guild_id)
            if not settings.get("enabled", True):
                return await self._public(interaction, content="🛑 The shop is currently disabled.", ephemeral=False)

            items = self._list_items(sb, guild_id, active_only=True, limit=25)
            if not items:
                return await self._public(interaction, content="No items are listed yet.", ephemeral=False)

            cur = get_primary_currency(sb, guild_id)
            emoji = cur.get("emoji") or "🪙"
            ticker = cur.get("ticker") or cur.get("name") or "CUR"

            embed = discord.Embed(title="🛒 Shop", color=discord.Color.blurple())
            embed.description = "Use `/shop buy` or click **Buy** buttons on a storefront post."

            for it in items[:10]:
                name = str(it.get("name") or "Item")
                price = int(it.get("price") or 0)
                desc = str(it.get("description") or "").strip()
                if len(desc) > 140:
                    desc = desc[:140] + "…"

                stock = it.get("stock")
                stock_txt = "∞" if stock is None else str(int(stock))
                approval = "✅ instant" if not it.get("requires_approval") else "🕒 approval"
                weight_txt = self._format_weight(it)
                weight_line = f"\nWeight: `{weight_txt}`" if weight_txt else ""
                it_type = ITEM_TYPE_LABEL.get(self._normalize_item_type(it.get("item_type")), "📦 Item")

                embed.add_field(
                    name=f"{name} • {emoji}{price} {ticker}",
                    value=f"{desc}\nType: `{it_type}` • Stock: `{stock_txt}` • {approval}{weight_line}\nID: `{str(it['item_id'])[:8]}`",
                    inline=False,
                )

            embed.timestamp = discord.utils.utcnow()
            return await self._public(interaction, embed=embed, ephemeral=False)

        except Exception as e:
            print(f"[shop browse] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, "Server error browsing the shop.")

    @app_commands.command(name="buy", description="Buy a shop item")
    @app_commands.autocomplete(item=_item_autocomplete)
    @app_commands.describe(item="Which item to buy", quantity="How many (default 1)", public_receipt="Post receipt publicly")
    async def buy(self, interaction: discord.Interaction, item: str, quantity: int = 1, public_receipt: bool = False):
        await interaction.response.defer(ephemeral=False if public_receipt else True)
        return await self._buy_internal(interaction, item_id=item, quantity=quantity, receipt_public=bool(public_receipt))

    @app_commands.command(name="my_orders", description="Show your recent shop orders")
    async def my_orders(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        uid = int(interaction.user.id)

        try:
            res = (
                sb.table("shop_orders")
                .select("order_id,item_id,quantity,total,status,created_at")
                .eq("guild_id", guild_id)
                .eq("buyer_discord_id", uid)
                .order("created_at", desc=True)
                .limit(15)
                .execute()
            )
            rows = getattr(res, "data", None) or []
            if not rows:
                return await self._private(interaction, "No orders found yet.")

            embed = discord.Embed(title="🧾 My Orders", color=discord.Color.dark_teal())
            for r in rows[:10]:
                code = self._short_order_code(str(r["order_id"]))
                embed.add_field(
                    name=f"{code} • {r.get('status')}",
                    value=f"Item: `{str(r['item_id'])[:8]}` • Qty: `{r.get('quantity')}` • Total: `{r.get('total')}`\nAt: `{str(r.get('created_at',''))[:19]}`",
                    inline=False,
                )
            embed.timestamp = discord.utils.utcnow()
            return await self._public(interaction, embed=embed, ephemeral=True)

        except Exception as e:
            print(f"[shop my_orders] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, "Server error fetching your orders.")

    # ───────────────────────────────────────────────────────────────────────────
    # STAFF COMMANDS
    # ───────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="create_item", description="Staff: Create a shop item")
    @app_commands.autocomplete(grants_item=_inv_item_autocomplete)
    @app_commands.describe(
        name="Item name",
        price="Price (integer)",
        description="Description",
        stock="Stock (leave empty for infinite)",
        role="Optional Discord role to grant on fulfillment",
        requires_approval="If true, staff must approve before fulfillment",
        weight="Optional weight number (integer)",
        weight_unit="Optional weight unit (e.g., WU)",
        grants_item="Inventory item to grant on fulfillment (pick from autocomplete)",
        grants_qty="How many inventory items per 1 purchased",
        item_type="item | consumable | material",
        image="Optional image attachment (shows in embed)",
    )
    async def create_item(
        self,
        interaction: discord.Interaction,
        name: str,
        price: int,
        description: str = "",
        stock: Optional[int] = None,
        role: Optional[discord.Role] = None,
        requires_approval: bool = False,
        weight: Optional[int] = None,
        weight_unit: Optional[str] = None,
        grants_item: Optional[str] = None,   # ✅ Discord-facing (value is item_id)
        grants_qty: int = 1,
        item_type: str = "item",
        image: Optional[discord.Attachment] = None,
    ):
        await interaction.response.defer(ephemeral=False)

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        if price <= 0:
            return await self._public(interaction, content="Price must be > 0.", ephemeral=False)
        if stock is not None and stock < 0:
            return await self._public(interaction, content="Stock can't be negative.", ephemeral=False)
        if weight is not None and weight < 0:
            return await self._public(interaction, content="Weight can't be negative.", ephemeral=False)
        if grants_qty < 0:
            return await self._public(interaction, content="grants_qty can't be negative.", ephemeral=False)

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            cur = get_primary_currency(sb, guild_id)
            currency_id = cur.get("currency_id")
            if not currency_id:
                return await self._public(interaction, content="❌ No primary currency configured for this server.", ephemeral=False)

            sres = sb.table("shops").select("shop_id").eq("guild_id", guild_id).limit(1).execute()
            srows = getattr(sres, "data", None) or []
            shop_id = srows[0].get("shop_id") if srows else None
            if not shop_id:
                return await self._public(interaction, content="❌ Shop is not initialized (missing shops.shop_id).", ephemeral=False)

            g_item_id = (grants_item or "").strip() or None
            if g_item_id:
                inv = self._get_inv_item(sb, guild_id, g_item_id)
                if not inv:
                    return await self._public(interaction, content="❌ grants_item was not found (pick from autocomplete).", ephemeral=False)

            image_url = image.url if image is not None else None
            it_type = self._normalize_item_type(item_type)

            row = {
                "guild_id": guild_id,
                "shop_id": str(shop_id),
                "name": name.strip(),
                "description": (description or "").strip(),
                "price": int(price),
                "currency_id": str(currency_id),
                "stock": int(stock) if stock is not None else None,
                "role_id": int(role.id) if role else None,
                "requires_approval": bool(requires_approval),
                "weight": int(weight) if weight is not None else None,
                "weight_unit": (weight_unit or "").strip()[:12] if weight is not None else None,
                "grants_item_id": g_item_id,
                "grants_qty": int(grants_qty),
                "item_type": it_type,
                "image_url": image_url,
                "is_active": True,
                "created_at": self._now_iso(),
            }

            res = sb.table("shop_items").insert(row).execute()
            data = getattr(res, "data", None) or []
            shop_item_id = str(data[0].get("item_id")) if data else "unknown"

            led = discord.Embed(title="📒 Shop Ledger", color=discord.Color.dark_grey())
            led.add_field(name="Action", value="CREATE_ITEM", inline=True)
            led.add_field(name="Item", value=f"**{name.strip()}** (`{shop_item_id[:8]}`)", inline=False)
            led.add_field(name="Type", value=f"`{ITEM_TYPE_LABEL.get(it_type, '📦 Item')}`", inline=True)
            led.add_field(name="Price", value=str(price), inline=True)
            led.add_field(name="Stock", value=("∞" if stock is None else str(stock)), inline=True)
            if weight is not None:
                led.add_field(name="Weight", value=f"{int(weight)} {(weight_unit or '').strip()}", inline=True)
            if g_item_id and grants_qty > 0:
                inv = self._get_inv_item(sb, guild_id, g_item_id)
                label = inv.get("name", g_item_id[:8]) if inv else g_item_id[:8]
                led.add_field(name="Grants", value=f"{grants_qty} × {label}", inline=True)
            if image_url:
                led.add_field(name="Image", value="✅ attached", inline=True)
            if role:
                led.add_field(name="Role Grant", value=role.mention, inline=True)
            led.add_field(name="By", value=interaction.user.mention, inline=False)
            led.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, embed=led)

            emoji = cur.get("emoji") or "🪙"
            ticker = cur.get("ticker") or cur.get("name") or "CUR"
            return await self._public(
                interaction,
                content=(
                    f"✅ Created shop item **{name}** (id `{shop_item_id[:8]}`) • "
                    f"type `{it_type}` • {emoji} `{ticker}`\n"
                    f"Next: `/shop publish item:{shop_item_id}` (defaults to forum)"
                ),
                ephemeral=False,
            )

        except Exception:
            traceback.print_exc()
            return await self._public(interaction, content="Server error creating item.", ephemeral=False)

    @app_commands.command(name="edit_item", description="Staff: Edit a shop item (and refresh storefront posts)")
    @app_commands.autocomplete(item=_item_autocomplete, grants_item=_inv_item_autocomplete)
    @app_commands.describe(
        item="Which shop item",
        name="New name",
        price="New price",
        description="New description",
        recipe_link="Crafting recipe link, if any",
        unique_owner="Who owns this item if it is unique",
        item_class="Weapon class / armor class / item class",
        cc="CC weight / carrying cost",
        stat_limits="Requirements to activate or equip this item",
        special_effects="Special effects this item has",
        usage_information="How this item is used",
        stock="New stock (empty for infinite)",
        role="New Discord role to grant on fulfillment",
        clear_role="If true, clears the role grant",
        requires_approval="Toggle approval requirement",
        is_active="Toggle active/inactive",
        weight="New weight (0 to clear)",
        weight_unit="New weight unit (e.g., WU)",
        grants_item="Inventory item to grant (pick from autocomplete)",
        grants_qty="How many inventory items per 1 purchased",
        clear_grants="If true, clears grants_item + grants_qty",
        item_type="item | consumable | material",
        image="Optional image attachment (replaces image)",
        clear_image="If true, clears the image",
    )
    async def edit_item(
        self,
        interaction: discord.Interaction,
        item: str,
        name: Optional[str] = None,
        price: Optional[int] = None,
        description: Optional[str] = None,
        recipe_link: Optional[str] = None,
        unique_owner: Optional[str] = None,
        item_class: Optional[str] = None,
        cc: Optional[int] = None,
        stat_limits: Optional[str] = None,
        special_effects: Optional[str] = None,
        usage_information: Optional[str] = None,
        stock: Optional[int] = None,
        role: Optional[discord.Role] = None,
        clear_role: bool = False,
        requires_approval: Optional[bool] = None,
        is_active: Optional[bool] = None,
        weight: Optional[int] = None,
        weight_unit: Optional[str] = None,
        grants_item: Optional[str] = None,
        grants_qty: Optional[int] = None,
        clear_grants: bool = False,
        item_type: Optional[str] = None,
        image: Optional[discord.Attachment] = None,
        clear_image: bool = False,
    ):
        await interaction.response.defer(ephemeral=False)

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            it = self._get_item(sb, guild_id, item)
            if not it:
                return await self._public(interaction, content="Item not found.", ephemeral=False)

            patch: dict[str, Any] = {}
            if name is not None and name.strip():
                patch["name"] = name.strip()
            if price is not None:
                if price <= 0:
                    return await self._public(interaction, content="Price must be > 0.", ephemeral=False)
                patch["price"] = int(price)
            if description is not None:
                patch["description"] = (description or "").strip()
            if stock is not None:
                if stock < 0:
                    return await self._public(interaction, content="Stock can't be negative.", ephemeral=False)
                patch["stock"] = int(stock)
            if cc is not None and cc < 0:
                return await self._public(interaction, content="CC can't be negative.", ephemeral=False)

            patch.update(self._optional_item_template_patch(
                recipe_link=recipe_link,
                unique_owner=unique_owner,
                item_class=item_class,
                cc=cc,
                stat_limits=stat_limits,
                special_effects=special_effects,
                usage_information=usage_information,
            ))
            if clear_role:
                patch["role_id"] = None
            elif role is not None:
                patch["role_id"] = int(role.id)
            if requires_approval is not None:
                patch["requires_approval"] = bool(requires_approval)
            if is_active is not None:
                patch["is_active"] = bool(is_active)

            if item_type is not None:
                patch["item_type"] = self._normalize_item_type(item_type)

            if clear_image:
                patch["image_url"] = None
            elif image is not None:
                patch["image_url"] = image.url

            if weight is not None:
                if int(weight) < 0:
                    return await self._public(interaction, content="Weight can't be negative.", ephemeral=False)
                if int(weight) == 0:
                    patch["weight"] = None
                    patch["weight_unit"] = None
                else:
                    patch["weight"] = int(weight)
                    patch["weight_unit"] = (weight_unit or it.get("weight_unit") or "").strip()[:12] or None

            # grants mapping
            if clear_grants:
                patch["grants_item_id"] = None
                patch["grants_qty"] = 0
            else:
                if grants_item is not None:
                    g = (grants_item or "").strip() or None
                    if g:
                        inv = self._get_inv_item(sb, guild_id, g)
                        if not inv:
                            return await self._public(interaction, content="❌ grants_item not found (pick from autocomplete).", ephemeral=False)
                    patch["grants_item_id"] = g

                if grants_qty is not None:
                    if int(grants_qty) < 0:
                        return await self._public(interaction, content="grants_qty can't be negative.", ephemeral=False)
                    patch["grants_qty"] = int(grants_qty)

            if not patch:
                return await self._public(interaction, content="No changes provided.", ephemeral=False)

            sb.table("shop_items").update(patch).eq("guild_id", guild_id).eq("item_id", str(it["item_id"])).execute()

            new_it = self._get_item(sb, guild_id, str(it["item_id"])) or it
            cur = get_primary_currency(sb, guild_id)
            embed = self._build_item_embed(it=new_it, cur=cur, guild_id=guild_id)

            # update shop message (best effort)
            shop_message_id = new_it.get("shop_message_id")
            if shop_message_id:
                ch_id = int(self._get_shop_settings(sb, guild_id).get("shop_channel_id") or DEFAULT_SHOP_CHANNEL_ID)
                ch = await self._get_text_channel(interaction.guild, ch_id)
                if ch:
                    try:
                        msg = await ch.fetch_message(int(shop_message_id))
                        await msg.edit(embed=embed, view=ShopItemView(self, item_id=str(new_it["item_id"])))
                    except Exception:
                        pass

            # update forum thread starter (best effort)
            forum_thread_id = new_it.get("forum_thread_id")
            if forum_thread_id:
                try:
                    thread = interaction.guild.get_thread(int(forum_thread_id)) or await interaction.guild.fetch_channel(int(forum_thread_id))
                    if isinstance(thread, discord.Thread):
                        try:
                            starter = await thread.fetch_message(thread.id)
                            await starter.edit(embed=embed, view=ShopItemView(self, item_id=str(new_it["item_id"])))
                        except Exception:
                            try:
                                await thread.send("🔁 Updated item details:", embed=embed)
                            except Exception:
                                pass
                except Exception:
                    pass

            led = discord.Embed(title="📒 Shop Ledger", color=discord.Color.dark_grey())
            led.add_field(name="Action", value="EDIT_ITEM", inline=True)
            led.add_field(name="Item", value=f"**{str(new_it.get('name') or '')}** (`{str(new_it['item_id'])[:8]}`)", inline=False)
            led.add_field(name="Patch", value=", ".join(sorted(patch.keys()))[:900] or "—", inline=False)
            led.add_field(name="By", value=interaction.user.mention, inline=False)
            led.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, embed=led)
            await self._post_item_review_thread_update(
                interaction,
                item=new_it,
                content=f"✏️ {interaction.user.mention} edited this listing. Updated fields: `{', '.join(sorted(patch.keys()))}`",
                embed=embed,
            )

            return await self._public(interaction, content="✅ Item updated (and storefront refreshed where possible).", ephemeral=False)

        except Exception:
            traceback.print_exc()
            return await self._public(interaction, content="Server error editing item.", ephemeral=False)

    @app_commands.command(name="publish", description="Staff: Publish a storefront post for an item (defaults to forum)")
    @app_commands.autocomplete(item=_item_autocomplete)
    @app_commands.describe(item="Which shop item", where="Where to post: forum, shop, or both")
    async def publish(self, interaction: discord.Interaction, item: str, where: str = "forum"):
        await interaction.response.defer(ephemeral=False)

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        where = (where or "forum").lower().strip()
        if where not in ("shop", "forum", "both"):
            return await self._public(interaction, content="where must be `forum`, `shop`, or `both`.", ephemeral=False)

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            it = self._get_item(sb, guild_id, item)
            if not it:
                return await self._public(interaction, content="Item not found.", ephemeral=False)

            settings = self._get_shop_settings(sb, guild_id)
            cur = get_primary_currency(sb, guild_id)
            embed = self._build_item_embed(it=it, cur=cur, guild_id=guild_id)
            view = ShopItemView(self, item_id=str(it["item_id"]))
            self.bot.add_view(view)

            patch: dict[str, Any] = {}

            # shop text channel post
            if where in ("shop", "both"):
                ch_id = int(settings.get("shop_channel_id") or DEFAULT_SHOP_CHANNEL_ID)
                ch = interaction.guild.get_channel(ch_id) or await interaction.guild.fetch_channel(ch_id)
                if not isinstance(ch, discord.TextChannel):
                    return await self._public(interaction, content="Shop channel id is not a text channel.", ephemeral=False)

                # If already posted, edit it; otherwise create it.
                existing_id = it.get("shop_message_id")
                if existing_id:
                    try:
                        msg = await ch.fetch_message(int(existing_id))
                        await msg.edit(embed=embed, view=view)
                        patch["shop_message_id"] = int(msg.id)
                    except Exception:
                        msg = await ch.send(embed=embed, view=view)
                        patch["shop_message_id"] = int(msg.id)
                else:
                    msg = await ch.send(embed=embed, view=view)
                    patch["shop_message_id"] = int(msg.id)

            # forum thread post
            # Player-shop listings publish to that shop's dedicated forum under The Market District.
            # General shop items fall back to the default forum channel.
            if where in ("forum", "both"):
                thread_id = await self._publish_item_to_forum(interaction, item=it, embed=embed, view=view)
                if not thread_id:
                    return await self._public(interaction, content="Forum channel could not be found or created.", ephemeral=False)
                patch["forum_thread_id"] = int(thread_id)

            if patch:
                sb.table("shop_items").update(patch).eq("guild_id", guild_id).eq("item_id", str(it["item_id"])).execute()

            return await self._public(interaction, content="✅ Published storefront post(s).", ephemeral=False)

        except Exception:
            traceback.print_exc()
            return await self._public(interaction, content="Server error publishing item.", ephemeral=False)

    @app_commands.command(name="remove_item", description="Staff: Deactivate an item and optionally delete its forum thread/shop post")
    @app_commands.autocomplete(item=_item_autocomplete)
    @app_commands.describe(
        item="Which shop item",
        delete_forum="If true, delete the associated forum thread",
        delete_shop_post="If true, delete the associated shop channel message",
    )
    async def remove_item(self, interaction: discord.Interaction, item: str, delete_forum: bool = True, delete_shop_post: bool = False):
        await interaction.response.defer(ephemeral=False)

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            it = self._get_item(sb, guild_id, item)
            if not it:
                return await self._public(interaction, content="Item not found.", ephemeral=False)

            settings = self._get_shop_settings(sb, guild_id)

            # delete forum thread
            if delete_forum:
                forum_thread_id = it.get("forum_thread_id")
                if forum_thread_id:
                    try:
                        thread = interaction.guild.get_thread(int(forum_thread_id)) or await interaction.guild.fetch_channel(int(forum_thread_id))
                        if isinstance(thread, discord.Thread):
                            await thread.delete(reason=f"Shop item removed by {interaction.user} ({interaction.user.id})")
                    except Exception:
                        pass

            # delete shop message
            if delete_shop_post:
                shop_message_id = it.get("shop_message_id")
                if shop_message_id:
                    try:
                        ch_id = int(settings.get("shop_channel_id") or DEFAULT_SHOP_CHANNEL_ID)
                        ch = await self._get_text_channel(interaction.guild, ch_id)
                        if ch:
                            msg = await ch.fetch_message(int(shop_message_id))
                            await msg.delete()
                    except Exception:
                        pass

            # deactivate + clear pointers
            patch = {"is_active": False}
            if delete_forum:
                patch["forum_thread_id"] = None
            if delete_shop_post:
                patch["shop_message_id"] = None

            sb.table("shop_items").update(patch).eq("guild_id", guild_id).eq("item_id", str(it["item_id"])).execute()

            led = discord.Embed(title="📒 Shop Ledger", color=discord.Color.dark_grey())
            led.add_field(name="Action", value="REMOVE_ITEM", inline=True)
            led.add_field(name="Item", value=f"**{str(it.get('name') or '')}** (`{str(it['item_id'])[:8]}`)", inline=False)
            led.add_field(name="Delete Forum", value=str(bool(delete_forum)), inline=True)
            led.add_field(name="Delete Shop Post", value=str(bool(delete_shop_post)), inline=True)
            led.add_field(name="By", value=interaction.user.mention, inline=False)
            led.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, embed=led)

            return await self._public(interaction, content="🗑️ Item removed (deactivated).", ephemeral=False)

        except Exception:
            traceback.print_exc()
            return await self._public(interaction, content="Server error removing item.", ephemeral=False)

    # approve remains from your version (unchanged besides inventory helper)
    @app_commands.command(name="approve", description="Staff: Approve a pending shop order")
    @app_commands.describe(order_id="Order UUID (or short code like K8-3F7A2C1B or first 8 chars)")
    async def approve(self, interaction: discord.Interaction, order_id: str):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=False)

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            oid = self._parse_order_lookup(order_id)

            q = sb.table("shop_orders").select("*").eq("guild_id", guild_id)
            q = q.eq("order_id", oid) if len(oid) == 36 else q.ilike("order_id", f"{oid}%")

            res = q.limit(1).execute()
            rows = getattr(res, "data", None) or []
            if not rows:
                return await self._public(interaction, content="Order not found.", ephemeral=False)

            order = rows[0]
            if str(order.get("status")) != "PAID":
                return await self._public(interaction, content=f"Order status is `{order.get('status')}`, not pending approval.", ephemeral=False)

            item = self._get_item(sb, guild_id, str(order["item_id"]))
            if not item:
                return await self._public(interaction, content="Item not found for this order.", ephemeral=False)

            # inventory fulfillment on approval
            inv_granted = False
            grants_item_id = item.get("grants_item_id")
            grants_qty = int(item.get("grants_qty") or 0)
            if grants_item_id and grants_qty > 0:
                try:
                    self._grant_inventory(
                        sb,
                        guild_id=guild_id,
                        character_id=str(order.get("buyer_character_id") or ""),
                        item_id=str(grants_item_id),
                        qty=int(grants_qty) * int(order.get("quantity") or 1),
                        actor_discord_id=int(interaction.user.id),
                        context="shop_fulfill_approved",
                        note=f"order={str(order['order_id'])[:8]} shop_item={str(item['item_id'])[:8]}",
                    )
                    inv_granted = True
                except Exception:
                    traceback.print_exc()

            # Role fulfillment on approval (needed for Shop Owner License / Merchant Writ).
            role_granted = False
            role_id = item.get("role_id")
            if role_id and interaction.guild:
                try:
                    member = interaction.guild.get_member(int(order["buyer_discord_id"]))
                    if member is None:
                        member = await interaction.guild.fetch_member(int(order["buyer_discord_id"]))
                    role = interaction.guild.get_role(int(role_id))
                    if member and role:
                        await member.add_roles(role, reason=f"Approved shop order {str(order['order_id'])[:8]}")
                        role_granted = True
                except Exception:
                    traceback.print_exc()

            sb.table("shop_orders").update(
                {"status": "FULFILLED", "approved_by": int(interaction.user.id), "approved_at": self._now_iso()}
            ).eq("guild_id", guild_id).eq("order_id", str(order["order_id"])).execute()

            code = self._short_order_code(str(order["order_id"]))

            led = discord.Embed(title="📒 Shop Ledger", color=discord.Color.dark_grey())
            led.add_field(name="Action", value="APPROVE_ORDER", inline=True)
            led.add_field(name="Order", value=f"`{code}`", inline=True)
            led.add_field(name="Buyer", value=f"<@{int(order['buyer_discord_id'])}>", inline=True)
            led.add_field(name="Item", value=f"**{str(item.get('name') or '')}** (`{str(item['item_id'])[:8]}`)", inline=False)
            led.add_field(name="Inventory Granted", value=str(bool(inv_granted)), inline=True)
            led.add_field(name="Role Granted", value=str(bool(role_granted)), inline=True)
            led.add_field(name="By", value=interaction.user.mention, inline=False)
            led.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, embed=led)

            msg = f"✅ Approved order `{code}`."
            if inv_granted:
                msg += " (inventory delivered)"
            if role_granted:
                msg += " (role granted)"
            return await self._public(interaction, content=msg, ephemeral=False)

        except Exception:
            traceback.print_exc()
            return await self._public(interaction, content="Server error approving order.", ephemeral=False)

    # (your deny/export/pending commands can be appended here later)


    @app_commands.command(name="deny", description="Staff: Deny a pending shop order")
    @app_commands.describe(order_id="Order UUID (or short code like K8-3F7A2C1B or first 8 chars)", reason="Reason for denial")
    async def deny(self, interaction: discord.Interaction, order_id: str, reason: str = "Denied by staff"):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=False)

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            oid = self._parse_order_lookup(order_id)
            q = sb.table("shop_orders").select("*").eq("guild_id", guild_id)
            q = q.eq("order_id", oid) if len(oid) == 36 else q.ilike("order_id", f"{oid}%")

            res = q.limit(1).execute()
            rows = getattr(res, "data", None) or []
            if not rows:
                return await self._public(interaction, content="Order not found.", ephemeral=False)

            order = rows[0]
            if str(order.get("status")) != "PAID":
                return await self._public(interaction, content=f"Order status is `{order.get('status')}`, not pending approval.", ephemeral=False)

            item = self._get_item(sb, guild_id, str(order["item_id"]))
            item_name = str(item.get("name") or "Item") if item else "Item"

            sb.table("shop_orders").update(
                {
                    "status": "DENIED",
                    "approved_by": int(interaction.user.id),
                    "approved_at": self._now_iso(),
                    "denial_reason": (reason or "Denied by staff")[:500],
                }
            ).eq("guild_id", guild_id).eq("order_id", str(order["order_id"])).execute()

            code = self._short_order_code(str(order["order_id"]))
            led = discord.Embed(title="📒 Shop Ledger", color=discord.Color.red())
            led.add_field(name="Action", value="DENY_ORDER", inline=True)
            led.add_field(name="Order", value=f"`{code}`", inline=True)
            led.add_field(name="Buyer", value=f"<@{int(order['buyer_discord_id'])}>", inline=True)
            led.add_field(name="Item", value=f"**{item_name}**", inline=False)
            led.add_field(name="Reason", value=(reason or "Denied by staff")[:900], inline=False)
            led.add_field(name="By", value=interaction.user.mention, inline=False)
            led.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, embed=led)

            return await self._public(
                interaction,
                content=f"❌ Denied order `{code}`. Note: this marks the order denied; it does not automatically refund currency.",
                ephemeral=False,
            )

        except Exception:
            traceback.print_exc()
            return await self._public(interaction, content="Server error denying order.", ephemeral=False)

    # ───────────────────────────────────────────────────────────────────────────
    # Core purchase flow (unchanged from your pasted file EXCEPT inventory helper call)
    # ───────────────────────────────────────────────────────────────────────────
    async def _buy_internal(self, interaction: discord.Interaction, *, item_id: str, quantity: int, receipt_public: bool):
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        if quantity <= 0:
            return await self._private(interaction, "Quantity must be > 0.")
        if quantity > 100:
            return await self._private(interaction, "Quantity too high (max 100).")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        buyer_id = int(interaction.user.id)

        try:
            settings = self._get_shop_settings(sb, guild_id)
            if not settings.get("enabled", True):
                return await self._private(interaction, "🛑 The shop is currently disabled.")

            it = self._get_item(sb, guild_id, item_id)
            if not it or not it.get("is_active", True):
                return await self._private(interaction, "That item wasn't found (or is inactive).")

            cur = get_primary_currency(sb, guild_id)
            currency_id = str(it.get("currency_id") or cur.get("currency_id") or "")
            if not currency_id:
                return await self._private(interaction, "No currency configured for this server/item.")
            emoji = cur.get("emoji") or "🪙"
            ticker = cur.get("ticker") or cur.get("name") or "CUR"

            player = get_active_character(sb, buyer_id)
            if not player:
                return await self._private(interaction, "No active OC set. Use `/oc select <name>`.")

            buyer_character_id = str(player.get("character_id") or "")
            if not buyer_character_id:
                return await self._private(interaction, "Active OC missing character_id (server config issue).")

            ensure_wallet(sb, buyer_character_id, currency_id)

            unit_price = int(it.get("price") or 0)
            if unit_price <= 0:
                return await self._private(interaction, "This item can't be purchased (price is invalid).")

            stock = it.get("stock")
            if stock is not None:
                stock_i = int(stock)
                if stock_i < quantity:
                    return await self._private(interaction, f"Not enough stock. Available: `{stock_i}`.")

            item_name = str(it.get("name") or "Item")
            requires_approval = bool(it.get("requires_approval"))

            subtotal = int(unit_price) * int(quantity)
            if subtotal <= 0:
                return await self._private(interaction, "Subtotal invalid; cannot purchase.")

            shop_id = it.get("shop_id") or None
            if not shop_id:
                sres = sb.table("shops").select("shop_id").eq("guild_id", guild_id).limit(1).execute()
                srows = getattr(sres, "data", None) or []
                if srows:
                    shop_id = srows[0].get("shop_id")
            if not shop_id:
                return await self._private(
                    interaction,
                    "Shop is not initialized (missing shop_id).\n"
                    "Create the guild shop row first in `shops` (with shop_id) or ensure items are linked to a shop_id."
                )

            cut_bps = int(settings.get("treasury_cut_bps") or 0)
            treasury_id = self._resolve_active_treasury_company_id(sb, guild_id) if cut_bps > 0 else None
            cut_amt = self._calc_cut(subtotal, cut_bps) if treasury_id else 0
            vendor_amt = subtotal - cut_amt

            vendor_company_id = str(it.get("vendor_company_id") or "") or (str(treasury_id) if treasury_id else "")
            if not vendor_company_id:
                vendor_company_id = self._resolve_active_treasury_company_id(sb, guild_id) or ""
            if not vendor_company_id:
                return await self._private(
                    interaction,
                    "No active company found to receive payments.\n"
                    "Create/mark a treasury company or set TREASURY_COMPANY_ID.",
                )

            self._ensure_company_wallet(sb, vendor_company_id, currency_id)
            if treasury_id:
                self._ensure_company_wallet(sb, str(treasury_id), currency_id)

            reason_base = f"shop purchase item={str(it['item_id'])[:8]} qty={quantity} name={item_name}"

            if vendor_amt > 0:
                apply_company_transaction(
                    sb,
                    guild_id=guild_id,
                    currency_id=currency_id,
                    tx_type="DEPOSIT",
                    amount=int(vendor_amt),
                    actor_discord_id=buyer_id,
                    from_character_id=buyer_character_id,
                    to_company_id=str(vendor_company_id),
                    reason=reason_base + " vendor",
                )

            if cut_amt > 0 and treasury_id and str(treasury_id) != str(vendor_company_id):
                apply_company_transaction(
                    sb,
                    guild_id=guild_id,
                    currency_id=currency_id,
                    tx_type="DEPOSIT",
                    amount=int(cut_amt),
                    actor_discord_id=buyer_id,
                    from_character_id=buyer_character_id,
                    to_company_id=str(treasury_id),
                    reason=reason_base + f" treasury_cut={cut_bps}bps",
                )

            if stock is not None:
                new_stock = int(stock) - int(quantity)
                sb.table("shop_items").update({"stock": new_stock}).eq("guild_id", guild_id).eq("item_id", str(it["item_id"])).execute()

            status = "PAID" if requires_approval else "FULFILLED"
            fee = 0
            total = int(subtotal) + int(fee)

            order_row: dict[str, Any] = {
                "guild_id": guild_id,
                "shop_id": str(shop_id),
                "buyer_discord_id": buyer_id,
                "buyer_character_id": buyer_character_id,
                "item_id": str(it["item_id"]),
                "currency_id": currency_id,
                "quantity": int(quantity),
                "unit_price": int(unit_price),
                "subtotal": int(subtotal),
                "fee": int(fee),
                "total": int(total),
                "status": status,
                "vendor_company_id": str(vendor_company_id),
                "treasury_company_id": str(treasury_id) if treasury_id else None,
                "requires_approval": bool(requires_approval),
                "reason": reason_base,
                "created_at": self._now_iso(),
            }

            ores = sb.table("shop_orders").insert(order_row).execute()
            odata = getattr(ores, "data", None) or []
            order_id = str(odata[0].get("order_id")) if odata else "unknown"

            # Inventory fulfillment for INSTANT orders only
            inv_granted = False
            grants_item_id = it.get("grants_item_id")
            grants_qty = int(it.get("grants_qty") or 0)
            if status == "FULFILLED" and grants_item_id and grants_qty > 0:
                try:
                    self._grant_inventory(
                        sb,
                        guild_id=guild_id,
                        character_id=buyer_character_id,
                        item_id=str(grants_item_id),
                        qty=int(grants_qty) * int(quantity),
                        actor_discord_id=buyer_id,
                        context="shop_fulfill_instant",
                        note=f"order={str(order_id)[:8]} shop_item={str(it['item_id'])[:8]}",
                    )
                    inv_granted = True
                except Exception:
                    traceback.print_exc()

            granted_role = False
            role_id = it.get("role_id")
            if status == "FULFILLED" and role_id and isinstance(interaction.user, discord.Member):
                try:
                    role = interaction.guild.get_role(int(role_id))
                    if role:
                        await interaction.user.add_roles(role, reason="Shop purchase")
                        granted_role = True
                except Exception:
                    pass

            vendor_name = self._get_company_name(sb, str(vendor_company_id))
            treasury_name = self._get_company_name(sb, str(treasury_id)) if treasury_id else None

            code = self._short_order_code(order_id)
            embed = discord.Embed(title="🧾 Shop Receipt", color=discord.Color.green())
            embed.add_field(name="Order", value=f"`{code}` • `{status}`", inline=False)
            embed.add_field(name="Buyer", value=f"{interaction.user.mention} • **{player.get('name','OC')}**", inline=False)
            embed.add_field(name="Item", value=f"**{item_name}**", inline=True)
            embed.add_field(name="Qty", value=f"`{quantity}`", inline=True)
            embed.add_field(name="Total", value=f"{emoji} `{total}` {ticker}", inline=True)

            wt = self._format_weight(it)
            if wt:
                embed.add_field(name="Weight", value=f"`{wt}`", inline=True)

            if inv_granted:
                embed.add_field(name="Inventory", value="✅ Item delivered to inventory.", inline=False)
            elif grants_item_id and grants_qty > 0 and status != "FULFILLED":
                embed.add_field(name="Inventory", value="🕒 Will be delivered after staff approval.", inline=False)

            if cut_amt > 0 and treasury_name and str(treasury_id) != str(vendor_company_id):
                embed.add_field(
                    name="Split",
                    value=(
                        f"Vendor (**{vendor_name}**): {emoji}`{vendor_amt}`\n"
                        f"Treasury (**{treasury_name}**): {emoji}`{cut_amt}` ({cut_bps} bps)"
                    ),
                    inline=False,
                )
            else:
                embed.add_field(name="Paid To", value=f"🏦 **{vendor_name}**", inline=False)

            if status != "FULFILLED":
                embed.add_field(name="Next Step", value="🕒 This item requires staff approval. Staff will approve/deny soon.", inline=False)
            elif role_id:
                embed.add_field(
                    name="Fulfillment",
                    value=("✅ Role granted." if granted_role else "⚠️ Role configured but could not be granted automatically."),
                    inline=False,
                )

            embed.timestamp = discord.utils.utcnow()

            await self._post_receipt_to_channel(interaction, embed=embed, ping_user=True)

            if requires_approval and order_id != "unknown":
                await self._post_approval_card(
                    interaction,
                    order_id=order_id,
                    buyer_mention=interaction.user.mention,
                    item_name=item_name,
                    quantity=int(quantity),
                    total=int(total),
                    emoji=emoji,
                    ticker=ticker,
                )

            led = discord.Embed(title="📒 Shop Ledger", color=discord.Color.dark_grey())
            led.add_field(name="Action", value="BUY", inline=True)
            led.add_field(name="Order", value=f"`{code}`", inline=True)
            led.add_field(name="Status", value=f"`{status}`", inline=True)
            led.add_field(name="Buyer", value=f"{interaction.user.mention} ({buyer_id})", inline=False)
            led.add_field(name="OC", value=f"**{player.get('name','OC')}** (`{buyer_character_id[:8]}`)", inline=False)
            led.add_field(name="Item", value=f"**{item_name}** (`{str(it['item_id'])[:8]}`)", inline=False)
            led.add_field(name="Qty / Total", value=f"`{quantity}` • {emoji}`{total}` {ticker}", inline=False)
            led.add_field(name="Inventory Granted", value=str(bool(inv_granted)), inline=True)
            led.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, embed=led)

            msg = f"✅ Purchase complete. Receipt posted in <#{RECEIPTS_CHANNEL_ID}>."
            if requires_approval:
                msg = f"🕒 Order submitted for approval. Receipt posted in <#{RECEIPTS_CHANNEL_ID}>."
            return await self._public(interaction, content=msg, ephemeral=not receipt_public)

        except RuntimeError as ex:
            if str(ex) == "INSUFFICIENT_FUNDS":
                return await self._private(interaction, "❌ Not enough funds for that purchase.")
            raise
        except Exception as e:
            print(f"[shop buy_internal] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, "Server error processing that purchase.")



# ─────────────────────────────────────────────────────────────────────────────
# BANK / COMPANY COMMAND GROUP
# Kept in this same extension so shop + bank systems move together.
# ─────────────────────────────────────────────────────────────────────────────

class BankCog(commands.GroupCog, group_name="bank", group_description="Company / bank accounts"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    def sb(self):
        sb = getattr(self.bot, "supabase", None)
        if sb is None:
            raise RuntimeError("Supabase is not configured on the bot.")
        return sb

    def _staff_ok(self, interaction: discord.Interaction) -> bool:
        return _has_admin(interaction) or _is_dev(interaction.user.id)

    async def _private_err(self, interaction: discord.Interaction, msg: str):
        if interaction.response.is_done():
            return await interaction.followup.send(msg, ephemeral=True)
        return await interaction.response.send_message(msg, ephemeral=True)

    async def _public_ok(self, interaction: discord.Interaction, content=None, embed=None):
        if interaction.response.is_done():
            return await interaction.followup.send(content=content, embed=embed)
        return await interaction.response.send_message(content=content, embed=embed)

    async def _post_ledger(self, interaction: discord.Interaction, embed: discord.Embed):
        """Best-effort: post to ledger channel without blocking command."""
        if not interaction.guild:
            return
        channel = interaction.guild.get_channel(LEDGER_CHANNEL_ID)
        if channel is None:
            try:
                channel = await interaction.guild.fetch_channel(LEDGER_CHANNEL_ID)
            except Exception:
                return
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            try:
                await channel.send(embed=embed)
            except Exception:
                pass

    async def _bank_autocomplete(self, interaction: discord.Interaction, current: str):
        if not interaction.guild:
            return []
        sb = self.sb()
        guild_id = int(interaction.guild.id)
        q = (current or "").lower().strip()

        res = sb.table("companies").select("company_id,name").eq("guild_id", guild_id).execute()
        rows = getattr(res, "data", None) or []

        out: list[app_commands.Choice[str]] = []
        for r in rows:
            name = str(r.get("name") or "")
            if q and q not in name.lower():
                continue
            out.append(app_commands.Choice(name=name[:100], value=str(r["company_id"])))
        return out[:25]

    def _role_rank(self, role: str | None) -> int:
        r = (role or "").upper()
        return {"OWNER": 3, "MANAGER": 2, "TELLER": 1}.get(r, 0)

    def _require_rank(self, have: int, need: int) -> bool:
        return have >= need

    def _get_member_rank(self, sb, company_id: str, discord_id: int) -> int:
        res = (
            sb.table("company_members")
            .select("role")
            .eq("company_id", company_id)
            .eq("discord_id", int(discord_id))
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        if not rows:
            return 0
        return self._role_rank(rows[0].get("role"))

    def _get_bank_name(self, sb, bank_id: str) -> str:
        res = sb.table("companies").select("name").eq("company_id", bank_id).limit(1).execute()
        rows = getattr(res, "data", None) or []
        return str(rows[0].get("name") or "Bank") if rows else "Bank"

    def _ensure_company_wallet(self, sb, company_id: str, currency_id: str):
        """
        Ensure company_wallets row exists WITHOUT throwing duplicates AND WITHOUT overwriting balance.
        IMPORTANT: Do NOT include 'balance' in the upsert payload.
        """
        sb.table("company_wallets").upsert(
            {"company_id": company_id, "currency_id": currency_id},
            on_conflict="company_id,currency_id",
        ).execute()

    # ─────────────────────────────────────────────────────────────
    # /bank create, /bank list, /bank balance
    # ─────────────────────────────────────────────────────────────

    @app_commands.command(name="create", description="Staff: Create a bank/company")
    @app_commands.describe(name="Bank name")
    async def create(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer()

        if not self._staff_ok(interaction):
            return await self._private_err(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private_err(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        name = name.strip()
        if not name:
            return await self._private_err(interaction, "Name can’t be empty.")

        try:
            ins = sb.table("companies").insert({"guild_id": guild_id, "name": name}).execute()
            row = (getattr(ins, "data", None) or [None])[0]
            if not row:
                return await self._private_err(interaction, "Failed to create bank.")

            # Auto-add creator as OWNER
            sb.table("company_members").insert(
                {"company_id": row["company_id"], "discord_id": int(interaction.user.id), "role": "OWNER"}
            ).execute()

            ledger = discord.Embed(
                title="Ledger • Bank Created",
                description=f"🏦 **{name}**",
                color=discord.Color.green(),
            )
            ledger.add_field(name="By", value=f"`{interaction.user}`", inline=False)
            ledger.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, ledger)

            return await self._public_ok(interaction, content=f"🏦 Created bank **{name}**")

        except Exception as e:
            print(f"[bank create] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error creating bank.")

    @app_commands.command(name="list", description="List banks/companies in this server")
    async def list(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if not interaction.guild:
            return await self._private_err(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            res = sb.table("companies").select("company_id,name").eq("guild_id", guild_id).execute()
            rows = getattr(res, "data", None) or []
            if not rows:
                return await self._public_ok(
                    interaction,
                    content="No banks yet. Staff can create one with `/bank create`.",
                )

            rows.sort(key=lambda r: str(r.get("name") or ""))

            embed = discord.Embed(title="Banks", color=discord.Color.blurple())
            embed.description = "\n".join([f"- **{r['name']}**" for r in rows])
            return await self._public_ok(interaction, embed=embed)

        except Exception as e:
            print(f"[bank list] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error listing banks.")

    @app_commands.command(name="balance", description="Show a bank's balance (primary currency)")
    @app_commands.describe(bank="Which bank")
    @app_commands.autocomplete(bank=_bank_autocomplete)
    async def balance(self, interaction: discord.Interaction, bank: str):
        await interaction.response.defer()

        if not interaction.guild:
            return await self._private_err(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            cur = get_primary_currency(sb, guild_id)
            self._ensure_company_wallet(sb, bank, cur["currency_id"])

            res = (
                sb.table("company_wallets")
                .select("balance")
                .eq("company_id", bank)
                .eq("currency_id", cur["currency_id"])
                .limit(1)
                .execute()
            )
            rows = getattr(res, "data", None) or []
            bal = int(rows[0]["balance"]) if rows else 0

            bname = self._get_bank_name(sb, bank)
            emoji = cur.get("emoji") or ""

            embed = discord.Embed(
                title=f"🏦 {bname} • Balance",
                description=f"{emoji} **{cur['name']}**: `{bal}`",
                color=discord.Color.dark_teal(),
            )
            return await self._public_ok(interaction, embed=embed)

        except Exception as e:
            print(f"[bank balance] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error getting bank balance.")

    # ─────────────────────────────────────────────────────────────
    # /bank deposit, withdraw, transfer (atomic via RPC)
    # ─────────────────────────────────────────────────────────────

    @app_commands.command(name="deposit", description="Deposit from your active OC into a bank")
    @app_commands.describe(bank="Which bank", amount="Amount", reason="Optional note")
    @app_commands.autocomplete(bank=_bank_autocomplete)
    async def deposit(self, interaction: discord.Interaction, bank: str, amount: int, reason: str | None = None):
        await interaction.response.defer()

        if not interaction.guild:
            return await self._private_err(interaction, "Use this in a server, not DMs.")
        if amount <= 0:
            return await self._private_err(interaction, "Amount must be > 0.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        actor_id = int(interaction.user.id)

        try:
            cur = get_primary_currency(sb, guild_id)
            sender = get_active_character(sb, actor_id)
            if not sender:
                return await self._private_err(interaction, "No active OC set. Use `/oc select <name>`.")

            ensure_wallet(sb, sender["character_id"], cur["currency_id"])
            self._ensure_company_wallet(sb, bank, cur["currency_id"])

            row = apply_company_transaction(
                sb,
                guild_id=guild_id,
                currency_id=cur["currency_id"],
                tx_type="DEPOSIT",
                amount=int(amount),
                actor_discord_id=actor_id,
                from_character_id=sender["character_id"],
                to_company_id=bank,
                reason=reason,
            )

            bname = self._get_bank_name(sb, bank)
            emoji = cur.get("emoji") or ""

            msg = f"🏦 **{sender['name']}** deposited {emoji} `{amount}` **{cur['name']}** into **{bname}**"
            if reason:
                msg += f"\n📝 _{reason}_"

            ledger = discord.Embed(
                title="Ledger • Bank Deposit",
                description=f"{emoji} **+{amount} {cur['ticker']}** → **{bname}**",
                color=discord.Color.green(),
            )
            ledger.add_field(name="From", value=f"**{sender['name']}** (`{interaction.user}`)", inline=False)
            ledger.add_field(name="Bank Balance", value=f"`{row.get('to_company_balance')}`", inline=True)
            ledger.add_field(name="OC Balance", value=f"`{row.get('from_character_balance')}`", inline=True)
            if reason:
                ledger.add_field(name="Reason", value=reason, inline=False)
            ledger.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, ledger)

            return await self._public_ok(interaction, content=msg)

        except RuntimeError as ex:
            if str(ex) == "INSUFFICIENT_FUNDS":
                return await self._private_err(interaction, "❌ Not enough funds.")
            raise
        except Exception as e:
            print(f"[bank deposit] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error depositing.")

    @app_commands.command(name="withdraw", description="Withdraw from a bank into your active OC (requires teller+)")
    @app_commands.describe(bank="Which bank", amount="Amount", reason="Optional note")
    @app_commands.autocomplete(bank=_bank_autocomplete)
    async def withdraw(self, interaction: discord.Interaction, bank: str, amount: int, reason: str | None = None):
        await interaction.response.defer()

        if not interaction.guild:
            return await self._private_err(interaction, "Use this in a server, not DMs.")
        if amount <= 0:
            return await self._private_err(interaction, "Amount must be > 0.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        actor_id = int(interaction.user.id)

        try:
            rank = self._get_member_rank(sb, bank, actor_id)
            if not self._staff_ok(interaction) and not self._require_rank(rank, 1):
                return await self._private_err(interaction, "❌ You must be a bank member (TELLER+) to withdraw.")

            cur = get_primary_currency(sb, guild_id)
            receiver = get_active_character(sb, actor_id)
            if not receiver:
                return await self._private_err(interaction, "No active OC set. Use `/oc select <name>`.")

            ensure_wallet(sb, receiver["character_id"], cur["currency_id"])
            self._ensure_company_wallet(sb, bank, cur["currency_id"])

            row = apply_company_transaction(
                sb,
                guild_id=guild_id,
                currency_id=cur["currency_id"],
                tx_type="WITHDRAW",
                amount=int(amount),
                actor_discord_id=actor_id,
                from_company_id=bank,
                to_character_id=receiver["character_id"],
                reason=reason,
            )

            bname = self._get_bank_name(sb, bank)
            emoji = cur.get("emoji") or ""

            msg = f"🏦 **{receiver['name']}** withdrew {emoji} `{amount}` **{cur['name']}** from **{bname}**"
            if reason:
                msg += f"\n📝 _{reason}_"

            ledger = discord.Embed(
                title="Ledger • Bank Withdraw",
                description=f"{emoji} **-{amount} {cur['ticker']}** ← **{bname}**",
                color=discord.Color.orange(),
            )
            ledger.add_field(name="To", value=f"**{receiver['name']}** (`{interaction.user}`)", inline=False)
            ledger.add_field(name="Bank Balance", value=f"`{row.get('from_company_balance')}`", inline=True)
            ledger.add_field(name="OC Balance", value=f"`{row.get('to_character_balance')}`", inline=True)
            if reason:
                ledger.add_field(name="Reason", value=reason, inline=False)
            ledger.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, ledger)

            return await self._public_ok(interaction, content=msg)

        except RuntimeError as ex:
            if str(ex) == "INSUFFICIENT_FUNDS":
                return await self._private_err(interaction, "❌ Bank has insufficient funds.")
            raise
        except Exception as e:
            print(f"[bank withdraw] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error withdrawing.")

    @app_commands.command(name="transfer", description="Transfer between banks (requires manager+)")
    @app_commands.describe(from_bank="From bank", to_bank="To bank", amount="Amount", reason="Optional note")
    @app_commands.autocomplete(from_bank=_bank_autocomplete, to_bank=_bank_autocomplete)
    async def transfer(self, interaction: discord.Interaction, from_bank: str, to_bank: str, amount: int, reason: str | None = None):
        await interaction.response.defer()

        if not interaction.guild:
            return await self._private_err(interaction, "Use this in a server, not DMs.")
        if amount <= 0:
            return await self._private_err(interaction, "Amount must be > 0.")
        if from_bank == to_bank:
            return await self._private_err(interaction, "Choose two different banks.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        actor_id = int(interaction.user.id)

        try:
            rank = self._get_member_rank(sb, from_bank, actor_id)
            if not self._staff_ok(interaction) and not self._require_rank(rank, 2):
                return await self._private_err(interaction, "❌ You must be MANAGER+ on the source bank to transfer.")

            cur = get_primary_currency(sb, guild_id)
            self._ensure_company_wallet(sb, from_bank, cur["currency_id"])
            self._ensure_company_wallet(sb, to_bank, cur["currency_id"])

            row = apply_company_transaction(
                sb,
                guild_id=guild_id,
                currency_id=cur["currency_id"],
                tx_type="TRANSFER",
                amount=int(amount),
                actor_discord_id=actor_id,
                from_company_id=from_bank,
                to_company_id=to_bank,
                reason=reason,
            )

            from_name = self._get_bank_name(sb, from_bank)
            to_name = self._get_bank_name(sb, to_bank)
            emoji = cur.get("emoji") or ""

            msg = f"🏦 Transferred {emoji} `{amount}` **{cur['name']}** from **{from_name}** → **{to_name}**"
            if reason:
                msg += f"\n📝 _{reason}_"

            ledger = discord.Embed(
                title="Ledger • Bank Transfer",
                description=f"{emoji} **{amount} {cur['ticker']}** • **{from_name}** → **{to_name}**",
                color=discord.Color.gold(),
            )
            ledger.add_field(name="By", value=f"`{interaction.user}`", inline=False)
            ledger.add_field(name=f"{from_name} Balance", value=f"`{row.get('from_company_balance')}`", inline=True)
            ledger.add_field(name=f"{to_name} Balance", value=f"`{row.get('to_company_balance')}`", inline=True)
            if reason:
                ledger.add_field(name="Reason", value=reason, inline=False)
            ledger.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, ledger)

            return await self._public_ok(interaction, content=msg)

        except RuntimeError as ex:
            if str(ex) == "INSUFFICIENT_FUNDS":
                return await self._private_err(interaction, "❌ Source bank has insufficient funds.")
            raise
        except Exception as e:
            print(f"[bank transfer] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error transferring.")

    # ─────────────────────────────────────────────────────────────
    # Membership management (staff-only for now)
    # ─────────────────────────────────────────────────────────────

    @app_commands.command(name="addmember", description="Staff: Add a member to a bank")
    @app_commands.describe(bank="Which bank", user="Who to add", role="OWNER / MANAGER / TELLER")
    @app_commands.autocomplete(bank=_bank_autocomplete)
    async def addmember(self, interaction: discord.Interaction, bank: str, user: discord.Member, role: str):
        await interaction.response.defer()

        if not self._staff_ok(interaction):
            return await self._private_err(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private_err(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        role_u = (role or "").upper().strip()
        if role_u not in ("OWNER", "MANAGER", "TELLER"):
            return await self._private_err(interaction, "Role must be OWNER, MANAGER, or TELLER.")

        try:
            sb.table("company_members").upsert(
                {"company_id": bank, "discord_id": int(user.id), "role": role_u},
                on_conflict="company_id,discord_id",
            ).execute()

            bname = self._get_bank_name(sb, bank)

            ledger = discord.Embed(
                title="Ledger • Bank Member Added",
                description=f"🏦 **{bname}**",
                color=discord.Color.blue(),
            )
            ledger.add_field(name="User", value=f"`{user}`", inline=False)
            ledger.add_field(name="Role", value=role_u, inline=True)
            ledger.add_field(name="By", value=f"`{interaction.user}`", inline=False)
            ledger.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, ledger)

            return await self._public_ok(interaction, content=f"✅ Added {user.mention} as **{role_u}** to **{bname}**.")

        except Exception as e:
            print(f"[bank addmember] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error adding member.")

    @app_commands.command(name="removemember", description="Staff: Remove a member from a bank")
    @app_commands.describe(bank="Which bank", user="Who to remove")
    @app_commands.autocomplete(bank=_bank_autocomplete)
    async def removemember(self, interaction: discord.Interaction, bank: str, user: discord.Member):
        await interaction.response.defer()

        if not self._staff_ok(interaction):
            return await self._private_err(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private_err(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        try:
            sb.table("company_members").delete().eq("company_id", bank).eq("discord_id", int(user.id)).execute()
            bname = self._get_bank_name(sb, bank)

            ledger = discord.Embed(
                title="Ledger • Bank Member Removed",
                description=f"🏦 **{bname}**",
                color=discord.Color.red(),
            )
            ledger.add_field(name="User", value=f"`{user}`", inline=False)
            ledger.add_field(name="By", value=f"`{interaction.user}`", inline=False)
            ledger.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, ledger)

            return await self._public_ok(interaction, content=f"🗑️ Removed {user.mention} from **{bname}**.")

        except Exception as e:
            print(f"[bank removemember] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error removing member.")



async def setup(bot: commands.Bot):
    # One extension, two familiar command groups: /shop and /bank.
    await bot.add_cog(ShopCog(bot))
    await bot.add_cog(BankCog(bot))
