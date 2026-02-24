from __future__ import annotations

import math
import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional, List

from config.skyfall import SKYFALL_GUILD, AP_EMOJI
from services.permissions import is_staff
from services.oc_service import get_oc_by_owner_and_name_or_raise
from services.ap_service import get_ap, add_ap
from services.log_service import log_tx
from services.store_service import list_skills_for_sale, get_skill_by_name
from services.db import get_supabase_client

from ui.confirm import ConfirmDeleteView

# Discord limits
MAX_SELECT_OPTIONS = 25  # select menus cap


# ---------------- Slash autocomplete helpers ----------------
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


async def skill_name_autocomplete_for_sale(interaction: discord.Interaction, current: str):
    sb = get_supabase_client()
    cur = (current or "").strip()
    try:
        q = sb.table("skill_catalog").select("skill_name").eq("for_sale", True).eq("active", True)
        if cur:
            q = q.ilike("skill_name", f"%{cur}%")
        res = q.order("skill_name").limit(25).execute()
        rows = getattr(res, "data", None) or []
        return [app_commands.Choice(name=r["skill_name"], value=r["skill_name"]) for r in rows if r.get("skill_name")]
    except Exception:
        return []


async def skill_name_autocomplete_all(interaction: discord.Interaction, current: str):
    sb = get_supabase_client()
    cur = (current or "").strip()
    try:
        q = sb.table("skill_catalog").select("skill_name")
        if cur:
            q = q.ilike("skill_name", f"%{cur}%")
        res = q.order("skill_name").limit(25).execute()
        rows = getattr(res, "data", None) or []
        return [app_commands.Choice(name=r["skill_name"], value=r["skill_name"]) for r in rows if r.get("skill_name")]
    except Exception:
        return []


# ---------------- UI helpers ----------------
def _format_cost(skill: dict) -> str:
    cost = int(skill.get("cost_ap") or 0)
    return f"{cost} {AP_EMOJI}" if cost > 0 else "Free"


def _bucket_skills(skills: list[dict]) -> dict[str, list[dict]]:
    paid = [s for s in skills if int(s.get("cost_ap") or 0) > 0]
    free = [s for s in skills if int(s.get("cost_ap") or 0) == 0]

    def _sort_key(s: dict):
        return (int(s.get("cost_ap") or 0), (s.get("skill_name") or "").lower())

    paid.sort(key=_sort_key)
    free.sort(key=_sort_key)
    all_sorted = sorted(skills, key=_sort_key)

    return {"all": all_sorted, "paid": paid, "free": free}


def _category_suffix(key: str) -> str:
    return {
        "all": "",
        "paid": f" • {AP_EMOJI} Paid Skills",
        "free": " • 🆓 Free Skills",
    }.get(key, "")


def _build_store_embed_paged(
    skills: list[dict],
    title_suffix: str = "",
    page: int = 0,
    per_page: int = 8,
) -> discord.Embed:
    """
    Paged store embed that avoids the 25-field limit by rendering into description.
    """
    embed = discord.Embed(
        title=f"{AP_EMOJI} Skill Store{title_suffix}",
        description=(
            "Use the dropdowns below to pick an OC + skill, then press **Buy**.\n"
            "(You can also use `/skill buy` anytime.)"
        ),
        color=discord.Color.gold(),
    )

    total = len(skills)
    if total == 0:
        embed.description += "\n\n_No skills found._"
        embed.set_footer(text="Page 1/1")
        return embed

    pages = max(1, math.ceil(total / max(1, per_page)))
    page = max(0, min(page, pages - 1))

    start = page * per_page
    end = start + per_page
    chunk = skills[start:end]

    blocks: List[str] = []
    for s in chunk:
        name = s.get("skill_name") or "Unknown Skill"
        cost = int(s.get("cost_ap") or 0)
        desc = (s.get("description") or "—").strip()
        if len(desc) > 220:
            desc = desc[:220] + "…"

        line = f"**{name}** — **{cost}** {AP_EMOJI}\n{desc}"
        if s.get("doc_url"):
            line += f"\n[Doc]({s['doc_url']})"
        blocks.append(line)

    text = "\n\n".join(blocks)
    if len(text) > 3900:
        text = text[:3900] + "\n\n…"

    embed.description += "\n\n" + text
    embed.set_footer(text=f"Page {page+1}/{pages} • Showing {start+1}-{min(end, total)} of {total}")
    return embed


