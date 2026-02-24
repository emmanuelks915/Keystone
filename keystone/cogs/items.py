# cogs/items.py
from __future__ import annotations

import math
import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional, List

from config.skyfall import SKYFALL_GUILD
from services.permissions import is_staff
from services.oc_service import get_oc_by_owner_and_name_or_raise
from services.inventory_service import add_item_qty, get_item_qty
from services.store_service import list_items_for_sale, get_item_by_name
from services.log_service import log_tx
from services.db import get_supabase_client
from ui.confirm import ConfirmDeleteView

# ---- currency emojis ----
TOKEN_EMOJI = "<:token:1447676379536691201>"
THRAL_EMOJI = "<:thral:1388999536143499477>"

# Discord limits
MAX_SELECT_OPTIONS = 25


# ---------------- OC autocomplete helpers ----------------
async def oc_name_autocomplete_all(interaction: discord.Interaction, current: str):
    sb = get_supabase_client()
    cur = (current or "").strip()
    try:
        q = sb.table("ocs").select("oc_name")
        if cur:
            q = q.ilike("oc_name", f"%{cur}%")
        res = q.order("oc_name").limit(25).execute()
        rows = getattr(res, "data", None) or []
        return [app_commands.Choice(name=r["oc_name"], value=r["oc_name"]) for r in rows if r.get("oc_name")]
    except Exception:
        return []


async def oc_name_autocomplete_owner(interaction: discord.Interaction, current: str):
    sb = get_supabase_client()
    cur = (current or "").strip()
    try:
        q = sb.table("ocs").select("oc_name").eq("owner_discord_id", str(interaction.user.id))
        if cur:
            q = q.ilike("oc_name", f"%{cur}%")
        res = q.order("oc_name").limit(25).execute()
        rows = getattr(res, "data", None) or []
        return [app_commands.Choice(name=r["oc_name"], value=r["oc_name"]) for r in rows if r.get("oc_name")]
    except Exception:
        return []


async def oc_name_autocomplete_target_player(interaction: discord.Interaction, current: str):
    sb = get_supabase_client()
    cur = (current or "").strip()
    try:
        ns = getattr(interaction, "namespace", None)
        to_player = getattr(ns, "to_player", None) if ns else None
        if not to_player:
            return []

        q = sb.table("ocs").select("oc_name").eq("owner_discord_id", str(to_player.id))
        if cur:
            q = q.ilike("oc_name", f"%{cur}%")

        res = q.order("oc_name").limit(25).execute()
        rows = getattr(res, "data", None) or []
        return [app_commands.Choice(name=r["oc_name"], value=r["oc_name"]) for r in rows if r.get("oc_name")]
    except Exception:
        return []


# ---------------- Item autocomplete helpers ----------------
async def item_name_autocomplete_for_sale(interaction: discord.Interaction, current: str):
    sb = get_supabase_client()
    cur = (current or "").strip()
    try:
        q = sb.table("item_catalog").select("item_name").eq("for_sale", True).eq("active", True)
        if cur:
            q = q.ilike("item_name", f"%{cur}%")
        res = q.order("item_name").limit(25).execute()
        rows = getattr(res, "data", None) or []
        return [app_commands.Choice(name=r["item_name"], value=r["item_name"]) for r in rows if r.get("item_name")]
    except Exception:
        return []


async def item_name_autocomplete_all(interaction: discord.Interaction, current: str):
    sb = get_supabase_client()
    cur = (current or "").strip()
    try:
        q = sb.table("item_catalog").select("item_name")
        if cur:
            q = q.ilike("item_name", f"%{cur}%")
        res = q.order("item_name").limit(25).execute()
        rows = getattr(res, "data", None) or []
        return [app_commands.Choice(name=r["item_name"], value=r["item_name"]) for r in rows if r.get("item_name")]
    except Exception:
        return []


# ---------------- wallet helpers ----------------
def _get_token_balance(sb, oc_id: str) -> int:
    res = sb.table("token_wallets").select("balance").eq("oc_id", oc_id).limit(1).execute()
    rows = getattr(res, "data", None) or []
    return int(rows[0].get("balance") or 0) if rows else 0


def _set_token_balance(sb, oc_id: str, new_balance: int) -> int:
    new_balance = int(new_balance)
    if new_balance < 0:
        raise ValueError("Tokens cannot go below 0.")
    sb.table("token_wallets").upsert({"oc_id": oc_id, "balance": new_balance}, on_conflict="oc_id").execute()
    return new_balance


def _get_thral_balance(sb, oc_id: str) -> int:
    res = sb.table("thral_wallets").select("balance").eq("oc_id", oc_id).limit(1).execute()
    rows = getattr(res, "data", None) or []
    return int(rows[0].get("balance") or 0) if rows else 0


