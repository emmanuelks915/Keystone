# cogs/oc_overview.py
from __future__ import annotations

from typing import Optional, List, Dict, Tuple
import discord
from discord import app_commands
from discord.ext import commands

from services.db import get_supabase_client
from config.skyfall import AP_EMOJI
from services.ap_service import get_ap

# ---------- emoji constants ----------
THRAL_EMOJI = "<:thral:1388999536143499477>"
TOKEN_EMOJI = "<:token:1447676379536691201>"

# ---------- guild lock ----------
SKYFALL_GUILD_ID = 1374730886234374235
SKYFALL_GUILD = discord.Object(id=SKYFALL_GUILD_ID)

STAT_CAP = 1500  # UI cap
STATS_STAFF_ROLE_ID = 1374730886490357822  # stats staff role

# ----------------- helpers -----------------
STAT_KEYS = [
    ("dexterity", "Dexterity"),
    ("reflexes", "Reflexes"),
    ("strength", "Strength"),
    ("durability", "Durability"),
    ("mana", "Mana"),
    ("magic_output", "Magic Output"),
    ("magic_control", "Magic Control"),
]
BST_KEYS = [k for k, _ in STAT_KEYS]  # BST excludes luck


def _bst(stats_row: Dict) -> int:
    return int(sum(int(stats_row.get(k, 0) or 0) for k in BST_KEYS))


def _fmt_stat_line(label: str, value: int) -> str:
    return f"{label:<14} {value:>4} / {STAT_CAP:<4}"


def _tier_from_progress(p: int) -> str:
    p = int(p or 0)
    if p >= 100:
        return "100/100"
    if p >= 75:
        return "75/100"
    if p >= 50:
        return "50/100"
    if p >= 25:
        return "25/100"
    return f"{p}/100"


