# cogs/tokens.py
from __future__ import annotations

import os
import json  # for pretty-printing payloads
from typing import Optional, List

import discord
from discord import app_commands
from discord.ext import commands

# ---------- emoji constants ----------
TOKEN_EMOJI = "<:Token:1447676379536691201>"

# ---------- guild lock ----------
SKYFALL_GUILD_ID = 1374730886234374235
SKYFALL_GUILD = discord.Object(id=SKYFALL_GUILD_ID)


# ---------- permission config (reuse same staff roles / superusers) ----------
def _parse_id_set(val: str) -> set[int]:
    out: set[int] = set()
    for chunk in (val or "").replace(" ", "").split(","):
        if not chunk:
            continue
        try:
            out.add(int(chunk))
        except ValueError:
            pass
    return out


TOKEN_STAFF_ROLE_IDS: set[int] = _parse_id_set(
    os.getenv(
        "TOKEN_STAFF_ROLE_IDS",
        # default: same roles as Thral staff
        "1374730886507139076,1381086606261223545,"
        "1374730886507139075,1374730886507139072,"
        "1374730886507139074,1374730886507139073,"
        "1374730886490357828",
    )
)

TOKEN_SUPERUSER_IDS: set[int] = _parse_id_set(os.getenv("TOKEN_SUPERUSER_IDS", ""))


def can_manage_tokens(member: discord.Member) -> bool:
    if member.id in TOKEN_SUPERUSER_IDS:
        return True
    return any(r.id in TOKEN_STAFF_ROLE_IDS for r in getattr(member, "roles", []))