def _slice_for_select(skills: list[dict], page: int, per_page: int = MAX_SELECT_OPTIONS) -> list[dict]:
    """
    Slice for the dropdown (max 25 options).
    We will drive this with the SAME page buttons as the embed, so there is only ONE pager.
    """
    total = len(skills)
    pages = max(1, math.ceil(total / max(1, per_page)))
    page = max(0, min(page, pages - 1))
    start = page * per_page
    end = start + per_page
    return skills[start:end]


async def _has_skill(sb, oc_id: str, skill_id: str) -> bool:
    res = sb.table("oc_skills").select("skill_id").eq("oc_id", oc_id).eq("skill_id", skill_id).limit(1).execute()
    rows = getattr(res, "data", None) or []
    return bool(rows)


async def _grant_skill_row(sb, oc_id: str, skill_id: str) -> None:
    sb.table("oc_skills").insert({"oc_id": oc_id, "skill_id": skill_id}).execute()


# =========================
# Interactive Skill Store UI (Paged, ONE pager)
# =========================
class SkillBuyConfirmModal(discord.ui.Modal, title="Confirm Skill Purchase"):
    confirm = discord.ui.TextInput(
        label="Type YES to confirm",
        placeholder="YES",
        default="YES",
        required=True,
        max_length=8,
    )

    def __init__(self, view: "SkillStoreView"):
        super().__init__()
        self.store_view = view

    async def on_submit(self, interaction: discord.Interaction):
        v = self.store_view
        if interaction.user.id != v.owner_id:
            return await interaction.response.send_message("This menu isn’t for you.", ephemeral=True)

        if str(self.confirm.value).strip().upper() != "YES":
            return await interaction.response.send_message("Cancelled (didn’t type YES).", ephemeral=True)

        if not v.selected_oc_id or not v.selected_skill_id:
            return await interaction.response.send_message("Select an OC and a skill first.", ephemeral=True)

        sb = get_supabase_client()

        # Fetch skill fresh (source of truth)
        try:
            res = (
                sb.table("skill_catalog")
                .select("skill_id, skill_name, description, cost_ap, active, doc_url, for_sale")
                .eq("skill_id", v.selected_skill_id)
                .limit(1)
                .execute()
            )
            rows = getattr(res, "data", None) or []
            skill = rows[0] if rows else None
        except Exception:
            skill = None

        if not skill:
            return await interaction.response.send_message("That skill no longer exists.", ephemeral=True)
        if not bool(skill.get("for_sale", True)) or not bool(skill.get("active", True)):
            return await interaction.response.send_message("That skill is not available for purchase.", ephemeral=True)

        if await _has_skill(sb, v.selected_oc_id, skill["skill_id"]):
            return await interaction.response.send_message("That OC already has this skill.", ephemeral=True)

        cost = int(skill.get("cost_ap") or 0)
        try:
            cur_ap = int(get_ap(v.selected_oc_id) or 0)
            if cost > 0 and cur_ap < cost:
                return await interaction.response.send_message(
                    f"Not enough AP. Need **{cost}** {AP_EMOJI}, you have **{cur_ap}** {AP_EMOJI}.",
                    ephemeral=True,
                )

            # Charge then grant; refund on failure
            if cost > 0:
                add_ap(v.selected_oc_id, -cost)

            try:
                await _grant_skill_row(sb, v.selected_oc_id, skill["skill_id"])
            except Exception:
                if cost > 0:
                    add_ap(v.selected_oc_id, cost)
                raise

            log_tx(
                interaction.user.id,
                v.selected_oc_id,
                "BUY_SKILL",
                ap_delta=-cost,
                notes=f"Bought {skill.get('skill_name')} ({skill.get('skill_id')})",
            )

        except Exception as e:
            return await interaction.response.send_message(f"Error: {e}", ephemeral=True)

        await interaction.response.send_message(
            f"Purchased **{skill.get('skill_name')}** for **{_format_cost(skill)}** on **{v.selected_oc_name}**.",
            ephemeral=True,
        )


