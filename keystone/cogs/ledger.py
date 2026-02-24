# cogs/ledger.py
from __future__ import annotations

from typing import Dict, List, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from config.skyfall import SKYFALL_GUILD
from services.db import get_supabase_client
from services.ap_service import get_ap

# ── Emojis ──────────────────────────────────────────────────────────────
THRAL_EMOJI = "<:thral:1388999536143499477>"
TOKEN_EMOJI = "<:token:1447676379536691201>"
AP_EMOJI = "<:Crystal_1:1379215625121042563>"  # ✅ updated

PAGE_SIZE = 12
LEADERBOARD_LIMIT = 25

# Staff role that can view staff-only ledger categories
STATS_STAFF_ROLE_ID = 1374730886490357822

# If you want loans/fines visible to everyone:
ALLOW_PUBLIC_DEBT_LEDGERS = False


def _is_staff_role(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    return any(r.id == STATS_STAFF_ROLE_ID for r in interaction.user.roles)


def currency_meta(key: str) -> Tuple[str, str]:
    return {
        "thral": ("Thral Ledger", THRAL_EMOJI),
        "tokens": ("Token Ledger", TOKEN_EMOJI),
        "ap": ("AP Ledger", AP_EMOJI),
        "loans": ("Loans Outstanding", "📄"),
        "fines": ("Fines Outstanding", "⚖️"),
    }.get(key, ("Ledger", "👁️"))


def chunk(items: list, size: int):
    return [items[i:i + size] for i in range(0, len(items), size)]


def _safe_int(x) -> int:
    try:
        return int(x or 0)
    except Exception:
        return 0


class LedgerState:
    def __init__(self, currency: str, sort: str, mode: str, page: int = 0):
        self.currency = currency      # thral/tokens/ap/loans/fines
        self.sort = sort              # asc/desc
        self.mode = mode              # leaderboard/full
        self.page = page              # 0-based


# ── Data building (matches oc_overview wallet logic) ─────────────────────
async def build_ledger_rows(sb, currency: str) -> List[Tuple[str, int]]:
    """
    Returns list of (owner_discord_id, amount) aggregated across all OCs.
    """
    oc_res = sb.table("ocs").select("oc_id, owner_discord_id").execute()
    oc_rows = getattr(oc_res, "data", None) or []

    oc_to_owner: Dict[str, str] = {}
    owners: Dict[str, int] = {}  # owner_id -> total

    for r in oc_rows:
        oc_id = r.get("oc_id")
        owner = str(r.get("owner_discord_id") or "").strip()
        if not oc_id or not owner:
            continue
        oc_to_owner[str(oc_id)] = owner
        owners.setdefault(owner, 0)

    if not oc_to_owner:
        return []

    if currency == "tokens":
        res = sb.table("token_wallets").select("oc_id, balance").execute()
        for r in getattr(res, "data", None) or []:
            oc_id = str(r.get("oc_id") or "")
            owner = oc_to_owner.get(oc_id)
            if owner:
                owners[owner] += _safe_int(r.get("balance"))
        return list(owners.items())

    if currency == "thral":
        res = sb.table("thral_wallets").select("oc_id, balance").execute()
        for r in getattr(res, "data", None) or []:
            oc_id = str(r.get("oc_id") or "")
            owner = oc_to_owner.get(oc_id)
            if owner:
                owners[owner] += _safe_int(r.get("balance"))
        return list(owners.items())

    if currency == "loans":
        res = (
            sb.table("thral_loans")
            .select("oc_id, remaining_balance, status")
            .eq("status", "active")
            .execute()
        )
        for r in getattr(res, "data", None) or []:
            oc_id = str(r.get("oc_id") or "")
            owner = oc_to_owner.get(oc_id)
            if owner:
                owners[owner] += _safe_int(r.get("remaining_balance"))
        return list(owners.items())

    if currency == "fines":
        # Not implemented yet (no schema provided).
        # If you add a fines table later, we’ll wire it up here.
        return list(owners.items())

    if currency == "ap":
        for oc_id, owner in oc_to_owner.items():
            try:
                owners[owner] += _safe_int(get_ap(oc_id))
            except Exception:
                pass
        return list(owners.items())

    return []


async def build_pages(guild: discord.Guild, state: LedgerState) -> List[discord.Embed]:
    sb = get_supabase_client()

    rows = await build_ledger_rows(sb, state.currency)
    rows.sort(key=lambda x: x[1], reverse=(state.sort == "desc"))

    if state.mode == "leaderboard":
        rows = rows[:LEADERBOARD_LIMIT]

    title, emoji = currency_meta(state.currency)

    if not rows:
        embed = discord.Embed(
            title=f"{emoji} {title}",
            description="No data found.",
            color=0xF39C12
        )
        embed.set_footer(text=f"{state.mode} • {state.sort}")
        return [embed]

    pages: List[discord.Embed] = []
    parts = chunk(rows, PAGE_SIZE)

    for page_i, part in enumerate(parts, start=1):
        lines = []
        for i, (owner_id, amt) in enumerate(part):
            rank = (page_i - 1) * PAGE_SIZE + i + 1
            member = guild.get_member(int(owner_id)) if owner_id.isdigit() else None
            name = member.mention if member else f"`{owner_id}`"
            lines.append(f"**{rank:>2}.** {name} — **{amt:,}**")

        embed = discord.Embed(
            title=f"{emoji} {title}",
            description="\n".join(lines),
            color=0xF39C12
        )
        embed.set_footer(text=f"Page {page_i}/{len(parts)} • {state.mode} • {state.sort}")
        pages.append(embed)

    return pages


# ── UI ──────────────────────────────────────────────────────────────────
class CurrencySelect(discord.ui.Select):
    def __init__(self, view: "LedgerView"):
        super().__init__(
            placeholder="Switch currency…",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Thral", value="thral", emoji=THRAL_EMOJI),
                discord.SelectOption(label="Tokens", value="tokens", emoji=TOKEN_EMOJI),
                discord.SelectOption(label="AP", value="ap", emoji=AP_EMOJI),
                discord.SelectOption(label="Loans", value="loans", emoji="📄"),
                discord.SelectOption(label="Fines", value="fines", emoji="⚖️"),
            ],
            row=0
        )
        self.view_ref = view

    async def callback(self, interaction: discord.Interaction):
        state = self.view_ref.state
        state.currency = self.values[0]
        state.page = 0

        if state.currency in ("loans", "fines") and not ALLOW_PUBLIC_DEBT_LEDGERS:
            if not _is_staff_role(interaction):
                await interaction.response.send_message("Loans/Fines ledgers are staff-only.", ephemeral=True)
                return

        await self.view_ref.refresh(interaction)