def _is_stats_staff(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    return any(r.id == STATS_STAFF_ROLE_ID for r in interaction.user.roles)


def _status_badge(status: str) -> str:
    s = (status or "").strip().lower()
    if s == "approved":
        return "✅ `approved`"
    if s == "pending_review":
        return "🟧 `pending_review`"
    if s == "needs_changes":
        return "⚠️ `needs_changes`"
    if s == "unallocated":
        return "⬜ `unallocated`"
    return f"`{status or 'unknown'}`"


def _status_color(status: str) -> discord.Color:
    s = (status or "").strip().lower()
    if s == "approved":
        return discord.Color.green()
    if s == "pending_review":
        return discord.Color.orange()
    if s == "needs_changes":
        return discord.Color.red()
    if s == "unallocated":
        return discord.Color.light_grey()
    return discord.Color.dark_gold()


def _fmt_money(n: int) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


class OverviewLinks(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(
            discord.ui.Button(
                style=discord.ButtonStyle.link,
                label="Stats Guide",
                url="https://skyfall-rp.com/starting-guides/stats/",
            )
        )
        self.add_item(
            discord.ui.Button(
                style=discord.ButtonStyle.link,
                label="Spirits & Devils",
                url="https://skyfall-rp.com/starting-guides/spirits-devils-starting-guides/",
            )
        )


class OCSummaryCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sb = get_supabase_client()

    # --------- OC autocomplete (ALL OCs) ----------
    async def oc_name_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        q = (current or "").strip()
        try:
            query = self.sb.table("ocs").select("oc_name")
            if q:
                query = query.ilike("oc_name", f"%{q}%")
            res = query.order("oc_name").limit(25).execute()
        except Exception as e:
            print(f"[oc_name_autocomplete] SELECT exception: {e}")
            return []

        names = [r.get("oc_name") for r in (getattr(res, "data", None) or []) if r.get("oc_name")]
        seen = set()
        out: List[app_commands.Choice[str]] = []
        for n in names:
            key = n.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(app_commands.Choice(name=n, value=n))
        return out[:20]

    # --------- db fetches ----------
    def _get_oc_by_name(self, oc_name: str) -> Dict:
        res = self.sb.table("ocs").select("*").eq("oc_name", oc_name).limit(1).execute()
        if not getattr(res, "data", None):
            raise ValueError(f"OC not found: {oc_name}")
        return res.data[0]

    def _get_stats(self, oc_id: str) -> Dict:
        res = self.sb.table("oc_stats").select("*").eq("oc_id", oc_id).limit(1).execute()
        return (res.data[0] if getattr(res, "data", None) else {"oc_id": oc_id})

    def _get_bond(self, oc_id: str, bond: str) -> Optional[Dict]:
        res = (
            self.sb.table("oc_bonds")
            .select("*")
            .eq("oc_id", oc_id)
            .eq("bond", bond)
            .limit(1)
            .execute()
        )
        return (res.data[0] if getattr(res, "data", None) else None)

    def _get_wallets(self, oc_id: str) -> Tuple[int, int, int, int, int]:
        """
        Returns: (token_bal, thral_bal, ap_bal, active_loan_count, total_owed)
        Mirrors /balance behavior.
        """
        # Tokens
        try:
            w = self.sb.table("token_wallets").select("balance").eq("oc_id", oc_id).limit(1).execute()
            token_bal = int((w.data[0]["balance"]) if getattr(w, "data", None) else 0)
        except Exception:
            token_bal = 0

        # Thral
        try:
            w = self.sb.table("thral_wallets").select("balance").eq("oc_id", oc_id).limit(1).execute()
            thral_bal = int((w.data[0]["balance"]) if getattr(w, "data", None) else 0)
        except Exception:
            thral_bal = 0

        # AP
        try:
            ap_bal = int(get_ap(oc_id))
        except Exception:
            ap_bal = 0

        # Loans
        active_count = 0
        total_owed = 0
        try:
            res = (
                self.sb.table("thral_loans")
                .select("remaining_balance")
                .eq("oc_id", oc_id)
                .eq("status", "active")
                .execute()
            )
            rows = getattr(res, "data", None) or []
            active_count = len(rows)
            total_owed = sum(int(r.get("remaining_balance") or 0) for r in rows)
        except Exception:
            active_count, total_owed = 0, 0

        return token_bal, thral_bal, ap_bal, active_count, total_owed

    def _get_injuries_summary(
        self,
        guild_id: str,
        oc_name: str,
        owner_discord_id: str,
        limit: int = 5,
    ) -> str:
        """
        Matches your injuries schema:
          injuries.guild_id (text)
          injuries.oc_name (text)
          injuries.owner_discord_id (text)
        """
        try:
            res = (
                self.sb.table("injuries")
                .select("injury,tier,status,heal_at,downgrade_at,created_at")
                .eq("guild_id", guild_id)
                .eq("oc_name", oc_name)
                .eq("owner_discord_id", owner_discord_id)
                .eq("status", "active")
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            rows = getattr(res, "data", None) or []
        except Exception as e:
            print(f"[_get_injuries_summary] SELECT exception: {e}")
            return "No injury data"

        if not rows:
            return "None"

        lines = []
        for r in rows:
            name = r.get("injury") or "Injury"
            tier = r.get("tier")
            tag = f"T{tier}" if tier is not None else ""

            next_date = r.get("downgrade_at") or r.get("heal_at")
            next_txt = f" • next: `{next_date}`" if next_date else ""

            lines.append(f"• {name} {tag}{next_txt}".strip())

        return "\n".join(lines)

    # ----------------- commands -----------------
    # IMPORTANT FIX:
    # - Group() does NOT accept guilds= in your discord.py version.
    # - Apply @app_commands.guilds(...) to the GROUP object instead.
    oc = app_commands.Group(name="oc", description="OC tools")
    oc = app_commands.guilds(SKYFALL_GUILD)(oc)

    @oc.command(
        name="overview",
        description="View an OC's overview card (stats + bonds + rank + injuries + balance).",
    )
    @app_commands.autocomplete(oc_name=oc_name_autocomplete)
    async def overview(self, interaction: discord.Interaction, oc_name: str):
        await interaction.response.defer(ephemeral=False)

        oc = self._get_oc_by_name(oc_name)
        oc_id = oc["oc_id"]

        stats_status = oc.get("stats_status", "unknown")
        rank = (oc.get("rank") or "Unranked").strip()

        stats = self._get_stats(oc_id)
        bst_val = _bst(stats)

        # NEW: Unallocated SP + Free resets remaining (stored on oc_stats)
        unalloc_sp = int(stats.get("unallocated_sp", 0) or 0)
        free_resets = int(stats.get("free_resets_remaining", 1) or 0)

        spirit = self._get_bond(oc_id, "spirit")
        devil = self._get_bond(oc_id, "devil")

        token_bal, thral_bal, ap_bal, loan_count, loan_owed = self._get_wallets(oc_id)

        owner_id = str(oc.get("owner_discord_id") or "")
        injuries_txt = self._get_injuries_summary(
            guild_id=str(SKYFALL_GUILD_ID),
            oc_name=oc_name,
            owner_discord_id=owner_id,
            limit=5,
        )

        # Stats block (left)
        left_lines = []
        for key, label in STAT_KEYS:
            left_lines.append(_fmt_stat_line(label, int(stats.get(key, 0) or 0)))
        left_block = "```" + "\n".join(left_lines) + "```"

        # Bonds
        spirit_friend = "N/A" if not spirit else _tier_from_progress(int(spirit.get("friendship", 0) or 0))
        spirit_dive = "N/A" if not spirit else _tier_from_progress(int(spirit.get("mastery", 0) or 0))
        devil_friend = "N/A" if not devil else _tier_from_progress(int(devil.get("friendship", 0) or 0))
        devil_unison = "N/A" if not devil else _tier_from_progress(int(devil.get("mastery", 0) or 0))

        loans_line = (
            "None"
            if loan_count <= 0
            else f"Active: **{loan_count}** • Owed: **{_fmt_money(loan_owed)} {THRAL_EMOJI}**"
        )

        right_block = (
            f"**Rank:** `{rank}`\n"
            f"**Status:** {_status_badge(stats_status)}\n"
            f"**BST:** `{bst_val}`\n"
            f"**Unallocated SP:** `{_fmt_money(unalloc_sp)}`\n"
            f"**Free Resets Remaining:** `{_fmt_money(free_resets)}`\n\n"
            f"**{TOKEN_EMOJI} Tokens:** `{_fmt_money(token_bal)}`\n"
            f"**{THRAL_EMOJI} Thral:** `{_fmt_money(thral_bal)}`\n"
            f"**{AP_EMOJI} AP:** `{_fmt_money(ap_bal)}`\n"
            f"**📄 Loans:** {loans_line}\n\n"
            f"**Devil Friendship:** `{devil_friend}`\n"
            f"**Devil Unison:** `{devil_unison}`\n"
            f"**Spirit Friendship:** `{spirit_friend}`\n"
            f"**Spirit Dive:** `{spirit_dive}`\n\n"
            f"**Injuries:**\n{injuries_txt}"
        )

        embed = discord.Embed(
            title=f"{oc_name} — Overview",
            description=f"{left_block}\n{right_block}",
            color=_status_color(stats_status),
        )

        if owner_id:
            embed.set_footer(text=f"Owner: {owner_id} • OC ID: {oc_id} • Tip: use /balance for full wallet view")

        avatar_url = oc.get("avatar_url")
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

        await interaction.followup.send(embed=embed, view=OverviewLinks())

    # ----------------- STATS (read-only) -----------------
    @oc.command(name="stats_view", description="View an OC's stats.")
    @app_commands.autocomplete(oc_name=oc_name_autocomplete)
    async def stats_view(self, interaction: discord.Interaction, oc_name: str):
        oc = self._get_oc_by_name(oc_name)
        stats = self._get_stats(oc["oc_id"])
        bst_val = _bst(stats)

        lines = [f"BST: {bst_val}"]
        for k, label in STAT_KEYS:
            lines.append(f"{label}: {int(stats.get(k, 0) or 0)} / {STAT_CAP}")
        lines.append(f"Luck: {int(stats.get('luck', 0) or 0)}")

        await interaction.response.send_message("```" + "\n".join(lines) + "```", ephemeral=True)

    # ----------------- STAFF EDITS -----------------
    @oc.command(name="stats_set", description="(Stats Staff) Set an OC stat to an exact value.")
    @app_commands.autocomplete(oc_name=oc_name_autocomplete)
    async def stats_set(
        self,
        interaction: discord.Interaction,
        oc_name: str,
        stat: str,
        value: int,
        reason: Optional[str] = None,
    ):
        if not _is_stats_staff(interaction):
            return await interaction.response.send_message("You don’t have permission to edit stats.", ephemeral=True)

        stat = stat.lower().replace(" ", "_")
        allowed = set([k for k, _ in STAT_KEYS] + ["luck"])
        if stat not in allowed:
            return await interaction.response.send_message(
                f"Invalid stat. Options: {', '.join(sorted(allowed))}",
                ephemeral=True,
            )

        oc = self._get_oc_by_name(oc_name)
        oc_id = oc["oc_id"]
        actor_id = str(interaction.user.id)

        current = self._get_stats(oc_id)
        old_val = int(current.get(stat, 0) or 0)
        new_val = int(value)
        delta = new_val - old_val

        self.sb.table("oc_stats").upsert({"oc_id": oc_id, stat: new_val}).execute()
        self.sb.table("oc_stat_logs").insert(
            {
                "oc_id": oc_id,
                "stat_key": stat,
                "delta": delta,
                "old_value": old_val,
                "new_value": new_val,
                "actor_discord_id": actor_id,
                "reason": reason or "staff set",
            }
        ).execute()

        await interaction.response.send_message(
            f"✅ Set **{oc_name}** {stat} to **{new_val}** (was {old_val}).",
            ephemeral=True,
        )

    @oc.command(name="stats_add", description="(Stats Staff) Add/Subtract from an OC stat.")
    @app_commands.autocomplete(oc_name=oc_name_autocomplete)
    async def stats_add(
        self,
        interaction: discord.Interaction,
        oc_name: str,
        stat: str,
        amount: int,
        reason: Optional[str] = None,
    ):
        if not _is_stats_staff(interaction):
            return await interaction.response.send_message("You don’t have permission to edit stats.", ephemeral=True)

        stat = stat.lower().replace(" ", "_")
        allowed = set([k for k, _ in STAT_KEYS] + ["luck"])
        if stat not in allowed:
            return await interaction.response.send_message(
                f"Invalid stat. Options: {', '.join(sorted(allowed))}",
                ephemeral=True,
            )

        oc = self._get_oc_by_name(oc_name)
        oc_id = oc["oc_id"]
        actor_id = str(interaction.user.id)

        current = self._get_stats(oc_id)
        old_val = int(current.get(stat, 0) or 0)
        new_val = max(0, old_val + int(amount))
        delta = new_val - old_val

        self.sb.table("oc_stats").upsert({"oc_id": oc_id, stat: new_val}).execute()
        self.sb.table("oc_stat_logs").insert(
            {
                "oc_id": oc_id,
                "stat_key": stat,
                "delta": delta,
                "old_value": old_val,
                "new_value": new_val,
                "actor_discord_id": actor_id,
                "reason": reason or "staff add",
            }
        ).execute()

        await interaction.response.send_message(
            f"✅ Updated **{oc_name}** {stat}: {old_val} → **{new_val}**.",
            ephemeral=True,
        )

    @oc.command(name="stats_history", description="View recent stat changes for an OC.")
    @app_commands.autocomplete(oc_name=oc_name_autocomplete)
    async def stats_history(self, interaction: discord.Interaction, oc_name: str, limit: int = 10):
        oc = self._get_oc_by_name(oc_name)
        oc_id = oc["oc_id"]

        res = (
            self.sb.table("oc_stat_logs")
            .select("created_at, stat_key, delta, old_value, new_value, actor_discord_id, reason")
            .eq("oc_id", oc_id)
            .order("created_at", desc=True)
            .limit(min(max(limit, 1), 25))
            .execute()
        )

        rows = getattr(res, "data", None) or []
        if not rows:
            return await interaction.response.send_message("No stat history found.", ephemeral=True)

        lines = []
        for r in rows:
            reason = (r.get("reason") or "").strip()
            reason_txt = f" — {reason}" if reason else ""
            lines.append(
                f"{r['created_at']} | {r['stat_key']} {r['old_value']}→{r['new_value']} (Δ{r['delta']}) "
                f"by {r['actor_discord_id']}{reason_txt}"
            )

        await interaction.response.send_message("```" + "\n".join(lines) + "```", ephemeral=True)

    # ----------------- BONDS (spirit/devil) -----------------
    @oc.command(name="bond_set", description="(Stats Staff) Set spirit/devil friendship or mastery.")
    @app_commands.autocomplete(oc_name=oc_name_autocomplete)
    async def bond_set(
        self,
        interaction: discord.Interaction,
        oc_name: str,
        bond: str,
        field: str,
        value: int,
        reason: Optional[str] = None,
    ):
        if not _is_stats_staff(interaction):
            return await interaction.response.send_message("You don’t have permission to edit bonds.", ephemeral=True)

        bond = bond.lower()
        if bond not in ("spirit", "devil"):
            return await interaction.response.send_message("Bond must be `spirit` or `devil`.", ephemeral=True)

        field = field.lower()
        if field not in ("friendship", "mastery"):
            return await interaction.response.send_message("Field must be `friendship` or `mastery`.", ephemeral=True)

        oc = self._get_oc_by_name(oc_name)
        oc_id = oc["oc_id"]
        actor_id = str(interaction.user.id)

        cur = self._get_bond(oc_id, bond)
        old_val = int(cur.get(field, 0)) if cur else 0
        new_val = max(0, min(100, int(value)))
        delta = new_val - old_val

        self.sb.table("oc_bonds").upsert({"oc_id": oc_id, "bond": bond, field: new_val}).execute()
        self.sb.table("oc_bond_logs").insert(
            {
                "oc_id": oc_id,
                "bond": bond,
                "field_key": field,
                "delta": delta,
                "old_value": old_val,
                "new_value": new_val,
                "actor_discord_id": actor_id,
                "reason": reason or "staff set",
            }
        ).execute()

        await interaction.response.send_message(
            f"✅ Set **{oc_name}** {bond} {field} to **{new_val}** (was {old_val}).",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(OCSummaryCog(bot))