def _set_thral_balance(sb, oc_id: str, new_balance: int) -> int:
    new_balance = int(new_balance)
    if new_balance < 0:
        raise ValueError("Thral cannot go below 0.")
    sb.table("thral_wallets").upsert({"oc_id": oc_id, "balance": new_balance}, on_conflict="oc_id").execute()
    return new_balance


def _charge_costs(sb, oc_id: str, token_cost: int, thral_cost: int) -> tuple[int, int]:
    token_cost = int(token_cost or 0)
    thral_cost = int(thral_cost or 0)

    token_bal = _get_token_balance(sb, oc_id)
    thral_bal = _get_thral_balance(sb, oc_id)

    if token_cost > 0 and token_bal < token_cost:
        raise ValueError(f"Not enough Tokens. Need {token_cost}, you have {token_bal}.")
    if thral_cost > 0 and thral_bal < thral_cost:
        raise ValueError(f"Not enough Thral. Need {thral_cost}, you have {thral_bal}.")

    if token_cost > 0:
        token_bal = _set_token_balance(sb, oc_id, token_bal - token_cost)
    if thral_cost > 0:
        thral_bal = _set_thral_balance(sb, oc_id, thral_bal - thral_cost)

    return token_bal, thral_bal


def _refund_costs(sb, oc_id: str, token_amt: int, thral_amt: int) -> None:
    token_amt = int(token_amt or 0)
    thral_amt = int(thral_amt or 0)

    if token_amt > 0:
        cur = _get_token_balance(sb, oc_id)
        _set_token_balance(sb, oc_id, cur + token_amt)

    if thral_amt > 0:
        cur = _get_thral_balance(sb, oc_id)
        _set_thral_balance(sb, oc_id, cur + thral_amt)


def _format_price(it: dict) -> str:
    token_cost = int(it.get("token_cost") or 0)
    thral_cost = int(it.get("thral_cost") or 0)

    parts = []
    if token_cost > 0:
        parts.append(f"**{token_cost}** {TOKEN_EMOJI}")
    if thral_cost > 0:
        parts.append(f"**{thral_cost}** {THRAL_EMOJI}")

    return " + ".join(parts) if parts else "—"


def _bucket_items(items: list[dict]) -> dict[str, list[dict]]:
    token_items = [i for i in items if int(i.get("token_cost") or 0) > 0 and int(i.get("thral_cost") or 0) == 0]
    thral_items = [i for i in items if int(i.get("thral_cost") or 0) > 0 and int(i.get("token_cost") or 0) == 0]
    mixed_items = [i for i in items if int(i.get("token_cost") or 0) > 0 and int(i.get("thral_cost") or 0) > 0]
    free_items = [i for i in items if int(i.get("token_cost") or 0) == 0 and int(i.get("thral_cost") or 0) == 0]

    def _sort_key(i: dict):
        return (int(i.get("token_cost") or 0), int(i.get("thral_cost") or 0), (i.get("item_name") or "").lower())

    for b in (token_items, thral_items, mixed_items, free_items):
        b.sort(key=_sort_key)

    return {
        "all": sorted(items, key=_sort_key),
        "token": token_items,
        "thral": thral_items,
        "mixed": mixed_items,
        "free": free_items,
    }


# =========================
# Store UI helpers (paged embed + dropdown slice)
# =========================
def _category_suffix(key: str) -> str:
    return {
        "all": "",
        "token": f" • {TOKEN_EMOJI} Token Items",
        "thral": f" • {THRAL_EMOJI} Thral Items",
        "mixed": " • 🔀 Mixed Currency",
        "free": " • 🆓 Free / Unpriced",
    }.get(key, "")


def _build_store_embed_paged(
    items: list[dict],
    title_suffix: str = "",
    page: int = 0,
    per_page: int = 8,
) -> discord.Embed:
    """
    Paged store embed that avoids the 25-field limit by rendering into description.
    """
    embed = discord.Embed(
        title=f"🛒 Item Store{title_suffix}",
        description="Use the dropdowns below to pick an OC + item, then press **Buy**.\n(You can also use `/item buy` anytime.)",
        color=discord.Color.green(),
    )

    total = len(items)
    if total == 0:
        embed.description += "\n\n_No items found._"
        embed.set_footer(text="Page 1/1")
        return embed

    pages = max(1, math.ceil(total / max(1, per_page)))
    page = max(0, min(page, pages - 1))

    start = page * per_page
    end = start + per_page
    chunk = items[start:end]

    blocks: List[str] = []
    for it in chunk:
        name = it.get("item_name") or "Unnamed Item"
        effect = (it.get("effect") or "—").strip()
        if len(effect) > 220:
            effect = effect[:220] + "…"
        duration = it.get("duration") or "—"
        price_line = _format_price(it)

        line = f"**{name}** — {price_line}\nEffect: {effect}\nDuration: {duration}"
        if it.get("doc_url"):
            line += f"\n[Doc]({it['doc_url']})"
        blocks.append(line)

    text = "\n\n".join(blocks)
    if len(text) > 3900:
        text = text[:3900] + "\n\n…"

    embed.description += "\n\n" + text
    embed.set_footer(text=f"Page {page+1}/{pages} • Showing {start+1}-{min(end, total)} of {total}")
    return embed


