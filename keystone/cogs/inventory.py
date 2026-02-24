import math
import discord
from typing import Optional, List, Dict
from discord import app_commands
from discord.ext import commands

from config.skyfall import SKYFALL_GUILD
from services.db import get_supabase_client
from services.oc_service import get_oc_by_owner_and_name_or_raise


# ---------------- OC autocomplete helpers ----------------
async def oc_name_autocomplete_inventory(interaction: discord.Interaction, current: str):
    """
    Autocomplete OC names for /inventory.
    - If the user has chosen `player`, only show that player's OCs.
    - Otherwise show all OCs (so you can look up anyone).
    """
    sb = get_supabase_client()
    cur = (current or "").strip()

    try:
        ns = getattr(interaction, "namespace", None)
        player = getattr(ns, "player", None) if ns else None

        # build query
        q = sb.table("ocs").select("oc_name")

        # If they selected a player, only show that player's OCs
        if player:
            q = q.eq("owner_discord_id", str(player.id))

        if cur:
            q = q.ilike("oc_name", f"%{cur}%")

        # Order/limit last (Supabase client is fine with this style)
        res = q.order("oc_name").limit(25).execute()
        rows = getattr(res, "data", None) or []
        return [app_commands.Choice(name=r["oc_name"], value=r["oc_name"]) for r in rows if r.get("oc_name")]
    except Exception:
        return []