class LedgerView(discord.ui.View):
    def __init__(self, guild: discord.Guild, author_id: int, state: LedgerState):
        super().__init__(timeout=240)
        self.guild = guild
        self.author_id = author_id
        self.state = state
        self.add_item(CurrencySelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    async def refresh(self, interaction: discord.Interaction):
        pages = await build_pages(self.guild, self.state)
        self.state.page = max(0, min(self.state.page, len(pages) - 1))
        self._sync(pages_len=len(pages))
        await interaction.response.edit_message(embed=pages[self.state.page], view=self)

    def _sync(self, pages_len: int):
        self.prev_btn.disabled = (self.state.page <= 0)
        self.next_btn.disabled = (self.state.page >= pages_len - 1)
        self.sort_btn.label = "Sort: High→Low" if self.state.sort == "desc" else "Sort: Low→High"
        self.mode_btn.label = "Mode: Top 25" if self.state.mode == "leaderboard" else "Mode: Full"

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary, row=1)
    async def prev_btn(self, interaction: discord.Interaction, _):
        self.state.page -= 1
        await self.refresh(interaction)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, row=1)
    async def next_btn(self, interaction: discord.Interaction, _):
        self.state.page += 1
        await self.refresh(interaction)

    @discord.ui.button(label="Sort: High→Low", style=discord.ButtonStyle.primary, row=1)
    async def sort_btn(self, interaction: discord.Interaction, _):
        self.state.sort = "asc" if self.state.sort == "desc" else "desc"
        self.state.page = 0
        await self.refresh(interaction)

    @discord.ui.button(label="Mode: Top 25", style=discord.ButtonStyle.success, row=1)
    async def mode_btn(self, interaction: discord.Interaction, _):
        self.state.mode = "full" if self.state.mode == "leaderboard" else "leaderboard"
        self.state.page = 0
        await self.refresh(interaction)


# ── Command ──────────────────────────────────────────────────────────────
class LedgerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ledger", description="All-seeing ledger view (filter + sort + mode + paging).")
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.choices(
        currency=[
            app_commands.Choice(name="Thral", value="thral"),
            app_commands.Choice(name="Tokens", value="tokens"),
            app_commands.Choice(name="AP", value="ap"),
            app_commands.Choice(name="Loans", value="loans"),
            app_commands.Choice(name="Fines", value="fines"),
        ],
        sort=[
            app_commands.Choice(name="Highest → Lowest", value="desc"),
            app_commands.Choice(name="Lowest → Highest", value="asc"),
        ],
        mode=[
            app_commands.Choice(name="Leaderboard (Top 25)", value="leaderboard"),
            app_commands.Choice(name="Full", value="full"),
        ],
    )
    async def ledger(
        self,
        interaction: discord.Interaction,
        currency: app_commands.Choice[str],
        sort: app_commands.Choice[str],
        mode: app_commands.Choice[str],
    ):
        cur = currency.value
        direction = sort.value
        ledger_mode = mode.value

        if cur in ("loans", "fines") and not ALLOW_PUBLIC_DEBT_LEDGERS and not _is_staff_role(interaction):
            await interaction.response.send_message("Loans/Fines ledgers are staff-only.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("Guild not found.", ephemeral=True)
            return

        state = LedgerState(currency=cur, sort=direction, mode=ledger_mode, page=0)
        pages = await build_pages(guild, state)

        view = LedgerView(guild, interaction.user.id, state)
        view._sync(pages_len=len(pages))

        await interaction.followup.send(embed=pages[0], view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(LedgerCog(bot))