def _slice_for_select(items: list[dict], page: int, per_page: int = MAX_SELECT_OPTIONS) -> list[dict]:
    """
    Slice for the dropdown (max 25 options).
    Dropdown follows the SAME page buttons as the embed (ONE pager).
    """
    total = len(items)
    pages = max(1, math.ceil(total / max(1, per_page)))
    page = max(0, min(page, pages - 1))
    start = page * per_page
    end = start + per_page
    return items[start:end]


def _set_select_default(select: discord.ui.Select, selected_value: Optional[str]) -> None:
    """Marks the selected option as default so the UI visually 'sticks' after interaction."""
    if not getattr(select, "options", None):
        return
    for opt in select.options:
        opt.default = bool(selected_value) and (opt.value == selected_value)


# =========================
# Interactive Store UI (Paged, ONE pager)
# =========================
class StoreBuyModal(discord.ui.Modal, title="Buy Item"):
    quantity = discord.ui.TextInput(label="Quantity", placeholder="1", default="1", required=True, max_length=6)

    def __init__(self, view: "StoreView"):
        super().__init__()
        self.store_view = view

    async def on_submit(self, interaction: discord.Interaction):
        view = self.store_view
        if interaction.user.id != view.owner_id:
            return await interaction.response.send_message("This menu isn’t for you.", ephemeral=True)

        if not view.selected_item_id or not view.selected_oc_id:
            return await interaction.response.send_message("Select an OC and an item first.", ephemeral=True)

        # Parse quantity
        try:
            qty = int(str(self.quantity.value).strip())
        except Exception:
            return await interaction.response.send_message("Quantity must be a number.", ephemeral=True)

        if qty <= 0:
            return await interaction.response.send_message("Quantity must be > 0.", ephemeral=True)

        sb = get_supabase_client()

        # Fetch item fresh (source of truth)
        it = get_item_by_name(view.selected_item_name or "")
        if not it:
            return await interaction.response.send_message("That item no longer exists.", ephemeral=True)
        if not bool(it.get("for_sale", True)) or not bool(it.get("active", True)):
            return await interaction.response.send_message("That item is not available for purchase.", ephemeral=True)

        token_price = int(it.get("token_cost") or 0)
        thral_price = int(it.get("thral_cost") or 0)

        token_total = token_price * qty
        thral_total = thral_price * qty

        try:
            _charge_costs(sb, view.selected_oc_id, token_total, thral_total)
            try:
                add_item_qty(view.selected_oc_id, it["item_id"], qty)
            except Exception:
                _refund_costs(sb, view.selected_oc_id, token_total, thral_total)
                raise

            log_tx(
                interaction.user.id,
                view.selected_oc_id,
                "BUY_ITEM",
                ap_delta=0,
                item_id=it["item_id"],
                quantity=qty,
                notes=f"Bought {it.get('item_name')} | token={token_total} thral={thral_total}",
            )
        except Exception as e:
            return await interaction.response.send_message(f"Error: {e}", ephemeral=True)

        parts = []
        if token_total > 0:
            parts.append(f"**{token_total}** {TOKEN_EMOJI}")
        if thral_total > 0:
            parts.append(f"**{thral_total}** {THRAL_EMOJI}")
        cost_str = " + ".join(parts) if parts else "free"

        await interaction.response.send_message(
            f"Purchased **{qty}x {it.get('item_name')}** for {cost_str} on **{view.selected_oc_name}**.",
            ephemeral=True,
        )