class InventoryView(discord.ui.View):
    def __init__(self, owner_id: int, oc_name: str, items: List[Dict], skills: List[Dict], per_page: int = 8):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.oc_name = oc_name
        self.items = items
        self.skills = skills
        self.per_page = per_page

        self.mode = "items"  # or "skills"
        self.page = 0
        self.message: Optional[discord.Message] = None

        self.select_mode.options = [
            discord.SelectOption(label="Items", value="items", default=True),
            discord.SelectOption(label="Skills", value="skills"),
        ]

    def _entries(self) -> List[Dict]:
        return self.items if self.mode == "items" else self.skills

    def total_pages(self) -> int:
        return max(1, math.ceil(len(self._entries()) / self.per_page))

    def slice(self) -> List[Dict]:
        start = self.page * self.per_page
        end = start + self.per_page
        return self._entries()[start:end]

    def make_embed(self) -> discord.Embed:
        kind = "Items" if self.mode == "items" else "Skills"
        entries = self._entries()

        embed = discord.Embed(
            title=f"Inventory • {self.oc_name} • {kind}",
            description=f"Owner: <@{self.owner_id}> • {kind}: **{len(entries)}**",
            color=discord.Color.teal(),
        )

        page_entries = self.slice()
        if not page_entries:
            embed.add_field(name="Empty", value="No entries found.", inline=False)
        else:
            for row in page_entries:
                if self.mode == "items":
                    name = row.get("item_name", "Unknown Item")
                    qty = row.get("quantity", 0)
                    active = row.get("active", True)
                    effect = row.get("effect") or "—"
                    duration = row.get("duration") or "—"
                    line = f"Qty: **{qty}** • Active: **{active}**\nEffect: {effect}\nDuration: {duration}"
                    if row.get("doc_url"):
                        line += f"\n[Doc]({row['doc_url']})"
                    embed.add_field(name=name, value=line, inline=False)
                else:
                    name = row.get("skill_name", "Unknown Skill")
                    cost = row.get("cost_ap", 0)
                    desc = (row.get("description") or "—").strip()
                    if len(desc) > 350:
                        desc = desc[:350] + "…"
                    line = f"Cost: **{cost}**\n{desc}"
                    if row.get("doc_url"):
                        line += f"\n[Doc]({row['doc_url']})"
                    embed.add_field(name=name, value=line, inline=False)

        embed.set_footer(text=f"Page {self.page+1}/{self.total_pages()}")
        return embed

    async def refresh(self, interaction: discord.Interaction):
        if self.message:
            await self.message.edit(embed=self.make_embed(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True

    @discord.ui.select(placeholder="Switch view…", min_values=1, max_values=1)
    async def select_mode(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.mode = select.values[0]
        self.page = 0
        for opt in select.options:
            opt.default = (opt.value == self.mode)
        await interaction.response.defer()
        await self.refresh(interaction)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
        await interaction.response.defer()
        await self.refresh(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < self.total_pages() - 1:
            self.page += 1
        await interaction.response.defer()
        await self.refresh(interaction)

    async def on_timeout(self):
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


# ✅ RENAMED COG CLASS to avoid "Cog named 'Inventory' already loaded"
class InventoryCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="inventory", description="View an OC's inventory (items + skills).")
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(
        oc_name="Exact OC name",
        player="Whose OC? (optional; helps narrow results)",
        public="Show publicly? (default: true)"
    )
    @app_commands.autocomplete(oc_name=oc_name_autocomplete_inventory)
    async def inventory(
        self,
        interaction: discord.Interaction,
        oc_name: str,
        player: Optional[discord.User] = None,
        public: Optional[bool] = True,
    ):
        ephemeral = not bool(public)
        await interaction.response.defer(ephemeral=ephemeral)

        sb = get_supabase_client()

        oc_name_norm = (oc_name or "").strip()
        if not oc_name_norm:
            return await interaction.followup.send("Please provide an `oc_name`.", ephemeral=ephemeral)

        try:
            # Resolve OC:
            # - If player provided: use strict owner+name (existing behavior)
            # - Else: find by name globally (must be unique-ish)
            if player:
                oc = get_oc_by_owner_and_name_or_raise(player.id, oc_name_norm)
                owner_id = player.id
                oc_id = oc["oc_id"]
            else:
                res = (
                    sb.table("ocs")
                    .select("oc_id, oc_name, owner_discord_id")
                    .eq("oc_name", oc_name_norm)
                    .limit(3)
                    .execute()
                )
                rows = getattr(res, "data", None) or []
                if not rows:
                    return await interaction.followup.send(
                        "OC not found. Tip: pick a name from autocomplete, or provide `player` to narrow it down.",
                        ephemeral=ephemeral,
                    )
                if len(rows) > 1:
                    return await interaction.followup.send(
                        "Multiple OCs share that name. Please re-run `/inventory` and pick a `player` to narrow it down.",
                        ephemeral=ephemeral,
                    )

                row = rows[0]
                oc_id = row["oc_id"]
                owner_id = int(row["owner_discord_id"])

            # Items
            inv_res = (
                sb.table("inventories_norm")
                .select("item_id, quantity")
                .eq("oc_id", oc_id)
                .gt("quantity", 0)
                .execute()
            )
            inv_rows = getattr(inv_res, "data", None) or []
            item_entries: List[Dict] = []
            if inv_rows:
                item_ids = [r["item_id"] for r in inv_rows]
                meta_res = (
                    sb.table("item_catalog")
                    .select("item_id, item_name, effect, duration, active, doc_url")
                    .in_("item_id", item_ids)
                    .execute()
                )
                meta_rows = getattr(meta_res, "data", None) or []
                meta_by_id = {m["item_id"]: m for m in meta_rows}
                for r in inv_rows:
                    meta = meta_by_id.get(r["item_id"], {})
                    item_entries.append({
                        "item_id": r["item_id"],
                        "quantity": r["quantity"],
                        "item_name": meta.get("item_name", "Unknown"),
                        "effect": meta.get("effect"),
                        "duration": meta.get("duration"),
                        "active": meta.get("active", True),
                        "doc_url": meta.get("doc_url"),
                    })
                item_entries.sort(key=lambda x: (x["item_name"].lower(), -int(x["quantity"])))

            # Skills
            skills_res = (
                sb.table("oc_skills")
                .select("skill_id")
                .eq("oc_id", oc_id)
                .execute()
            )
            oc_skill_rows = getattr(skills_res, "data", None) or []
            skill_entries: List[Dict] = []
            if oc_skill_rows:
                skill_ids = [r["skill_id"] for r in oc_skill_rows]
                cat_res = (
                    sb.table("skill_catalog")
                    .select("skill_id, skill_name, description, cost_ap, active, doc_url")
                    .in_("skill_id", skill_ids)
                    .execute()
                )
                cat_rows = getattr(cat_res, "data", None) or []
                skill_entries = cat_rows
                skill_entries.sort(key=lambda s: (s.get("skill_name", "").lower()))

        except Exception as e:
            return await interaction.followup.send(f"Error: {e}", ephemeral=ephemeral)

        if not item_entries and not skill_entries:
            embed = discord.Embed(
                title=f"Inventory • {oc_name_norm}",
                description=f"Owner: <@{owner_id}>\nNo items or skills found.",
                color=discord.Color.teal(),
            )
            return await interaction.followup.send(embed=embed, ephemeral=ephemeral)

        view = InventoryView(owner_id=owner_id, oc_name=oc_name_norm, items=item_entries, skills=skill_entries)
        msg = await interaction.followup.send(embed=view.make_embed(), view=view, ephemeral=ephemeral)
        view.message = msg


async def setup(bot):
    # ✅ add the renamed cog
    await bot.add_cog(InventoryCog(bot))