class SkillCategorySelect(discord.ui.Select):
    def __init__(self, view: "SkillStoreView"):
        self.store_view = view
        options = [
            discord.SelectOption(label="All Skills", value="all", description="Everything for sale"),
            discord.SelectOption(label="Paid Skills", value="paid", description="Costs AP"),
            discord.SelectOption(label="Free Skills", value="free", description="0 cost skills"),
        ]
        super().__init__(placeholder="Filter store…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        v = self.store_view
        if interaction.user.id != v.owner_id:
            return await interaction.response.send_message("This menu isn’t for you.", ephemeral=True)

        v.category_key = self.values[0]

        # reset selection + page on filter change
        v.embed_page = 0
        v.selected_skill_id = None
        v.selected_skill_name = None
        v.buy_button.disabled = True

        for opt in self.options:
            opt.default = (opt.value == v.category_key)

        bucket = v.current_bucket()

        # dropdown follows embed page (ONE pager)
        v.skill_select.refresh_options(_slice_for_select(bucket, v.embed_page), selected_skill_id=None)

        v._sync_nav_buttons()
        embed = _build_store_embed_paged(bucket, _category_suffix(v.category_key), page=v.embed_page, per_page=v.embed_per_page)
        await interaction.response.edit_message(embed=embed, view=v)


class SkillOCSelect(discord.ui.Select):
    def __init__(self, view: "SkillStoreView", oc_rows: list[dict]):
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

        for opt in self.options:
            opt.default = (opt.value == oc_id)

        v.buy_button.disabled = not (v.selected_oc_id and v.selected_skill_id)
        await interaction.response.edit_message(view=v)


class SkillSelect(discord.ui.Select):
    def __init__(self, view: "SkillStoreView", skills: list[dict]):
        self.store_view = view
        super().__init__(placeholder="Select a skill…", min_values=1, max_values=1, options=[])
        self.refresh_options(skills, selected_skill_id=None)

    def refresh_options(self, skills: list[dict], selected_skill_id: Optional[str]):
        options: list[discord.SelectOption] = []
        for s in skills[:MAX_SELECT_OPTIONS]:
            sid = s.get("skill_id")
            name = s.get("skill_name")
            if not sid or not name:
                continue
            opt = discord.SelectOption(label=str(name), value=str(sid), description=str(_format_cost(s))[:100])
            opt.default = (selected_skill_id is not None and str(sid) == str(selected_skill_id))
            options.append(opt)

        if not options:
            self.options = [discord.SelectOption(label="No skills on this page", value="none")]
            self.disabled = True
        else:
            self.options = options
            self.disabled = False

    async def callback(self, interaction: discord.Interaction):
        v = self.store_view
        if interaction.user.id != v.owner_id:
            return await interaction.response.send_message("This menu isn’t for you.", ephemeral=True)

        sid = self.values[0]
        if sid == "none":
            v.selected_skill_id = None
            v.selected_skill_name = None
        else:
            v.selected_skill_id = sid
            v.selected_skill_name = next((opt.label for opt in self.options if opt.value == sid), None)

        for opt in self.options:
            opt.default = (opt.value == sid)

        v.buy_button.disabled = not (v.selected_oc_id and v.selected_skill_id)
        await interaction.response.edit_message(view=v)


class SkillBuyButton(discord.ui.Button):
    def __init__(self, view: "SkillStoreView"):
        super().__init__(label="Buy", style=discord.ButtonStyle.success, disabled=True)
        self.store_view = view

    async def callback(self, interaction: discord.Interaction):
        v = self.store_view
        if interaction.user.id != v.owner_id:
            return await interaction.response.send_message("This button isn’t for you.", ephemeral=True)

        if not v.selected_oc_id:
            return await interaction.response.send_message("Pick an OC first.", ephemeral=True)
        if not v.selected_skill_id:
            return await interaction.response.send_message("Pick a skill first.", ephemeral=True)

        await interaction.response.send_modal(SkillBuyConfirmModal(v))


class EmbedNavButton(discord.ui.Button):
    def __init__(self, view: "SkillStoreView", direction: str):
        label = "◀ Page" if direction == "prev" else "Page ▶"
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self.store_view = view
        self.direction = direction

    async def callback(self, interaction: discord.Interaction):
        v = self.store_view
        if interaction.user.id != v.owner_id:
            return await interaction.response.send_message("This menu isn’t for you.", ephemeral=True)

        bucket = v.current_bucket()
        pages = v.embed_pages()

        if self.direction == "prev":
            v.embed_page = max(0, v.embed_page - 1)
        else:
            v.embed_page = min(pages - 1, v.embed_page + 1)

        # ONE pager: dropdown follows the embed page
        v.selected_skill_id = None
        v.selected_skill_name = None
        v.buy_button.disabled = True
        v.skill_select.refresh_options(_slice_for_select(bucket, v.embed_page), selected_skill_id=None)

        v._sync_nav_buttons()
        embed = _build_store_embed_paged(bucket, _category_suffix(v.category_key), page=v.embed_page, per_page=v.embed_per_page)
        await interaction.response.edit_message(embed=embed, view=v)


class SkillStoreView(discord.ui.View):
    def __init__(
        self,
        owner_id: int,
        buckets: dict[str, list[dict]],
        oc_rows: list[dict],
        embed_per_page: int = 8,
    ):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.buckets = buckets

        self.category_key: str = "all"
        self.selected_oc_id: Optional[str] = None
        self.selected_oc_name: Optional[str] = None
        self.selected_skill_id: Optional[str] = None
        self.selected_skill_name: Optional[str] = None

        # ONE pager:
        # - embed_page controls the embed display
        # - dropdown slice follows embed_page (25 max)
        self.embed_per_page = embed_per_page
        self.embed_page = 0

        self.category_select = SkillCategorySelect(self)
        self.oc_select = SkillOCSelect(self, oc_rows)
        self.skill_select = SkillSelect(self, _slice_for_select(self.current_bucket(), self.embed_page))
        self.buy_button = SkillBuyButton(self)

        self.prev_embed_button = EmbedNavButton(self, "prev")
        self.next_embed_button = EmbedNavButton(self, "next")

        for opt in self.category_select.options:
            opt.default = (opt.value == "all")

        self.add_item(self.category_select)
        self.add_item(self.oc_select)
        self.add_item(self.skill_select)
        self.add_item(self.buy_button)

        # navigation row (ONLY pages)
        self.add_item(self.prev_embed_button)
        self.add_item(self.next_embed_button)

        self._sync_nav_buttons()

    def current_bucket(self) -> list[dict]:
        return self.buckets.get(self.category_key, [])

    def embed_pages(self) -> int:
        total = len(self.current_bucket())
        return max(1, math.ceil(total / max(1, self.embed_per_page)))

    def _sync_nav_buttons(self):
        # bounds check
        self.embed_page = max(0, min(self.embed_page, self.embed_pages() - 1))

        self.prev_embed_button.disabled = (self.embed_page <= 0)
        self.next_embed_button.disabled = (self.embed_page >= self.embed_pages() - 1)

        # Informative labels
        self.prev_embed_button.label = f"◀ Page ({self.embed_page+1}/{self.embed_pages()})"
        self.next_embed_button.label = f"Page ▶ ({self.embed_page+1}/{self.embed_pages()})"

    async def on_timeout(self):
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True


# =========================
# Cog
# =========================
class Skills(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    skill_group = app_commands.Group(name="skill", description="Skill commands")

    async def _staff_only(self, interaction: discord.Interaction) -> bool:
        return isinstance(interaction.user, discord.Member) and is_staff(interaction.user)

    # ---------------- Store (Interactive) ----------------
    @skill_group.command(name="store", description="View skills currently for sale.")
    async def store(self, interaction: discord.Interaction, public: Optional[bool] = True):
        ephemeral = not bool(public)
        await interaction.response.defer(ephemeral=ephemeral)

        skills = list_skills_for_sale()
        if not skills:
            return await interaction.followup.send("No skills are currently for sale.", ephemeral=ephemeral)

        buckets = _bucket_skills(skills)

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
        view = SkillStoreView(owner_id=interaction.user.id, buckets=buckets, oc_rows=oc_rows, embed_per_page=8)

        if not oc_rows:
            embed.set_footer(text="You have no registered OCs. Use /oc_register first.")

        await interaction.followup.send(embed=embed, view=view, ephemeral=ephemeral)

    # ---------------- Buy (Fallback Slash) ----------------
    @skill_group.command(name="buy", description="Buy a skill for one of your OCs (fallback).")
    @app_commands.autocomplete(oc_name=oc_name_autocomplete_owner, skill_name=skill_name_autocomplete_for_sale)
    async def buy(self, interaction: discord.Interaction, oc_name: str, skill_name: str, public: Optional[bool] = True):
        ephemeral = not bool(public)
        await interaction.response.defer(ephemeral=ephemeral)

        sb = get_supabase_client()
        try:
            oc = get_oc_by_owner_and_name_or_raise(interaction.user.id, oc_name)

            skill = get_skill_by_name(skill_name)
            if not skill:
                return await interaction.followup.send("Skill not found (check exact name).", ephemeral=ephemeral)
            if not bool(skill.get("for_sale", True)) or not bool(skill.get("active", True)):
                return await interaction.followup.send("That skill is not for sale / inactive.", ephemeral=ephemeral)

            if await _has_skill(sb, oc["oc_id"], skill["skill_id"]):
                return await interaction.followup.send("That OC already has this skill.", ephemeral=ephemeral)

            cost = int(skill.get("cost_ap") or 0)
            cur_ap = int(get_ap(oc["oc_id"]) or 0)
            if cost > 0 and cur_ap < cost:
                return await interaction.followup.send(
                    f"Not enough AP. Need **{cost}** {AP_EMOJI}, you have **{cur_ap}** {AP_EMOJI}.",
                    ephemeral=ephemeral,
                )

            if cost > 0:
                add_ap(oc["oc_id"], -cost)

            try:
                await _grant_skill_row(sb, oc["oc_id"], skill["skill_id"])
            except Exception:
                if cost > 0:
                    add_ap(oc["oc_id"], cost)
                raise

            log_tx(
                interaction.user.id,
                oc["oc_id"],
                "BUY_SKILL",
                ap_delta=-cost,
                notes=f"Bought {skill.get('skill_name')} ({skill.get('skill_id')})",
            )

        except Exception as e:
            return await interaction.followup.send(f"Error: {e}", ephemeral=ephemeral)

        await interaction.followup.send(
            f"Purchased **{skill.get('skill_name')}** for **{_format_cost(skill)}** on **{oc_name}**.",
            ephemeral=ephemeral,
        )

    # ---------------- Staff grant ----------------
    @skill_group.command(name="grant", description="(Staff) Grant a skill to an OC.")
    @app_commands.autocomplete(skill_name=skill_name_autocomplete_for_sale)
    async def grant(
        self,
        interaction: discord.Interaction,
        player: discord.User,
        oc_name: str,
        skill_name: str,
        reason: Optional[str] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        if not await self._staff_only(interaction):
            return await interaction.followup.send("Staff only.", ephemeral=True)

        sb = get_supabase_client()
        try:
            oc = get_oc_by_owner_and_name_or_raise(player.id, oc_name)
            skill = get_skill_by_name(skill_name)
            if not skill:
                return await interaction.followup.send("Skill not found.", ephemeral=True)

            if await _has_skill(sb, oc["oc_id"], skill["skill_id"]):
                return await interaction.followup.send("That OC already has this skill.", ephemeral=True)

            await _grant_skill_row(sb, oc["oc_id"], skill["skill_id"])

            log_tx(
                interaction.user.id,
                oc["oc_id"],
                "SKILL_GRANT",
                ap_delta=0,
                notes=f"Granted {skill.get('skill_name')} | {reason or ''}".strip(),
            )
        except Exception as e:
            return await interaction.followup.send(f"Error: {e}", ephemeral=True)

        await interaction.followup.send(f"Granted **{skill_name}** to **{oc_name}**.", ephemeral=True)

    # =========================
    # Staff Admin: Create / Edit / Delete
    # =========================
    @skill_group.command(name="create", description="(Staff) Create a skill in the catalog.")
    async def create(
        self,
        interaction: discord.Interaction,
        skill_name: str,
        cost_ap: int = 0,
        description: str = "—",
        for_sale: Optional[bool] = True,
        active: Optional[bool] = True,
        doc_url: Optional[str] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        if not await self._staff_only(interaction):
            return await interaction.followup.send("Staff only.", ephemeral=True)

        skill_name = (skill_name or "").strip()
        if not skill_name:
            return await interaction.followup.send("Skill name cannot be empty.", ephemeral=True)
        if cost_ap < 0:
            return await interaction.followup.send("cost_ap must be >= 0.", ephemeral=True)

        existing = get_skill_by_name(skill_name)
        if existing:
            return await interaction.followup.send("A skill with that name already exists.", ephemeral=True)

        payload = {
            "skill_name": skill_name,
            "cost_ap": int(cost_ap),
            "description": description or "",
            "for_sale": bool(for_sale),
            "active": bool(active),
            "doc_url": (doc_url.strip() if doc_url else None),
        }

        sb = get_supabase_client()
        try:
            sb.table("skill_catalog").insert(payload).execute()
        except Exception as e:
            return await interaction.followup.send(f"Create failed: {e}", ephemeral=True)

        await interaction.followup.send(f"Created skill **{skill_name}**.", ephemeral=True)

    @skill_group.command(name="edit", description="(Staff) Edit an existing skill.")
    @app_commands.autocomplete(skill_name=skill_name_autocomplete_all)
    async def edit(
        self,
        interaction: discord.Interaction,
        skill_name: str,
        new_name: Optional[str] = None,
        cost_ap: Optional[int] = None,
        description: Optional[str] = None,
        for_sale: Optional[bool] = None,
        active: Optional[bool] = None,
        doc_url: Optional[str] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        if not await self._staff_only(interaction):
            return await interaction.followup.send("Staff only.", ephemeral=True)

        existing = get_skill_by_name(skill_name)
        if not existing:
            return await interaction.followup.send("Skill not found (check exact name).", ephemeral=True)

        update: dict = {}

        if new_name is not None:
            nn = new_name.strip()
            if not nn:
                return await interaction.followup.send("new_name cannot be empty.", ephemeral=True)

            clash = get_skill_by_name(nn)
            if clash and clash.get("skill_id") != existing.get("skill_id"):
                return await interaction.followup.send("Another skill already uses that name.", ephemeral=True)
            update["skill_name"] = nn

        if cost_ap is not None:
            if cost_ap < 0:
                return await interaction.followup.send("cost_ap must be >= 0.", ephemeral=True)
            update["cost_ap"] = int(cost_ap)

        if description is not None:
            update["description"] = description

        if for_sale is not None:
            update["for_sale"] = bool(for_sale)

        if active is not None:
            update["active"] = bool(active)

        if doc_url is not None:
            update["doc_url"] = doc_url.strip() if doc_url else None

        if not update:
            return await interaction.followup.send("Nothing to update.", ephemeral=True)

        sb = get_supabase_client()
        try:
            sb.table("skill_catalog").update(update).eq("skill_id", existing["skill_id"]).execute()
        except Exception as e:
            return await interaction.followup.send(f"Edit failed: {e}", ephemeral=True)

        await interaction.followup.send(f"Updated skill **{skill_name}**.", ephemeral=True)

    @skill_group.command(name="delete", description="(Staff) Delete a skill from the catalog (confirm required).")
    @app_commands.autocomplete(skill_name=skill_name_autocomplete_all)
    async def delete(self, interaction: discord.Interaction, skill_name: str):
        await interaction.response.defer(ephemeral=True)
        if not await self._staff_only(interaction):
            return await interaction.followup.send("Staff only.", ephemeral=True)

        existing = get_skill_by_name(skill_name)
        if not existing:
            return await interaction.followup.send("Skill not found (check exact name).", ephemeral=True)

        view = ConfirmDeleteView(requester_id=interaction.user.id)
        msg = await interaction.followup.send(
            f"Delete skill **{existing.get('skill_name')}**? This removes it from the catalog (does not remove from OCs who already have it).",
            view=view,
            ephemeral=True,
        )
        await view.wait()

        if view.value is not True:
            try:
                return await msg.edit(content="Cancelled.", view=None)
            except Exception:
                return

        sb = get_supabase_client()
        try:
            sb.table("skill_catalog").delete().eq("skill_id", existing["skill_id"]).execute()
        except Exception as e:
            return await msg.edit(content=f"Delete failed: {e}", view=None)

        await msg.edit(content=f"Deleted **{existing.get('skill_name')}**.", view=None)


async def setup(bot: commands.Bot):
    cog = Skills(bot)
    bot.tree.add_command(Skills.skill_group, guild=SKYFALL_GUILD)
    await bot.add_cog(cog)