class Tokens(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- helpers ----------
    def _supabase(self):
        """Use the shared Supabase client attached in bot.py."""
        return self.bot.supabase

    def _validate_oc_name(self, oc_name: str) -> tuple[bool, str]:
        if not oc_name or not isinstance(oc_name, str):
            return False, ""
        cleaned = oc_name.strip()
        if not cleaned:
            return False, ""
        if len(cleaned) > 100:
            return False, cleaned
        return True, cleaned

    async def _resolve_oc_id(self, supabase, owner_discord_id: str, oc_name: str) -> Optional[str]:
        try:
            res = (
                supabase.table("ocs")
                .select("oc_id")
                .eq("owner_discord_id", owner_discord_id)
                .eq("oc_name", oc_name)
                .limit(1)
                .execute()
            )
            rows = getattr(res, "data", None) or []
            return rows[0]["oc_id"] if rows else None
        except Exception as e:
            print(f"[_resolve_oc_id tokens] Exception resolving OC {oc_name} for {owner_discord_id}: {e}")
            return None

    async def _get_oc_by_id(self, supabase, oc_id: str) -> Optional[dict]:
        """
        Fetch a full OC record (including avatar_url) by oc_id.
        Returns dict with oc_id, oc_name, owner_discord_id, avatar_url or None.
        """
        try:
            res = (
                supabase.table("ocs")
                .select("oc_id, oc_name, owner_discord_id, avatar_url")
                .eq("oc_id", oc_id)
                .limit(1)
                .execute()
            )
            rows = getattr(res, "data", None) or []
            return rows[0] if rows else None
        except Exception as e:
            print(f"[_get_oc_by_id tokens] Exception fetching OC {oc_id}: {e}")
            return None

    # ---------- OC AUTOCOMPLETE (GLOBAL) ----------
    async def _oc_autocomplete_global(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        """
        Global OC autocomplete: show any OC in the registry whose name matches `current`.
        Ignores ownership so everyone can see everyone's OCs in the dropdown.
        """
        try:
            supabase = self._supabase()
        except Exception as e:
            print(f"[token_oc_autocomplete_global] supabase access error: {e}")
            return []

        try:
            query = supabase.table("ocs").select("oc_name")
            if current:
                query = query.ilike("oc_name", f"%{current}%")
            res = query.order("oc_name").limit(25).execute()
            rows = getattr(res, "data", None) or []
        except Exception as e:
            print(f"[token_oc_autocomplete_global] SELECT exception: {e}")
            return []

        choices: List[app_commands.Choice[str]] = []
        for row in rows:
            name = row.get("oc_name")
            if not name:
                continue
            choices.append(app_commands.Choice(name=name, value=name))
        return choices

    # ---------- staff: grant / revoke / fine ----------
    @app_commands.command(name="token_grant", description="STAFF: grant Tokens to an OC")
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(player="Owner of the OC", oc_name="Exact OC name", amount="Positive amount")
    @app_commands.autocomplete(oc_name=_oc_autocomplete_global)
    async def token_grant(self, interaction: discord.Interaction, player: discord.User, oc_name: str, amount: int):
        await interaction.response.defer(ephemeral=False)

        if not isinstance(interaction.user, discord.Member) or not can_manage_tokens(interaction.user):
            return await interaction.followup.send("❌ You don't have permission.", ephemeral=False)
        if amount <= 0:
            return await interaction.followup.send("Amount must be positive.", ephemeral=False)

        is_valid, clean_oc_name = self._validate_oc_name(oc_name)
        if not is_valid:
            return await interaction.followup.send("Invalid OC name provided.", ephemeral=False)

        try:
            supabase = self._supabase()
        except Exception as e:
            print(f"[token_grant] supabase access error: {e}")
            return await interaction.followup.send("Server configuration error.", ephemeral=False)

        oc_id = await self._resolve_oc_id(supabase, str(player.id), clean_oc_name)
        if not oc_id:
            return await interaction.followup.send("OC not found.", ephemeral=False)

        # Fetch OC for canonical name + avatar
        oc_row = await self._get_oc_by_id(supabase, oc_id)
        display_oc_name = oc_row.get("oc_name", clean_oc_name) if oc_row else clean_oc_name
        avatar_url = oc_row.get("avatar_url") if oc_row else None

        payload = {
            "p_oc_id": oc_id,
            "p_delta": amount,
            "p_reason": "grant",
            "p_ctx": {"by": str(interaction.user.id), "msg": str(interaction.id)},
        }
        print(f"[token_grant] payload: {json.dumps(payload, ensure_ascii=False)}")

        try:
            res = supabase.rpc("token_adjust", payload).execute()
            print(f"[token_grant] raw response: {res}")
            if hasattr(res, "error") and res.error:
                print(f"[token_grant] RPC error: {res.error}")
                return await interaction.followup.send(
                    f"DB error while granting Tokens: `{res.error}`",
                    ephemeral=True,
                )
        except Exception as e:
            print(f"[token_grant] RPC exception: {e}")
            return await interaction.followup.send(
                f"Could not grant Tokens (RPC exception: `{e}`)",
                ephemeral=True,
            )

        data_container = getattr(res, "data", None) or [{}]
        data = data_container[0] if isinstance(data_container, list) else data_container
        new_bal = data.get("new_balance", "—")

        embed = discord.Embed(
            title=f"{TOKEN_EMOJI} Tokens Granted",
            description=f"OC **{display_oc_name}** • Owner <@{player.id}>\nAmount: **+{amount}** {TOKEN_EMOJI}",
            color=discord.Color.green(),
        )
        embed.add_field(name="New Balance", value=f"{new_bal} {TOKEN_EMOJI}")
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)
        await interaction.followup.send(embed=embed, ephemeral=False)

    @app_commands.command(name="token_revoke", description="STAFF: revoke Tokens from an OC")
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(player="Owner of the OC", oc_name="Exact OC name", amount="Positive amount")
    @app_commands.autocomplete(oc_name=_oc_autocomplete_global)
    async def token_revoke(self, interaction: discord.Interaction, player: discord.User, oc_name: str, amount: int):
        await interaction.response.defer(ephemeral=False)

        if not isinstance(interaction.user, discord.Member) or not can_manage_tokens(interaction.user):
            return await interaction.followup.send("❌ You don't have permission.", ephemeral=False)
        if amount <= 0:
            return await interaction.followup.send("Amount must be positive.", ephemeral=False)

        is_valid, clean_oc_name = self._validate_oc_name(oc_name)
        if not is_valid:
            return await interaction.followup.send("Invalid OC name provided.", ephemeral=False)

        try:
            supabase = self._supabase()
        except Exception as e:
            print(f"[token_revoke] supabase access error: {e}")
            return await interaction.followup.send("Server configuration error.", ephemeral=False)

        oc_id = await self._resolve_oc_id(supabase, str(player.id), clean_oc_name)
        if not oc_id:
            return await interaction.followup.send("OC not found.", ephemeral=False)

        oc_row = await self._get_oc_by_id(supabase, oc_id)
        display_oc_name = oc_row.get("oc_name", clean_oc_name) if oc_row else clean_oc_name
        avatar_url = oc_row.get("avatar_url") if oc_row else None

        payload = {
            "p_oc_id": oc_id,
            "p_delta": -abs(amount),
            "p_reason": "revoke",
            "p_ctx": {"by": str(interaction.user.id), "msg": str(interaction.id)},
        }
        print(f"[token_revoke] payload: {json.dumps(payload, ensure_ascii=False)}")

        try:
            res = supabase.rpc("token_adjust", payload).execute()
            print(f"[token_revoke] raw response: {res}")
            if hasattr(res, "error") and res.error:
                print(f"[token_revoke] RPC error: {res.error}")
                return await interaction.followup.send(
                    f"DB error while revoking Tokens: `{res.error}`",
                    ephemeral=True,
                )
        except Exception as e:
            print(f"[token_revoke] RPC exception: {e}")
            return await interaction.followup.send(
                f"Could not revoke Tokens (RPC exception: `{e}`)",
                ephemeral=True,
            )

        data_container = getattr(res, "data", None) or [{}]
        data = data_container[0] if isinstance(data_container, list) else data_container

        embed = discord.Embed(
            title=f"{TOKEN_EMOJI} Tokens Revoked",
            description=f"OC **{display_oc_name}** • Owner <@{player.id}>\nAmount: **-{amount}** {TOKEN_EMOJI}",
            color=discord.Color.red(),
        )
        embed.add_field(name="New Balance", value=f"{data.get('new_balance','—')} {TOKEN_EMOJI}")
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)
        await interaction.followup.send(embed=embed, ephemeral=False)

    @app_commands.command(name="token_fine", description="STAFF: fine an OC (deduct Tokens)")
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(
        player="Owner of the OC",
        oc_name="Exact OC name",
        amount="Positive amount",
        note="Reason",
    )
    @app_commands.autocomplete(oc_name=_oc_autocomplete_global)
    async def token_fine(
        self,
        interaction: discord.Interaction,
        player: discord.User,
        oc_name: str,
        amount: int,
        note: Optional[str] = None,
    ):
        await interaction.response.defer(ephemeral=False)

        if not isinstance(interaction.user, discord.Member) or not can_manage_tokens(interaction.user):
            return await interaction.followup.send("❌ You don't have permission.", ephemeral=False)
        if amount <= 0:
            return await interaction.followup.send("Amount must be positive.", ephemeral=False)

        is_valid, clean_oc_name = self._validate_oc_name(oc_name)
        if not is_valid:
            return await interaction.followup.send("Invalid OC name provided.", ephemeral=False)

        try:
            supabase = self._supabase()
        except Exception as e:
            print(f"[token_fine] supabase access error: {e}")
            return await interaction.followup.send("Server configuration error.", ephemeral=False)

        oc_id = await self._resolve_oc_id(supabase, str(player.id), clean_oc_name)
        if not oc_id:
            return await interaction.followup.send("OC not found.", ephemeral=False)

        oc_row = await self._get_oc_by_id(supabase, oc_id)
        display_oc_name = oc_row.get("oc_name", clean_oc_name) if oc_row else clean_oc_name
        avatar_url = oc_row.get("avatar_url") if oc_row else None

        payload = {
            "p_oc_id": oc_id,
            "p_delta": -abs(amount),
            "p_reason": "fine",
            "p_ctx": {"by": str(interaction.user.id), "msg": str(interaction.id), "note": note or ""},
        }
        print(f"[token_fine] payload: {json.dumps(payload, ensure_ascii=False)}")

        try:
            res = supabase.rpc("token_adjust", payload).execute()
            print(f"[token_fine] raw response: {res}")
            if hasattr(res, "error") and res.error:
                print(f"[token_fine] RPC error: {res.error}")
                return await interaction.followup.send(
                    f"DB error while fining Tokens: `{res.error}`",
                    ephemeral=True,
                )
        except Exception as e:
            print(f"[token_fine] RPC exception: {e}")
            return await interaction.followup.send(
                f"Could not apply fine (RPC exception: `{e}`)",
                ephemeral=True,
            )

        data_container = getattr(res, "data", None) or [{}]
        data = data_container[0] if isinstance(data_container, list) else data_container

        embed = discord.Embed(
            title=f"{TOKEN_EMOJI} Token Fine",
            description=(
                f"OC **{display_oc_name}** • Owner <@{player.id}>\n"
                f"Fine: **-{amount}** {TOKEN_EMOJI}\n"
                f"{('Reason: ' + note) if note else ''}"
            ),
            color=discord.Color.orange(),
        )
        embed.add_field(name="New Balance", value=f"{data.get('new_balance','—')} {TOKEN_EMOJI}")
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)
        await interaction.followup.send(embed=embed, ephemeral=False)

    # ---------- players: pay / history ----------
    @app_commands.command(name="token_pay", description="Donate Tokens from one of your OCs to another player's OC")
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(
        from_oc="Your OC name (payer)",
        to_player="Recipient player",
        to_oc="Recipient OC name",
        amount="Positive amount to donate"
    )
    @app_commands.autocomplete(
        from_oc=_oc_autocomplete_global,
        to_oc=_oc_autocomplete_global,
    )
    async def token_pay(
        self,
        interaction: discord.Interaction,
        from_oc: str,
        to_player: discord.User,
        to_oc: str,
        amount: int,
    ):
        await interaction.response.defer(ephemeral=False)
        if amount <= 0:
            return await interaction.followup.send("Amount must be positive.", ephemeral=False)

        try:
            supabase = self._supabase()
        except Exception as e:
            print(f"[token_pay] supabase access error: {e}")
            return await interaction.followup.send("Server configuration error.", ephemeral=False)

        from_oc_id = await self._resolve_oc_id(supabase, str(interaction.user.id), from_oc.strip())
        if not from_oc_id:
            return await interaction.followup.send("Your payer OC was not found.", ephemeral=False)

        to_oc_id = await self._resolve_oc_id(supabase, str(to_player.id), to_oc.strip())
        if not to_oc_id:
            return await interaction.followup.send("Recipient OC was not found.", ephemeral=False)

        # Fetch OCs for names (and maybe art later if you want)
        from_row = await self._get_oc_by_id(supabase, from_oc_id)
        to_row = await self._get_oc_by_id(supabase, to_oc_id)

        from_display = from_row.get("oc_name", from_oc) if from_row else from_oc
        to_display = to_row.get("oc_name", to_oc) if to_row else to_oc

        thumb_url = None
        if from_row and from_row.get("avatar_url"):
            thumb_url = from_row["avatar_url"]
        elif to_row and to_row.get("avatar_url"):
            thumb_url = to_row["avatar_url"]

        payload = {
            "p_from_oc": from_oc_id,
            "p_to_oc": to_oc_id,
            "p_amount": amount,
            "p_reason": "donation",
            "p_ctx": {"by": str(interaction.user.id), "msg": str(interaction.id), "to": str(to_player.id)},
        }
        print(f"[token_pay] payload: {json.dumps(payload, ensure_ascii=False)}")

        try:
            res = supabase.rpc("token_transfer", payload).execute()
            print(f"[token_pay] raw response: {res}")
            if hasattr(res, "error") and res.error:
                print(f"[token_pay] RPC error: {res.error}")
                return await interaction.followup.send(
                    f"DB error while transferring Tokens: `{res.error}`",
                    ephemeral=True,
                )
        except Exception as e:
            print(f"[token_pay] RPC exception: {e}")
            return await interaction.followup.send(
                f"Could not transfer Tokens (RPC exception: `{e}`)",
                ephemeral=True,
            )

        data_container = getattr(res, "data", None) or [{}]
        data = data_container[0] if isinstance(data_container, list) else data_container
        from_bal = data.get("from_new_balance", "—")
        to_bal = data.get("to_new_balance", "—")

        embed = discord.Embed(
            title=f"{TOKEN_EMOJI} Token Donation",
            description=(
                f"**{from_display}** → **{to_display}**\n"
                f"Donor: <@{interaction.user.id}> • Recipient: <@{to_player.id}>\n"
                f"Amount: **{amount}** {TOKEN_EMOJI}"
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Your New Balance", value=f"{from_bal} {TOKEN_EMOJI}")
        embed.add_field(name="Recipient New Balance", value=f"{to_bal} {TOKEN_EMOJI}")
        if thumb_url:
            embed.set_thumbnail(url=thumb_url)
        await interaction.followup.send(embed=embed, ephemeral=False)

    @app_commands.command(name="token_history", description="Show recent Token transactions for an OC")
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(oc_name="OC name", limit="Number of entries (max 25, default 10)")
    @app_commands.autocomplete(oc_name=_oc_autocomplete_global)
    async def token_history(self, interaction: discord.Interaction, oc_name: str, limit: Optional[int] = 10):
        await interaction.response.defer(ephemeral=False)
        limit = max(1, min(25, limit or 10))

        try:
            supabase = self._supabase()
        except Exception as e:
            print(f"[token_history] supabase access error: {e}")
            return await interaction.followup.send("Server configuration error.", ephemeral=False)

        oc_id = await self._resolve_oc_id(supabase, str(interaction.user.id), oc_name.strip())
        if not oc_id:
            return await interaction.followup.send("OC not found.", ephemeral=False)

        # For nicer title + art
        oc_row = await self._get_oc_by_id(supabase, oc_id)
        display_name = oc_row.get("oc_name", oc_name) if oc_row else oc_name
        avatar_url = oc_row.get("avatar_url") if oc_row else None

        try:
            tx = (
                supabase.table("token_tx")
                .select("delta, reason, created_at, ctx")
                .eq("oc_id", oc_id)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            rows = getattr(tx, "data", None) or []
        except Exception as e:
            print(f"[token_history] SELECT exception: {e}")
            return await interaction.followup.send("Could not fetch history (DB error).", ephemeral=False)

        if not rows:
            return await interaction.followup.send("No transactions yet.", ephemeral=False)

        embed = discord.Embed(
            title=f"📜 Token History • {display_name}",
            color=discord.Color.dark_gold(),
        )
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

        for r in rows:
            delta = r["delta"]
            sign = "＋" if delta > 0 else "－"
            when = r["created_at"]
            reason = r.get("reason", "txn")

            try:
                timestamp = int(discord.utils.parse_time(when).timestamp())
                time_str = f"<t:{timestamp}:R>"
            except (ValueError, TypeError, AttributeError) as e:
                print(f"[token_history] Failed to parse timestamp '{when}': {e}")
                time_str = str(when)

            embed.add_field(
                name=f"{sign}{abs(delta)} {TOKEN_EMOJI} • {reason}",
                value=time_str,
                inline=False
            )

        await interaction.followup.send(embed=embed, ephemeral=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(Tokens(bot))
