# cogs/help.py — Keystone Codex
from __future__ import annotations

from collections import defaultdict
from difflib import get_close_matches
from typing import Iterable

import discord
from discord import app_commands
from discord.ext import commands

from utils.help_data import (
    FAQS,
    HELP_ENTRIES,
    PLAYER_CATEGORY_ORDER,
    STAFF_CATEGORY_ORDER,
    STAFF_HINT_WORDS,
    HelpEntry,
    get_entry,
    normalize_command_name,
)

EMBED_COLOR = 0xF59E0B  # warm Keystone/citrus accent
MAX_FIELD_VALUE = 1024


def _is_staff(interaction: discord.Interaction) -> bool:
    bot = interaction.client
    user = interaction.user

    checker = getattr(bot, "can_run_dev_command", None)
    if callable(checker):
        try:
            return bool(checker(user))
        except Exception:
            pass

    if isinstance(user, discord.Member):
        if user.guild_permissions.administrator:
            return True
        staff_role_ids = getattr(bot, "staff_role_ids", set()) or set()
        return any(role.id in staff_role_ids for role in user.roles)

    return False


def _qualified_name(command: app_commands.Command | app_commands.Group) -> str:
    try:
        return normalize_command_name(command.qualified_name)
    except Exception:
        return normalize_command_name(getattr(command, "name", ""))


def _command_signature(command: app_commands.Command) -> str:
    parts = [f"/{command.qualified_name}"]
    for param in getattr(command, "parameters", []):
        required = getattr(param, "required", False)
        name = getattr(param, "display_name", None) or getattr(param, "name", "option")
        parts.append(f"<{name}>" if required else f"[{name}]")
    return " ".join(parts)


def _looks_staff_only(command_name: str, description: str) -> bool:
    text = f"{command_name} {description}".lower()
    return any(word in text for word in STAFF_HINT_WORDS)


def _category_for(command_name: str, description: str, staff_only: bool) -> str:
    first = command_name.split()[0] if command_name else "other"

    if staff_only:
        if first in {"xp", "stats", "ap", "sp", "skills", "traits", "checks"}:
            return "Staff: XP & Stats"
        if first in {"inventory", "items"}:
            return "Staff: Inventory"
        if first in {"wallet", "currency", "bank", "tax", "stipend", "mint", "burn", "setbalance", "tokens"}:
            return "Staff: Economy Admin"
        if first in {"shop", "commerce"}:
            return "Staff: Shops & Commerce"
        if first == "giveaway":
            return "Staff: Giveaways"
        if first in {"rp", "postwindow"}:
            return "Staff: RP Management"
        if first in {"sync", "reload", "wipe", "backup", "jobs", "status"}:
            return "Staff: Debugging"
        return "Staff: Configuration"

    if first in {"oc", "oc_register", "overview"}:
        return "Character & OC"
    if first in {"xp", "stats", "skills", "traits", "ap", "sp", "checks"}:
        return "XP & Stats"
    if first in {"roll", "contest", "stat", "dice"}:
        return "Dice & Combat"
    if first in {"inventory", "items"}:
        return "Inventory & Items"
    if first in {"wallet", "pay", "bank", "casino", "leaderboard", "tokens"}:
        return "Economy"
    if first in {"shop", "commerce"}:
        return "Shops & Commerce"
    if first == "giveaway":
        return "Giveaways"
    if first in {"rp", "postwindow"}:
        return "RP Tracker"
    if first == "travel":
        return "Travel"
    return "Other"


def _truncate(value: str, limit: int = MAX_FIELD_VALUE) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