class StoreCategorySelect(discord.ui.Select):
    def __init__(self, view: "StoreView"):
        self.store_view = view
        options = [
            discord.SelectOption(label="All Items", value="all", description="Everything for sale"),
            discord.SelectOption(label="Token Items", value="token", description="Priced only in Tokens"),
            discord.SelectOption(label="Thral Items", value="thral", description="Priced only in Thral"),
            discord.SelectOption(label="Mixed Currency", value="mixed", description="Costs Tokens + Thral"),
            discord.SelectOption(label="Free / Unpriced", value="free", description="0 cost items"),
        ]
        super().__init__(placeholder="Filter store…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        v = self.store_view
        if interaction.user.id != v.owner_id:
            return await interaction.response.send_message("This menu isn’t for you.", ephemeral=True)

        v.category_key = self.values[0]

        # reset paging + item selection on filter switch (keep OC)
        v.page = 0
        v.selected_item_id = None
        v.selected_item_name = None
        v.buy_button.disabled = True

        for opt in self.options:
            opt.default = (opt.value == v.category_key)

        bucket = v.current_bucket()

        # dropdown follows page
        v.item_select.refresh_options(_slice_for_select(bucket, v.page), selected_item_id=None)

        v._sync_nav_buttons()
        embed = _build_store_embed_paged(bucket, _category_suffix(v.category_key), page=v.page, per_page=v.embed_per_page)

        v.sync_defaults()
        await interaction.response.edit_message(embed=embed, view=v)


class StoreOCSelect(discord.ui.Select):
    def __init__(self, view: "StoreView", oc_rows: list[dict]):
        self.store_view = view
        options: list[discord.SelectOption] = []

        for r in oc_rows[:MAX_SELECT_OPTIONS]:
            oc_id = r.get("oc_id")
            oc_name = r.get("oc_name")
            if not oc_id or not oc_name:
                continue
            options.append(discord.SelectOption(label=str(oc_name), value=str(oc_id)))

        super().__init__(
            placeholder="Select OC…",
            min_values=1,
            max_values=1,
            options=options or [discord.SelectOption(label="No OCs found", value="none")],
            disabled=(len(options) == 0),
        )

    async def callback(self, interaction: discord.Interaction):
        v = self.store_view
        if interaction.user.id != v.owner_id:
            return await interaction.response.send_message("This menu isn’t for you.", ephemeral=True)

        oc_id = self.values[0]
        if oc_id == "none":
            v.selected_oc_id = None
            v.selected_oc_name = None
        else:
            v.selected_oc_id = oc_id
            v.selected_oc_name = next((opt.label for opt in self.options if opt.value == oc_id), None)

        v.buy_button.disabled = not (v.selected_oc_id and v.selected_item_id)
        v.sync_defaults()
        await interaction.response.edit_message(view=v)


class StoreItemSelect(discord.ui.Select):
    def __init__(self, view: "StoreView", items: list[dict]):
        self.store_view = view
        super().__init__(placeholder="Select an item…", min_values=1, max_values=1, options=[])
        self.refresh_options(items, selected_item_id=None)

    def refresh_options(self, items: list[dict], selected_item_id: Optional[str]):
        options: list[discord.SelectOption] = []
        for it in items[:MAX_SELECT_OPTIONS]:
            item_id = it.get("item_id")
            item_name = it.get("item_name")
            if not item_id or not item_name:
                continue
            price_hint = _format_price(it)
            options.append(
                discord.SelectOption(
                    label=str(item_name),
                    value=str(item_id),
                    description=price_hint[:100],
                    default=bool(selected_item_id) and (str(item_id) == str(selected_item_id)),
                )
            )

        if not options:
            self.options = [discord.SelectOption(label="No items on this page", value="none")]
            self.disabled = True
            return

        self.options = options
        self.disabled = False

    async def callback(self, interaction: discord.Interaction):
        v = self.store_view
        if interaction.user.id != v.owner_id:
            return await interaction.response.send_message("This menu isn’t for you.", ephemeral=True)

        item_id = self.values[0]
        if item_id == "none":
            v.selected_item_id = None
            v.selected_item_name = None
        else:
            v.selected_item_id = item_id
            v.selected_item_name = next((opt.label for opt in self.options if opt.value == item_id), None)

        v.buy_button.disabled = not (v.selected_oc_id and v.selected_item_id)
        v.sync_defaults()
        await interaction.response.edit_message(view=v)


class StoreBuyButton(discord.ui.Button):
    def __init__(self, view: "StoreView"):
        super().__init__(label="Buy", style=discord.ButtonStyle.success, disabled=True)
        self.store_view = view

    async def callback(self, interaction: discord.Interaction):
        v = self.store_view
        if interaction.user.id != v.owner_id:
            return await interaction.response.send_message("This button isn’t for you.", ephemeral=True)

        if not v.selected_oc_id:
            return await interaction.response.send_message("Pick an OC first.", ephemeral=True)
        if not v.selected_item_id:
            return await interaction.response.send_message("Pick an item first.", ephemeral=True)

        await interaction.response.send_modal(StoreBuyModal(v))


class PageNavButton(discord.ui.Button):
    def __init__(self, view: "StoreView", direction: str):
        label = "◀ Page" if direction == "prev" else "Page ▶"
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self.store_view = view
        self.direction = direction

    async def callback(self, interaction: discord.Interaction):
        v = self.store_view
        if interaction.user.id != v.owner_id:
            return await interaction.response.send_message("This menu isn’t for you.", ephemeral=True)

        bucket = v.current_bucket()
        pages = v.pages()

        if self.direction == "prev":
            v.page = max(0, v.page - 1)
        else:
            v.page = min(pages - 1, v.page + 1)

        # page change invalidates item selection (avoid buying wrong item)
        v.selected_item_id = None
        v.selected_item_name = None
        v.buy_button.disabled = True

        # dropdown follows page
        v.item_select.refresh_options(_slice_for_select(bucket, v.page), selected_item_id=None)

        v._sync_nav_buttons()
        embed = _build_store_embed_paged(bucket, _category_suffix(v.category_key), page=v.page, per_page=v.embed_per_page)

        v.sync_defaults()
        await interaction.response.edit_message(embed=embed, view=v)


class StoreView(discord.ui.View):
    def __init__(self, owner_id: int, buckets: dict[str, list[dict]], oc_rows: list[dict], embed_per_page: int = 8):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.buckets = buckets

        self.category_key: str = "all"
        self.selected_oc_id: Optional[str] = None
        self.selected_oc_name: Optional[str] = None
        self.selected_item_id: Optional[str] = None
        self.selected_item_name: Optional[str] = None

        # ONE pager
        self.embed_per_page = embed_per_page
        self.page = 0

        # UI elements
        self.category_select = StoreCategorySelect(self)
        self.oc_select = StoreOCSelect(self, oc_rows)
        self.item_select = StoreItemSelect(self, _slice_for_select(self.current_bucket(), self.page))
        self.buy_button = StoreBuyButton(self)

        self.prev_page_button = PageNavButton(self, "prev")
        self.next_page_button = PageNavButton(self, "next")

        self.add_item(self.category_select)
        self.add_item(self.oc_select)
        self.add_item(self.item_select)
        self.add_item(self.buy_button)
        self.add_item(self.prev_page_button)
        self.add_item(self.next_page_button)

        self._sync_nav_buttons()
        self.sync_defaults()

    def current_bucket(self) -> list[dict]:
        return self.buckets.get(self.category_key, [])

    def pages(self) -> int:
        total = len(self.current_bucket())
        return max(1, math.ceil(total / max(1, self.embed_per_page)))

    def _sync_nav_buttons(self):
        self.page = max(0, min(self.page, self.pages() - 1))
        self.prev_page_button.disabled = (self.page <= 0)
        self.next_page_button.disabled = (self.page >= self.pages() - 1)

        self.prev_page_button.label = f"◀ Page ({self.page+1}/{self.pages()})"
        self.next_page_button.label = f"Page ▶ ({self.page+1}/{self.pages()})"

    def sync_defaults(self) -> None:
        _set_select_default(self.category_select, self.category_key)
        _set_select_default(self.oc_select, self.selected_oc_id or "none")
        _set_select_default(self.item_select, self.selected_item_id or "none")

    async def on_timeout(self):
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True


# =========================
# Cog
# =========================
class Items(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    item_group = app_commands.Group(name="item", description="Item store and item actions")

    # ---------------- Store ----------------
    @item_group.command(name="store", description="View items currently for sale.")
    @app_commands.describe(public="Show publicly? (default: true)")
    async def store(self, interaction: discord.Interaction, public: Optional[bool] = True):
        ephemeral = not bool(public)
        await interaction.response.defer(ephemeral=ephemeral)

        items = list_items_for_sale()
        if not items:
            return await interaction.followup.send("No items are currently for sale.", ephemeral=ephemeral)

        buckets = _bucket_items(items)

        sb = get_supabase_client()
        oc_res = (
            sb.table("ocs")
            .select("oc_id, oc_name")
            .eq("owner_discord_id", str(interaction.user.id))
            .order("oc_name")
            .limit(MAX_SELECT_OPTIONS)
            .execute()
        )
        oc_rows = getattr(oc_res, "data", None) or []

        embed = _build_store_embed_paged(buckets["all"], "", page=0, per_page=8)
        view = StoreView(owner_id=interaction.user.id, buckets=buckets, oc_rows=oc_rows, embed_per_page=8)

        if not oc_rows:
            embed.set_footer(text="You have no registered OCs. Use /oc_register first.")

        await interaction.followup.send(embed=embed, view=view, ephemeral=ephemeral)

    # ---------------- Info ----------------
    @item_group.command(name="info", description="View info for an item.")
    @app_commands.autocomplete(item_name=item_name_autocomplete_all)
    async def info(self, interaction: discord.Interaction, item_name: str):
        await interaction.response.defer(ephemeral=False)

        it = get_item_by_name(item_name)
        if not it:
            return await interaction.followup.send("Item not found (check exact name).", ephemeral=False)

        embed = discord.Embed(title=f"Item • {it.get('item_name','(unknown)')}", color=discord.Color.teal())
        embed.add_field(name="Price", value=_format_price(it), inline=True)
        embed.add_field(name="For Sale", value=f"**{bool(it.get('for_sale', True))}**", inline=True)
        embed.add_field(name="Active", value=f"**{bool(it.get('active', True))}**", inline=True)
        embed.add_field(name="Effect", value=(it.get("effect") or "—")[:1024], inline=False)
        embed.add_field(name="Duration", value=str(it.get("duration") or "—"), inline=False)
        if it.get("doc_url"):
            embed.add_field(name="Doc", value=f"[Open]({it['doc_url']})", inline=False)

        await interaction.followup.send(embed=embed, ephemeral=False)

    # ---------------- Buy (fallback) ----------------
    @item_group.command(name="buy", description="Buy an item for an OC using Tokens/Thral.")
    @app_commands.autocomplete(oc_name=oc_name_autocomplete_owner, item_name=item_name_autocomplete_for_sale)
    async def buy(self, interaction: discord.Interaction, oc_name: str, item_name: str, quantity: int = 1):
        await interaction.response.defer(ephemeral=False)

        if quantity <= 0:
            return await interaction.followup.send("Quantity must be > 0.", ephemeral=False)

        sb = get_supabase_client()

        try:
            oc = get_oc_by_owner_and_name_or_raise(interaction.user.id, oc_name)
            it = get_item_by_name(item_name)
            if not it:
                return await interaction.followup.send("Item not found (check exact name).", ephemeral=False)
            if not bool(it.get("for_sale", True)):
                return await interaction.followup.send("That item is not currently for sale.", ephemeral=False)
            if not bool(it.get("active", True)):
                return await interaction.followup.send("That item is inactive.", ephemeral=False)

            token_price = int(it.get("token_cost") or 0)
            thral_price = int(it.get("thral_cost") or 0)

            if token_price <= 0 and thral_price <= 0:
                return await interaction.followup.send(
                    "This item has no price set. Staff must set token_cost/thral_cost.",
                    ephemeral=False,
                )

            token_total = token_price * int(quantity)
            thral_total = thral_price * int(quantity)

            _charge_costs(sb, oc["oc_id"], token_total, thral_total)
            try:
                add_item_qty(oc["oc_id"], it["item_id"], quantity)
            except Exception:
                _refund_costs(sb, oc["oc_id"], token_total, thral_total)
                raise

            log_tx(
                interaction.user.id,
                oc["oc_id"],
                "BUY_ITEM",
                ap_delta=0,
                item_id=it["item_id"],
                quantity=quantity,
                notes=f"Bought {it.get('item_name')} | token={token_total} thral={thral_total}",
            )

        except Exception as e:
            return await interaction.followup.send(f"Error: {e}", ephemeral=False)

        price_out = []
        if token_total > 0:
            price_out.append(f"**{token_total}** {TOKEN_EMOJI}")
        if thral_total > 0:
            price_out.append(f"**{thral_total}** {THRAL_EMOJI}")

        await interaction.followup.send(
            f"Purchased **{quantity}x {it.get('item_name')}** for {' + '.join(price_out) if price_out else 'free'} for **{oc_name}**.",
            ephemeral=False,
        )

    # ---------------- Sell ----------------
    @item_group.command(name="sell", description="Sell an item back (defaults to 50% refund in the same currencies).")
    @app_commands.autocomplete(oc_name=oc_name_autocomplete_owner, item_name=item_name_autocomplete_all)
    async def sell(self, interaction: discord.Interaction, oc_name: str, item_name: str, quantity: int = 1):
        await interaction.response.defer(ephemeral=False)

        if quantity <= 0:
            return await interaction.followup.send("Quantity must be > 0.", ephemeral=False)

        sb = get_supabase_client()

        try:
            oc = get_oc_by_owner_and_name_or_raise(interaction.user.id, oc_name)
            it = get_item_by_name(item_name)
            if not it:
                return await interaction.followup.send("Item not found (check exact name).", ephemeral=False)

            have = get_item_qty(oc["oc_id"], it["item_id"])
            if have < quantity:
                return await interaction.followup.send(f"Not enough quantity. You have **{have}**.", ephemeral=False)

            token_price = int(it.get("token_cost") or 0)
            thral_price = int(it.get("thral_cost") or 0)

            token_refund = (token_price * quantity) // 2
            thral_refund = (thral_price * quantity) // 2

            add_item_qty(oc["oc_id"], it["item_id"], -quantity)

            if token_refund > 0:
                cur = _get_token_balance(sb, oc["oc_id"])
                _set_token_balance(sb, oc["oc_id"], cur + token_refund)

            if thral_refund > 0:
                cur = _get_thral_balance(sb, oc["oc_id"])
                _set_thral_balance(sb, oc["oc_id"], cur + thral_refund)

            log_tx(
                interaction.user.id,
                oc["oc_id"],
                "SELL_ITEM",
                ap_delta=0,
                item_id=it["item_id"],
                quantity=quantity,
                notes=f"Sold {it.get('item_name')} | token_refund={token_refund} thral_refund={thral_refund}",
            )

        except Exception as e:
            return await interaction.followup.send(f"Error: {e}", ephemeral=False)

        refund_parts = []
        if token_refund > 0:
            refund_parts.append(f"**{token_refund}** {TOKEN_EMOJI}")
        if thral_refund > 0:
            refund_parts.append(f"**{thral_refund}** {THRAL_EMOJI}")

        await interaction.followup.send(
            f"Sold **{quantity}x {it.get('item_name')}** for {' + '.join(refund_parts) if refund_parts else 'nothing'}.",
            ephemeral=False,
        )

    # ---------------- Use ----------------
    @item_group.command(name="use", description="Use an item (removes quantity and logs usage).")
    @app_commands.autocomplete(oc_name=oc_name_autocomplete_owner, item_name=item_name_autocomplete_all)
    async def use(self, interaction: discord.Interaction, oc_name: str, item_name: str, quantity: int = 1):
        await interaction.response.defer(ephemeral=False)

        if quantity <= 0:
            return await interaction.followup.send("Quantity must be > 0.", ephemeral=False)

        try:
            oc = get_oc_by_owner_and_name_or_raise(interaction.user.id, oc_name)
            it = get_item_by_name(item_name)
            if not it:
                return await interaction.followup.send("Item not found (check exact name).", ephemeral=False)

            have = get_item_qty(oc["oc_id"], it["item_id"])
            if have < quantity:
                return await interaction.followup.send(f"Not enough quantity. You have **{have}**.", ephemeral=False)

            add_item_qty(oc["oc_id"], it["item_id"], -quantity)
            log_tx(
                interaction.user.id,
                oc["oc_id"],
                "USE_ITEM",
                ap_delta=0,
                item_id=it["item_id"],
                quantity=quantity,
                notes=f"Used {it.get('item_name')}",
            )

        except Exception as e:
            return await interaction.followup.send(f"Error: {e}", ephemeral=False)

        await interaction.followup.send(f"Used **{quantity}x {it.get('item_name')}** on **{oc_name}**.", ephemeral=False)

    # ---------------- Give ----------------
    @item_group.command(name="give", description="Give an item from one of your OCs to another player's OC.")
    @app_commands.autocomplete(
        from_oc_name=oc_name_autocomplete_owner,
        to_oc_name=oc_name_autocomplete_target_player,
        item_name=item_name_autocomplete_all,
    )
    async def give(
        self,
        interaction: discord.Interaction,
        from_oc_name: str,
        to_player: discord.User,
        to_oc_name: str,
        item_name: str,
        quantity: int = 1,
    ):
        await interaction.response.defer(ephemeral=False)

        if quantity <= 0:
            return await interaction.followup.send("Quantity must be > 0.", ephemeral=False)

        try:
            from_oc = get_oc_by_owner_and_name_or_raise(interaction.user.id, from_oc_name)
            to_oc = get_oc_by_owner_and_name_or_raise(to_player.id, to_oc_name)
            it = get_item_by_name(item_name)
            if not it:
                return await interaction.followup.send("Item not found (check exact name).", ephemeral=False)

            have = get_item_qty(from_oc["oc_id"], it["item_id"])
            if have < quantity:
                return await interaction.followup.send(f"Not enough quantity. You have **{have}**.", ephemeral=False)

            add_item_qty(from_oc["oc_id"], it["item_id"], -quantity)
            add_item_qty(to_oc["oc_id"], it["item_id"], quantity)

            log_tx(
                interaction.user.id,
                from_oc["oc_id"],
                "GIVE_ITEM_OUT",
                item_id=it["item_id"],
                quantity=quantity,
                notes=f"Gave to {to_player.id}:{to_oc_name}",
            )
            log_tx(
                interaction.user.id,
                to_oc["oc_id"],
                "GIVE_ITEM_IN",
                item_id=it["item_id"],
                quantity=quantity,
                notes=f"Received from {interaction.user.id}:{from_oc_name}",
            )

        except Exception as e:
            return await interaction.followup.send(f"Error: {e}", ephemeral=False)

        await interaction.followup.send(
            f"Gave **{quantity}x {it.get('item_name')}** from **{from_oc_name}** → **{to_oc_name}**.",
            ephemeral=False,
        )

    # ---------------- Staff Admin ----------------
    async def _staff_only(self, interaction: discord.Interaction) -> bool:
        return isinstance(interaction.user, discord.Member) and is_staff(interaction.user)

    @item_group.command(name="create", description="(Staff) Create an item in the catalog.")
    async def create(
        self,
        interaction: discord.Interaction,
        item_name: str,
        token_cost: int = 0,
        thral_cost: int = 0,
        effect: str = "—",
        duration: Optional[str] = None,
        for_sale: Optional[bool] = True,
        active: Optional[bool] = True,
        doc_url: Optional[str] = None,
        item_key: Optional[str] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        if not await self._staff_only(interaction):
            return await interaction.followup.send("Staff only.", ephemeral=True)

        if token_cost < 0 or thral_cost < 0:
            return await interaction.followup.send("token_cost and thral_cost must be >= 0.", ephemeral=True)

        payload = {
            "item_name": item_name.strip(),
            "token_cost": int(token_cost),
            "thral_cost": int(thral_cost),
            "price_ap": int(token_cost),
            "effect": effect,
            "duration": duration,
            "for_sale": bool(for_sale),
            "active": bool(active),
            "doc_url": doc_url,
            "item_key": item_key,
        }

        sb = get_supabase_client()
        try:
            sb.table("item_catalog").insert(payload).execute()
        except Exception as e:
            return await interaction.followup.send(f"Create failed: {e}", ephemeral=True)

        await interaction.followup.send(f"Created item **{item_name}**.", ephemeral=True)

    @item_group.command(name="edit", description="(Staff) Edit an existing item.")
    @app_commands.autocomplete(item_name=item_name_autocomplete_all)
    async def edit(
        self,
        interaction: discord.Interaction,
        item_name: str,
        new_name: Optional[str] = None,
        token_cost: Optional[int] = None,
        thral_cost: Optional[int] = None,
        effect: Optional[str] = None,
        duration: Optional[str] = None,
        for_sale: Optional[bool] = None,
        active: Optional[bool] = None,
        doc_url: Optional[str] = None,
        item_key: Optional[str] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        if not await self._staff_only(interaction):
            return await interaction.followup.send("Staff only.", ephemeral=True)

        sb = get_supabase_client()
        existing = get_item_by_name(item_name)
        if not existing:
            return await interaction.followup.send("Item not found (check exact name).", ephemeral=True)

        update: dict = {}

        if new_name is not None:
            update["item_name"] = new_name.strip()
        if effect is not None:
            update["effect"] = effect.strip()
        if duration is not None:
            update["duration"] = duration.strip() if duration else None
        if for_sale is not None:
            update["for_sale"] = bool(for_sale)
        if active is not None:
            update["active"] = bool(active)
        if doc_url is not None:
            update["doc_url"] = doc_url.strip() if doc_url else None
        if item_key is not None:
            update["item_key"] = item_key.strip() if item_key else None

        if token_cost is not None:
            if token_cost < 0:
                return await interaction.followup.send("token_cost must be >= 0.", ephemeral=True)
            update["token_cost"] = int(token_cost)
            update["price_ap"] = int(token_cost)
        if thral_cost is not None:
            if thral_cost < 0:
                return await interaction.followup.send("thral_cost must be >= 0.", ephemeral=True)
            update["thral_cost"] = int(thral_cost)

        if not update:
            return await interaction.followup.send("Nothing to update.", ephemeral=True)

        try:
            sb.table("item_catalog").update(update).eq("item_id", existing["item_id"]).execute()
        except Exception as e:
            return await interaction.followup.send(f"Edit failed: {e}", ephemeral=True)

        await interaction.followup.send(f"Updated item **{item_name}**.", ephemeral=True)

    @item_group.command(name="delete", description="(Staff) Delete an item from the catalog (confirm required).")
    @app_commands.autocomplete(item_name=item_name_autocomplete_all)
    async def delete(self, interaction: discord.Interaction, item_name: str):
        await interaction.response.defer(ephemeral=True)
        if not await self._staff_only(interaction):
            return await interaction.followup.send("Staff only.", ephemeral=True)

        existing = get_item_by_name(item_name)
        if not existing:
            return await interaction.followup.send("Item not found (check exact name).", ephemeral=True)

        view = ConfirmDeleteView(requester_id=interaction.user.id)
        msg = await interaction.followup.send(
            f"Delete item **{existing.get('item_name')}**? This removes it from the catalog (does not remove from OCs).",
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if view.value is not True:
            return await msg.edit(content="Cancelled.", view=None)

        sb = get_supabase_client()
        try:
            sb.table("item_catalog").delete().eq("item_id", existing["item_id"]).execute()
        except Exception as e:
            return await msg.edit(content=f"Delete failed: {e}", view=None)

        await msg.edit(content=f"Deleted **{existing.get('item_name')}**.", view=None)


async def setup(bot: commands.Bot):
    cog = Items(bot)
    bot.tree.add_command(Items.item_group, guild=SKYFALL_GUILD)
    await bot.add_cog(cog)
