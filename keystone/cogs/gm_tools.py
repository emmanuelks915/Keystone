# cogs/gm_tools.py
from __future__ import annotations

import datetime as dt
from typing import Optional, Dict, Any, List, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from config.skyfall import SKYFALL_GUILD
from services.db import get_supabase_client


# -----------------------------
# Injury rules (from your doc)
# -----------------------------
# tier points follow the equivalency ladder:
# T1=1, T2=2, T3=4, T4=8, T5=16
TIER_POINTS = {1: 1, 2: 2, 3: 4, 4: 8, 5: 16}

# damage to tier mapping:
# T1: 1-55
# T2: 56-110
# T3: 111-215
# T4: 216-600
# T5: 601+
def tier_from_damage(dmg: int) -> int:
    if dmg <= 0:
        return 1
    if 1 <= dmg <= 55:
        return 1
    if 56 <= dmg <= 110:
        return 2
    if 111 <= dmg <= 215:
        return 3
    if 216 <= dmg <= 600:
        return 4
    return 5


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(dt_obj: Optional[dt.datetime]) -> Optional[str]:
    return dt_obj.isoformat() if dt_obj else None


def parse_iso(s: Optional[str]) -> Optional[dt.datetime]:
    if not s:
        return None
    # Python can parse ISO with timezone via fromisoformat
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def add_days(d: dt.datetime, days: int) -> dt.datetime:
    return d + dt.timedelta(days=days)


def compute_schedule(created_at: dt.datetime, tier: int) -> Tuple[Optional[dt.datetime], Optional[dt.datetime]]:
    """
    Returns (downgrade_at, heal_at) per your rules:

    - T1: heals on its own (no exact time given). We'll leave null by default (GM can resolve),
          OR you can later set a standard (ex: 7 days). For now: no auto schedule.
    - T2: heals on its own after 2 weeks -> heal_at = created + 14 days
    - T3: heals down to T2 after 2 weeks -> downgrade_at = created + 14 days (then it becomes T2 with a new heal timer)
    - T4: does not heal on its own -> None
    - T5: does not heal on its own -> None
    """
    if tier == 2:
        return (None, add_days(created_at, 14))
    if tier == 3:
        return (add_days(created_at, 14), None)
    return (None, None)


def points_for_tier(tier: int) -> int:
    return TIER_POINTS.get(tier, 1)


def equiv_tier_from_points(points: int) -> int:
    """
    Determine "danger equivalency" tier from total points.
    If you have enough lower-tier injuries that "match" a higher tier,
    you're treated as that higher tier for how dangerous it is.

    Thresholds:
      >=16 => T5 equivalent
      >=8  => T4 equivalent
      >=4  => T3 equivalent
      >=2  => T2 equivalent
      >=1  => T1 equivalent
    """
    if points >= 16:
        return 5
    if points >= 8:
        return 4
    if points >= 4:
        return 3
    if points >= 2:
        return 2
    return 1


# -----------------------------
# In-memory fallback (dev)
# -----------------------------
_MEMORY_INJURIES: Dict[str, Dict[str, Any]] = {}