class CodexCategorySelect(discord.ui.Select):
    def __init__(self, parent: "CodexView", categories: list[str]):
        self.parent_view = parent
        options = [
            discord.SelectOption(label=category[:100], value=category, emoji="📖")
            for category in categories[:25]
        ]
        super().__init__(
            placeholder="Choose a Keystone Codex category...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        category = self.values[0]
        embed = self.parent_view.cog.build_category_embed(
            interaction,
            category=category,
            staff_mode=self.parent_view.staff_mode,
        )
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class CodexView(discord.ui.View):
    def __init__(self, cog: "HelpCog", *, staff_mode: bool, categories: list[str]):
        super().__init__(timeout=180)
        self.cog = cog
        self.staff_mode = staff_mode
        if categories:
            self.add_item(CodexCategorySelect(self, categories))

    @discord.ui.button(label="FAQ", style=discord.ButtonStyle.secondary, emoji="❓")
    async def faq_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=self.cog.build_faq_index_embed(staff_mode=self.staff_mode),
            view=self,
        )

    @discord.ui.button(label="Tips", style=discord.ButtonStyle.secondary, emoji="💡")
    async def tips_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=self.cog.build_tips_embed(staff_mode=self.staff_mode),
            view=self,
        )

    @discord.ui.button(label="Home", style=discord.ButtonStyle.primary, emoji="🏠")
    async def home_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=self.cog.build_home_embed(interaction, staff_mode=self.staff_mode),
            view=self,
        )