class GMTools(commands.Cog):
    """GM tooling commands: injury tracker, etc."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        try:
            self.sb = get_supabase_client()
        except Exception:
            self.sb = None

    # ---- permissions (replace later with your staff-role system) ----
    def _is_gm(self, member: discord.Member) -> bool:
        return member.guild_permissions.administrator

    def _require_gm(self, interaction: discord.Interaction) -> bool:
        return bool(
            interaction.guild
            and isinstance(interaction.user, discord.Member)
            and self._is_gm(interaction.user)
        )

    # ---- supabase helpers ----
    async def _sb_insert(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        res = self.sb.table("injuries").insert(payload).execute()
        if not res.data:
            raise RuntimeError("Supabase insert returned no data.")
        return res.data[0]

    async def _sb_get(self, injury_id: str) -> Optional[Dict[str, Any]]:
        res = self.sb.table("injuries").select("*").eq("id", injury_id).limit(1).execute()
        return (res.data or [None])[0]

    async def _sb_update(self, injury_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        res = self.sb.table("injuries").update(patch).eq("id", injury_id).execute()
        if not res.data:
            raise RuntimeError("Supabase update returned no data.")
        return res.data[0]

    async def _sb_delete(self, injury_id: str) -> None:
        self.sb.table("injuries").delete().eq("id", injury_id).execute()

    async def _sb_list(
        self,
        guild_id: int,
        oc_name: Optional[str] = None,
        owner_id: Optional[int] = None,
        include_resolved: bool = False,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        q = self.sb.table("injuries").select("*").eq("guild_id", str(guild_id))
        if oc_name:
            q = q.ilike("oc_name", oc_name)
        if owner_id:
            q = q.eq("owner_discord_id", str(owner_id))
        if not include_resolved:
            q = q.eq("status", "active")
        q = q.order("created_at", desc=True).limit(limit)
        res = q.execute()
        return res.data or []

    # -----------------------------
    # Injury group
    # -----------------------------
    injury_group = app_commands.Group(
        name="injury",
        description="GM injury tracker commands",
        guild_ids=[SKYFALL_GUILD.id] if hasattr(SKYFALL_GUILD, "id") else None,
    )

    @injury_group.command(name="add", description="Add an injury to an OC (GM only).")
    @app_commands.describe(
        oc_name="OC name",
        owner="Discord user who owns the OC",
        injury="What happened / injury description",
        damage="Optional: damage that went unblocked/undodged (auto-tier)",
        tier="Optional: force a tier (1-5). If set, overrides damage-tier.",
        source="Optional: cause (enemy, mission, etc.)",
        notes="Optional: restrictions / details",
    )
    async def injury_add(
        self,
        interaction: discord.Interaction,
        oc_name: str,
        owner: discord.Member,
        injury: str,
        damage: Optional[int] = None,
        tier: Optional[int] = None,
        source: Optional[str] = None,
        notes: Optional[str] = None,
    ):
        if not self._require_gm(interaction):
            return await interaction.response.send_message("❌ GM only.", ephemeral=True)

        # Determine tier
        final_tier: int
        if tier is not None:
            if tier < 1 or tier > 5:
                return await interaction.response.send_message("Tier must be 1-5.", ephemeral=True)
            final_tier = tier
        elif damage is not None:
            final_tier = tier_from_damage(damage)
        else:
            # If neither provided, default to T1 (GM can heal/resolve later)
            final_tier = 1

        created = utcnow()
        downgrade_at, heal_at = compute_schedule(created, final_tier)
        points = points_for_tier(final_tier)

        payload = {
            "guild_id": str(interaction.guild_id),
            "oc_name": oc_name,
            "owner_discord_id": str(owner.id),

            "injury": injury,
            "source": source,
            "notes": notes,

            "damage": damage,
            "tier": final_tier,
            "points": points,

            "status": "active",
            "created_at": created.isoformat(),
            "updated_at": created.isoformat(),
            "resolved_at": None,

            "downgrade_at": iso(downgrade_at),
            "heal_at": iso(heal_at),

            "created_by_discord_id": str(interaction.user.id),
            "healing_log": [],
        }

        if self.sb:
            row = await self._sb_insert(payload)
            injury_id = row.get("id", "unknown")
        else:
            injury_id = f"mem_{len(_MEMORY_INJURIES) + 1}"
            payload["id"] = injury_id
            _MEMORY_INJURIES[injury_id] = payload

        embed = discord.Embed(
            title="🩸 Injury Added",
            description=f"**{oc_name}** — {injury}",
            timestamp=utcnow(),
        )
        embed.add_field(name="Owner", value=owner.mention, inline=True)
        embed.add_field(name="Tier", value=f"T{final_tier}  (points: {points})", inline=True)
        if damage is not None:
            embed.add_field(name="Damage", value=str(damage), inline=True)
        if heal_at:
            embed.add_field(name="Auto-Heal", value=f"<t:{int(heal_at.timestamp())}:D>", inline=False)
        if downgrade_at:
            embed.add_field(name="Auto-Downgrade", value=f"<t:{int(downgrade_at.timestamp())}:D> (T3→T2)", inline=False)
        if source:
            embed.add_field(name="Source", value=source, inline=False)
        if notes:
            embed.add_field(name="Notes", value=notes, inline=False)
        embed.set_footer(text=f"ID: {injury_id}")

        await interaction.response.send_message(embed=embed)

    def _apply_auto_progression_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """
        Apply auto rules based on time *in display*.
        We won't mutate DB here; this is just for list/read formatting.
        Use /injury tick if you want to write changes back.
        """
        if row.get("status") != "active":
            return row

        now = utcnow()
        tier = int(row.get("tier") or 1)

        downgrade_at = parse_iso(row.get("downgrade_at"))
        heal_at = parse_iso(row.get("heal_at"))

        # T3 downgrades to T2 after 2 weeks
        if tier == 3 and downgrade_at and now >= downgrade_at:
            row = dict(row)
            row["tier"] = 2
            row["points"] = points_for_tier(2)
            # after downgrade, it becomes a T2 with heal timer of 2 weeks from now
            row["downgrade_at"] = None
            row["heal_at"] = iso(add_days(now, 14))
            row["_auto_note"] = "Auto: T3 downgraded to T2 (scarring implied)."
            return row

        # T2 heals on its own after 2 weeks
        if tier == 2 and heal_at and now >= heal_at:
            row = dict(row)
            row["status"] = "resolved"
            row["_auto_note"] = "Auto: T2 natural healing period completed."
            return row

        return row

    @injury_group.command(name="tick", description="Apply auto healing/downgrade rules to injuries (GM only).")
    @app_commands.describe(
        oc_name="Optional: only tick injuries for this OC",
        owner="Optional: only tick injuries for this owner",
        limit="Max records to process (default 100)"
    )
    async def injury_tick(
        self,
        interaction: discord.Interaction,
        oc_name: Optional[str] = None,
        owner: Optional[discord.Member] = None,
        limit: int = 100,
    ):
        if not self._require_gm(interaction):
            return await interaction.response.send_message("❌ GM only.", ephemeral=True)

        if not interaction.guild_id:
            return await interaction.response.send_message("Must be used in a server.", ephemeral=True)

        if limit < 1:
            limit = 1
        if limit > 200:
            limit = 200

        if self.sb:
            rows = await self._sb_list(
                guild_id=interaction.guild_id,
                oc_name=oc_name,
                owner_id=owner.id if owner else None,
                include_resolved=False,
                limit=limit,
            )

            updated = 0
            resolved = 0
            downgraded = 0

            for r in rows:
                before_tier = int(r.get("tier") or 1)
                before_status = r.get("status")
                after = self._apply_auto_progression_row(r)

                # write back if changed
                patch: Dict[str, Any] = {}
                if int(after.get("tier") or 1) != before_tier:
                    patch["tier"] = int(after["tier"])
                    patch["points"] = int(after["points"])
                    patch["downgrade_at"] = after.get("downgrade_at")
                    patch["heal_at"] = after.get("heal_at")
                    downgraded += 1

                if after.get("status") != before_status:
                    patch["status"] = after["status"]
                    patch["resolved_at"] = utcnow().isoformat()
                    resolved += 1

                if patch:
                    patch["updated_at"] = utcnow().isoformat()
                    await self._sb_update(r["id"], patch)
                    updated += 1

            return await interaction.response.send_message(
                f"✅ Tick complete. Updated: {updated} | Downgraded: {downgraded} | Resolved: {resolved}",
                ephemeral=True
            )

        # memory fallback (no supabase)
        updated = 0
        for iid, r in list(_MEMORY_INJURIES.items()):
            if r.get("guild_id") != str(interaction.guild_id):
                continue
            if oc_name and r.get("oc_name", "").lower() != oc_name.lower():
                continue
            if owner and r.get("owner_discord_id") != str(owner.id):
                continue
            if r.get("status") != "active":
                continue

            after = self._apply_auto_progression_row(r)
            if after != r:
                _MEMORY_INJURIES[iid] = after
                updated += 1

        await interaction.response.send_message(f"✅ Tick complete. Updated: {updated}", ephemeral=True)

    @injury_group.command(name="list", description="List injuries + show danger equivalency.")
    @app_commands.describe(
        oc_name="Optional: filter by OC name",
        owner="Optional: filter by owner",
        include_resolved="Include resolved injuries too"
    )
    async def injury_list(
        self,
        interaction: discord.Interaction,
        oc_name: Optional[str] = None,
        owner: Optional[discord.Member] = None,
        include_resolved: bool = False,
    ):
        if not interaction.guild_id:
            return await interaction.response.send_message("Must be used in a server.", ephemeral=True)

        if self.sb:
            rows = await self._sb_list(
                guild_id=interaction.guild_id,
                oc_name=oc_name,
                owner_id=owner.id if owner else None,
                include_resolved=include_resolved,
                limit=50,
            )
        else:
            rows = [
                r for r in _MEMORY_INJURIES.values()
                if r.get("guild_id") == str(interaction.guild_id)
            ]
            if oc_name:
                rows = [r for r in rows if r.get("oc_name", "").lower() == oc_name.lower()]
            if owner:
                rows = [r for r in rows if r.get("owner_discord_id") == str(owner.id)]
            if not include_resolved:
                rows = [r for r in rows if r.get("status") == "active"]
            rows = sorted(rows, key=lambda r: r.get("created_at", ""), reverse=True)[:50]

        if not rows:
            return await interaction.response.send_message("No injuries found for that filter.", ephemeral=True)

        # apply auto progression for display
        display_rows = [self._apply_auto_progression_row(dict(r)) for r in rows]

        # compute points/danger equivalency across ACTIVE injuries
        active_points = sum(int(r.get("points") or 0) for r in display_rows if r.get("status") == "active")
        danger_equiv = equiv_tier_from_points(active_points)

        embed = discord.Embed(
            title="🩹 Injury Tracker",
            description=(
                f"Active points: **{active_points}** → danger equivalency: **T{danger_equiv}**\n"
                f"*(This uses your ladder: T2=2×T1, T3=2×T2, etc.)*"
            ),
            timestamp=utcnow(),
        )

        for r in display_rows[:10]:
            status = r.get("status", "active")
            iid = r.get("id", "unknown")
            tier = int(r.get("tier") or 1)
            pts = int(r.get("points") or 0)

            extra = ""
            auto_note = r.get("_auto_note")
            if auto_note:
                extra += f"\n_{auto_note}_"

            heal_at = parse_iso(r.get("heal_at"))
            downgrade_at = parse_iso(r.get("downgrade_at"))
            if status == "active":
                if downgrade_at:
                    extra += f"\nDowngrade: <t:{int(downgrade_at.timestamp())}:R> (T3→T2)"
                if heal_at:
                    extra += f"\nHeals: <t:{int(heal_at.timestamp())}:R>"

            line = (
                f"**{r.get('oc_name')}** — {r.get('injury')}\n"
                f"Tier: `T{tier}` (pts {pts}) | Status: `{status}` | ID: `{iid}`"
                f"{extra}"
            )
            embed.add_field(name="\u200b", value=line, inline=False)

        if len(display_rows) > 10:
            embed.set_footer(text=f"+{len(display_rows)-10} more… narrow your filter to see them")

        await interaction.response.send_message(embed=embed)

    @injury_group.command(name="heal", description="Log healing applied to an injury (GM for now).")
    @app_commands.describe(
        injury_id="Injury ID",
        healer="Who performed the healing",
        method="Spell/item/service used (free text)",
        effect="What it does mechanically (ex: resolve, downgrade, stop bleeding, etc.)",
    )
    async def injury_heal(
        self,
        interaction: discord.Interaction,
        injury_id: str,
        healer: discord.Member,
        method: str,
        effect: str,
    ):
        # You said: healer players OR NPC shop.
        # Until you give healer roles/permissions, we keep this GM only.
        if not self._require_gm(interaction):
            return await interaction.response.send_message("❌ GM only (until healer permissions are defined).", ephemeral=True)

        entry = {
            "by_discord_id": str(healer.id),
            "method": method,
            "effect": effect,
            "at": utcnow().isoformat(),
        }

        if self.sb:
            row = await self._sb_get(injury_id)
            if not row:
                return await interaction.response.send_message("Couldn’t find that injury ID.", ephemeral=True)

            log = row.get("healing_log") or []
            log.append(entry)

            patch = {"healing_log": log, "updated_at": utcnow().isoformat()}
            await self._sb_update(injury_id, patch)

            oc_name = row.get("oc_name", "Unknown OC")
            injury_txt = row.get("injury", "Unknown injury")
        else:
            row = _MEMORY_INJURIES.get(injury_id)
            if not row:
                return await interaction.response.send_message("Couldn’t find that injury ID.", ephemeral=True)
            row["healing_log"].append(entry)
            row["updated_at"] = utcnow().isoformat()
            oc_name = row["oc_name"]
            injury_txt = row["injury"]

        embed = discord.Embed(
            title="✨ Healing Logged",
            description=f"**{oc_name}** — {injury_txt}",
            timestamp=utcnow(),
        )
        embed.add_field(name="Healer", value=healer.mention, inline=True)
        embed.add_field(name="Method", value=method, inline=False)
        embed.add_field(name="Effect", value=effect, inline=False)
        embed.set_footer(text=f"Injury ID: {injury_id}")

        await interaction.response.send_message(embed=embed)

    @injury_group.command(name="resolve", description="Resolve an injury (GM only).")
    @app_commands.describe(injury_id="Injury ID", note="Optional resolution note")
    async def injury_resolve(self, interaction: discord.Interaction, injury_id: str, note: Optional[str] = None):
        if not self._require_gm(interaction):
            return await interaction.response.send_message("❌ GM only.", ephemeral=True)

        patch = {
            "status": "resolved",
            "resolved_at": utcnow().isoformat(),
            "updated_at": utcnow().isoformat(),
        }
        if note:
            patch["notes"] = note

        if self.sb:
            try:
                row = await self._sb_update(injury_id, patch)
            except Exception:
                return await interaction.response.send_message("Couldn’t find that injury ID.", ephemeral=True)
            oc_name = row.get("oc_name", "Unknown OC")
            injury_txt = row.get("injury", "Unknown injury")
        else:
            if injury_id not in _MEMORY_INJURIES:
                return await interaction.response.send_message("Couldn’t find that injury ID.", ephemeral=True)
            _MEMORY_INJURIES[injury_id].update(patch)
            oc_name = _MEMORY_INJURIES[injury_id]["oc_name"]
            injury_txt = _MEMORY_INJURIES[injury_id]["injury"]

        await interaction.response.send_message(f"✅ Resolved: **{oc_name}** — {injury_txt} (`{injury_id}`)")

    @injury_group.command(name="remove", description="Delete an injury entry (GM only).")
    @app_commands.describe(injury_id="Injury ID")
    async def injury_remove(self, interaction: discord.Interaction, injury_id: str):
        if not self._require_gm(interaction):
            return await interaction.response.send_message("❌ GM only.", ephemeral=True)

        if self.sb:
            await self._sb_delete(injury_id)
        else:
            _MEMORY_INJURIES.pop(injury_id, None)

        await interaction.response.send_message(f"🗑️ Removed injury `{injury_id}`.")

    # register group
    @commands.Cog.listener()
    async def on_ready(self):
        try:
            self.bot.tree.add_command(self.injury_group, guild=SKYFALL_GUILD)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(GMTools(bot), guild=SKYFALL_GUILD)