class HelpCog(commands.Cog):
    """Interactive Keystone help/codex system."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def walk_app_commands(self) -> list[app_commands.Command]:
        found: list[app_commands.Command] = []

        def visit(cmd: app_commands.Command | app_commands.Group):
            if isinstance(cmd, app_commands.Group):
                for child in cmd.commands:
                    visit(child)
            elif isinstance(cmd, app_commands.Command):
                found.append(cmd)

        for command in self.bot.tree.get_commands():
            visit(command)

        return sorted(found, key=lambda c: c.qualified_name)

    def collect_entries(self, *, include_staff: bool) -> list[HelpEntry]:
        merged: dict[str, HelpEntry] = dict(HELP_ENTRIES)

        for command in self.walk_app_commands():
            name = _qualified_name(command)
            if not name or name in merged:
                continue

            description = command.description or "No description provided yet."
            staff_only = _looks_staff_only(name, description)
            if staff_only and not include_staff:
                continue

            merged[name] = HelpEntry(
                command=name,
                category=_category_for(name, description, staff_only),
                description=description,
                usage=_command_signature(command),
                examples=(_command_signature(command),),
                staff_only=staff_only,
            )

        entries = [entry for entry in merged.values() if include_staff or not entry.staff_only]
        return sorted(entries, key=lambda e: (e.category, e.command))

    def categories_for(self, *, staff_mode: bool) -> list[str]:
        entries = self.collect_entries(include_staff=staff_mode)
        present = {entry.category for entry in entries if entry.staff_only is staff_mode or staff_mode}
        preferred = list(STAFF_CATEGORY_ORDER if staff_mode else PLAYER_CATEGORY_ORDER)
        ordered = [cat for cat in preferred if cat in present]
        extras = sorted(present - set(ordered))
        return ordered + extras

    def build_home_embed(self, interaction: discord.Interaction, *, staff_mode: bool) -> discord.Embed:
        title = "🛠️ Keystone Staff Codex" if staff_mode else "📖 Keystone Codex"
        desc = (
            "Staff-side guide for approvals, economy tools, debugging, and management commands."
            if staff_mode
            else "Player guide for characters, XP, stats, dice, inventory, shops, giveaways, RP tracking, and travel."
        )
        embed = discord.Embed(title=title, description=desc, color=EMBED_COLOR)
        embed.add_field(
            name="How to use this",
            value=(
                "Use the dropdown to pick a category.\n"
                "Use `/help command:<command>` for one command.\n"
                "Use `/help search:<word>` to find related commands."
            ),
            inline=False,
        )

        categories = self.categories_for(staff_mode=staff_mode)
        if categories:
            embed.add_field(
                name="Categories",
                value="\n".join(f"• {cat}" for cat in categories[:20]),
                inline=False,
            )

        embed.set_footer(text="Keystone help is permission-aware. Staff commands stay tucked away from regular player help.")
        return embed

    def build_category_embed(self, interaction: discord.Interaction, *, category: str, staff_mode: bool) -> discord.Embed:
        entries = [
            entry
            for entry in self.collect_entries(include_staff=staff_mode)
            if entry.category == category and (staff_mode or not entry.staff_only)
        ]
        embed = discord.Embed(
            title=f"📖 {category}",
            description="Commands and notes for this Keystone system.",
            color=EMBED_COLOR,
        )

        if not entries:
            embed.description = "No commands found for this category yet."
            return embed

        lines: list[str] = []
        for entry in entries[:25]:
            lines.append(f"**/{entry.command}** — {entry.description}")
        embed.add_field(name="Commands", value=_truncate("\n".join(lines)), inline=False)

        rich = [entry for entry in entries if entry.examples or entry.tips]
        if rich:
            entry = rich[0]
            detail = [f"**Usage:** `{entry.usage}`"]
            if entry.examples:
                detail.append("**Examples:** " + ", ".join(f"`{x}`" for x in entry.examples[:3]))
            if entry.tips:
                detail.append("**Tip:** " + entry.tips[0])
            embed.add_field(name="Quick example", value=_truncate("\n".join(detail)), inline=False)

        return embed

    def build_command_embed(self, entry: HelpEntry) -> discord.Embed:
        embed = discord.Embed(
            title=f"/{entry.command}",
            description=entry.description,
            color=EMBED_COLOR,
        )
        embed.add_field(name="Category", value=entry.category, inline=True)
        embed.add_field(name="Visibility", value="Staff" if entry.staff_only else "Player", inline=True)
        embed.add_field(name="Usage", value=f"`{entry.usage}`", inline=False)

        if entry.examples:
            embed.add_field(
                name="Examples",
                value="\n".join(f"`{example}`" for example in entry.examples[:8]),
                inline=False,
            )
        if entry.tips:
            embed.add_field(
                name="Tips",
                value="\n".join(f"• {tip}" for tip in entry.tips[:8]),
                inline=False,
            )
        return embed

    def build_search_embed(self, *, query: str, include_staff: bool) -> discord.Embed:
        q = query.strip().lower()
        entries = self.collect_entries(include_staff=include_staff)
        matches = [
            entry
            for entry in entries
            if q in entry.command.lower()
            or q in entry.category.lower()
            or q in entry.description.lower()
            or any(q in alias.lower() for alias in entry.aliases)
        ]

        embed = discord.Embed(title=f"🔎 Search: {query}", color=EMBED_COLOR)
        if not matches:
            names = [entry.command for entry in entries]
            close = get_close_matches(q, names, n=5, cutoff=0.25)
            if close:
                embed.description = "No exact matches. Did you mean:\n" + "\n".join(f"• `/{x}`" for x in close)
            else:
                embed.description = "No matching Keystone commands found."
            return embed

        lines = [f"**/{entry.command}** — {entry.description}" for entry in matches[:20]]
        embed.description = _truncate("\n".join(lines), 4000)
        return embed

    def build_faq_index_embed(self, *, staff_mode: bool) -> discord.Embed:
        embed = discord.Embed(
            title="❓ Keystone FAQ",
            description="Use `/help faq:<key>` to open one directly.",
            color=EMBED_COLOR,
        )
        lines = []
        for key, faq in FAQS.items():
            if key.startswith("staff") and not staff_mode:
                continue
            lines.append(f"`{key}` — {faq['title']}")
        embed.add_field(name="Available FAQs", value=_truncate("\n".join(lines)), inline=False)
        return embed

    def build_faq_embed(self, key: str) -> discord.Embed:
        faq = FAQS.get(key)
        if not faq:
            return discord.Embed(
                title="FAQ not found",
                description="Use `/help` and press FAQ to see available FAQ keys.",
                color=EMBED_COLOR,
            )
        return discord.Embed(title=f"❓ {faq['title']}", description=faq["body"], color=EMBED_COLOR)

    def build_tips_embed(self, *, staff_mode: bool) -> discord.Embed:
        embed = discord.Embed(title="💡 Keystone Tips", color=EMBED_COLOR)
        if staff_mode:
            embed.description = (
                "• Use `/staffhelp` for staff-only workflows.\n"
                "• Use `/help search:<system>` before hunting through cogs.\n"
                "• For command changes, use `!sync_commands` carefully and do not spam sync.\n"
                "• If a Supabase-backed command fails, check the table schema/cache first."
            )
        else:
            embed.description = (
                "• If Keystone cannot find your OC, run `/oc select`.\n"
                "• Use `/xp history` when you need to audit XP.\n"
                "• Use `/inventory view` before buying or equipping items.\n"
                "• Use `/help search:<word>` instead of guessing command names."
            )
        return embed

    async def command_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        include_staff = _is_staff(interaction)
        q = current.lower().strip()
        choices = []
        for entry in self.collect_entries(include_staff=include_staff):
            if not q or q in entry.command.lower() or q in entry.description.lower():
                choices.append(app_commands.Choice(name=f"/{entry.command}"[:100], value=entry.command))
            if len(choices) >= 25:
                break
        return choices

    async def faq_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        q = current.lower().strip()
        out = []
        for key, faq in FAQS.items():
            if key.startswith("staff") and not _is_staff(interaction):
                continue
            label = f"{key} — {faq['title']}"
            if not q or q in key.lower() or q in faq["title"].lower():
                out.append(app_commands.Choice(name=label[:100], value=key))
            if len(out) >= 25:
                break
        return out

    @app_commands.command(name="help", description="Open the Keystone player help codex.")
    @app_commands.describe(command="Optional command to explain", search="Optional keyword search", faq="Optional FAQ key")
    @app_commands.autocomplete(command=command_autocomplete, faq=faq_autocomplete)
    async def help_command(
        self,
        interaction: discord.Interaction,
        command: str | None = None,
        search: str | None = None,
        faq: str | None = None,
    ):
        include_staff = _is_staff(interaction)

        if command:
            entry = get_entry(command)
            if entry is None:
                # Try live scanned commands too.
                normalized = normalize_command_name(command)
                entry = next((e for e in self.collect_entries(include_staff=include_staff) if e.command == normalized), None)
            if entry is None or (entry.staff_only and not include_staff):
                return await interaction.response.send_message(
                    "I couldn't find that command in the Keystone Codex.",
                    ephemeral=True,
                )
            return await interaction.response.send_message(embed=self.build_command_embed(entry), ephemeral=True)

        if search:
            return await interaction.response.send_message(
                embed=self.build_search_embed(query=search, include_staff=include_staff),
                ephemeral=True,
            )

        if faq:
            return await interaction.response.send_message(embed=self.build_faq_embed(faq), ephemeral=True)

        view = CodexView(self, staff_mode=False, categories=self.categories_for(staff_mode=False))
        await interaction.response.send_message(
            embed=self.build_home_embed(interaction, staff_mode=False),
            view=view,
            ephemeral=True,
        )

    @app_commands.command(name="staffhelp", description="Open the Keystone staff help codex.")
    @app_commands.describe(command="Optional staff command to explain", search="Optional keyword search", faq="Optional FAQ key")
    @app_commands.autocomplete(command=command_autocomplete, faq=faq_autocomplete)
    async def staff_help_command(
        self,
        interaction: discord.Interaction,
        command: str | None = None,
        search: str | None = None,
        faq: str | None = None,
    ):
        if not _is_staff(interaction):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)

        if command:
            normalized = normalize_command_name(command)
            entry = get_entry(normalized)
            if entry is None:
                entry = next((e for e in self.collect_entries(include_staff=True) if e.command == normalized), None)
            if entry is None:
                return await interaction.response.send_message(
                    "I couldn't find that command in the Keystone Staff Codex.",
                    ephemeral=True,
                )
            return await interaction.response.send_message(embed=self.build_command_embed(entry), ephemeral=True)

        if search:
            return await interaction.response.send_message(
                embed=self.build_search_embed(query=search, include_staff=True),
                ephemeral=True,
            )

        if faq:
            return await interaction.response.send_message(embed=self.build_faq_embed(faq), ephemeral=True)

        view = CodexView(self, staff_mode=True, categories=self.categories_for(staff_mode=True))
        await interaction.response.send_message(
            embed=self.build_home_embed(interaction, staff_mode=True),
            view=view,
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
