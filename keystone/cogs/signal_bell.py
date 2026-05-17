# cogs/signal_bell.py
# Keystone • The Signal Bell
# Detects successful DISBOARD bumps, lets the bumper claim 1 XP or 1 primary currency
# for their active OC, logs to Supabase, and pings Signal Crew when the cooldown ends.

from __future__ import annotations

import os
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import discord
from discord.ext import commands, tasks

from services.currency_service import ensure_wallet, get_primary_currency
from services.economy_service import transfer
from services.oc_service import get_active_character
from services.xp_service import XPDuplicateAwardError, XPService

DISBOARD_BOT_ID = 302050872383242240

SIGNAL_BELL_CHANNEL_ID = int(os.getenv("SIGNAL_BELL_CHANNEL_ID", "0") or "0")
SIGNAL_BELL_ROLE_ID = int(os.getenv("SIGNAL_BELL_ROLE_ID", "0") or "0")
SIGNAL_BELL_REWARD_AMOUNT = int(os.getenv("SIGNAL_BELL_REWARD_AMOUNT", "1") or "1")
SIGNAL_BELL_COOLDOWN_HOURS = int(os.getenv("SIGNAL_BELL_COOLDOWN_HOURS", "2") or "2")


class SignalRewardView(discord.ui.View):
    def __init__(self, cog: "SignalBell", log_id: str, bumper_id: int):
        super().__init__(timeout=60 * 60 * SIGNAL_BELL_COOLDOWN_HOURS)
        self.cog = cog
        self.log_id = str(log_id)
        self.bumper_id = int(bumper_id)

    async def _claim(self, interaction: discord.Interaction, reward_type: str):
        if int(interaction.user.id) != self.bumper_id:
            return await interaction.response.send_message(
                "Only the person who rang the Signal Bell can claim this reward.",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)

        try:
            result = await self.cog.claim_reward(
                guild=interaction.guild,
                user=interaction.user,
                log_id=self.log_id,
                reward_type=reward_type,
            )
        except Exception:
            traceback.print_exc()
            return await interaction.followup.send("Signal reward failed to process.", ephemeral=True)

        if not result.get("ok"):
            return await interaction.followup.send(result.get("message", "Reward could not be claimed."), ephemeral=True)

        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

        return await interaction.followup.send(result["message"], ephemeral=True)

    @discord.ui.button(label="Claim 1 XP", style=discord.ButtonStyle.primary, custom_id="signal_bell:claim_xp")
    async def claim_xp(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._claim(interaction, "xp")

    @discord.ui.button(label="Claim 1 Currency", style=discord.ButtonStyle.success, custom_id="signal_bell:claim_currency")
    async def claim_currency(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._claim(interaction, "currency")


class SignalBell(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ready_check.start()

    def cog_unload(self):
        self.ready_check.cancel()

    def sb(self):
        sb = getattr(self.bot, "supabase", None)
        if sb is None:
            raise RuntimeError("Supabase is not configured on the bot.")
        return sb

    def _is_disboard_success(self, message: discord.Message) -> bool:
        content = (message.content or "").lower()
        embed_parts: list[str] = []
        for embed in message.embeds:
            embed_parts.append(embed.title or "")
            embed_parts.append(embed.description or "")
            for field in embed.fields:
                embed_parts.append(field.name or "")
                embed_parts.append(field.value or "")
        combined = f"{content} {' '.join(embed_parts).lower()}"

        success_phrases = (
            "bump done",
            "bumped",
            "server bumped",
            "bump successful",
            "done bumping",
        )
        return any(phrase in combined for phrase in success_phrases)

    def _get_bumper_id(self, message: discord.Message) -> Optional[int]:
        # discord.py versions differ here; try both common attributes.
        interaction = getattr(message, "interaction", None)
        if interaction and getattr(interaction, "user", None):
            return int(interaction.user.id)

        interaction_metadata = getattr(message, "interaction_metadata", None)
        if interaction_metadata and getattr(interaction_metadata, "user", None):
            return int(interaction_metadata.user.id)

        # Fallback: sometimes DISBOARD mentions the bumper in message content/embed text.
        mentions = [m for m in message.mentions if not m.bot]
        if mentions:
            return int(mentions[0].id)

        return None

    async def _fetch_member_name(self, guild: discord.Guild, user_id: int) -> str:
        member = guild.get_member(user_id)
        if member:
            return member.display_name
        try:
            user = await self.bot.fetch_user(user_id)
            return user.display_name
        except Exception:
            return str(user_id)

    def _insert_log(
        self,
        *,
        guild_id: int,
        channel_id: int,
        bumper_id: int | None,
        bumper_name: str | None,
        bump_message_id: int,
        next_bump_at: datetime,
    ) -> dict[str, Any]:
        row = {
            "guild_id": int(guild_id),
            "channel_id": int(channel_id),
            "bumper_id": int(bumper_id) if bumper_id else None,
            "bumper_name": bumper_name,
            "reward_amount": int(SIGNAL_BELL_REWARD_AMOUNT),
            "bump_message_id": str(bump_message_id),
            "next_bump_at": next_bump_at.isoformat(),
            "source": "disboard",
            "ready_announced": False,
            "reward_claimed": False,
        }
        res = self.sb().table("signal_bell_logs").insert(row).execute()
        data = getattr(res, "data", None) or []
        return data[0] if data else row

    def _get_log(self, log_id: str) -> dict[str, Any] | None:
        res = (
            self.sb()
            .table("signal_bell_logs")
            .select("*")
            .eq("id", str(log_id))
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        return rows[0] if rows else None

    def _mark_claimed(self, log_id: str, reward_type: str, character_id: str):
        self.sb().table("signal_bell_logs").update(
            {
                "reward_claimed": True,
                "reward_type": reward_type,
                "reward_character_id": str(character_id),
                "reward_claimed_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("id", str(log_id)).execute()

    async def claim_reward(
        self,
        *,
        guild: discord.Guild | None,
        user: discord.abc.User,
        log_id: str,
        reward_type: str,
    ) -> dict[str, Any]:
        if guild is None:
            return {"ok": False, "message": "Use this in a server, not DMs."}

        log = self._get_log(log_id)
        if not log:
            return {"ok": False, "message": "I could not find that Signal Bell log."}

        if log.get("reward_claimed"):
            return {"ok": False, "message": "That Signal Bell reward was already claimed."}

        if log.get("bumper_id") and int(log["bumper_id"]) != int(user.id):
            return {"ok": False, "message": "Only the person who rang the Signal Bell can claim this reward."}

        sb = self.sb()
        active = get_active_character(sb, int(user.id))
        if not active:
            return {"ok": False, "message": "No active OC set. Use `/oc select <name>` first, then try again."}

        character_id = str(active["character_id"])
        oc_name = str(active.get("name") or "your active OC")
        amount = int(log.get("reward_amount") or SIGNAL_BELL_REWARD_AMOUNT or 1)

        if reward_type == "xp":
            xp = XPService(sb)
            try:
                xp.award_xp(
                    guild_id=int(guild.id),
                    character_id=character_id,
                    amount=amount,
                    source="staff",
                    title="Signal Bell bump reward",
                    actor_discord_id=int(user.id),
                    external_ref=f"signal_bell:{log_id}",
                    notes="Reward for manually bumping Railbound through DISBOARD.",
                )
            except XPDuplicateAwardError:
                return {"ok": False, "message": "That XP reward was already claimed."}

            self._mark_claimed(log_id, "xp", character_id)
            return {"ok": True, "message": f"✅ **{oc_name}** received `{amount}` XP from the Signal Bell."}

        if reward_type == "currency":
            cur = get_primary_currency(sb, int(guild.id))
            ensure_wallet(sb, character_id, cur["currency_id"])
            transfer(
                sb,
                guild_id=int(guild.id),
                currency_id=cur["currency_id"],
                amount=amount,
                tx_type="mint",
                actor_discord_id=int(user.id),
                from_character_id=None,
                to_character_id=character_id,
                reason="Signal Bell bump reward",
            )

            self._mark_claimed(log_id, "currency", character_id)
            emoji = cur.get("emoji") or ""
            name = cur.get("name") or "currency"
            return {"ok": True, "message": f"✅ **{oc_name}** received {emoji} `{amount}` **{name}** from the Signal Bell."}

        return {"ok": False, "message": "Unknown reward type."}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.id != DISBOARD_BOT_ID:
            return
        if not message.guild:
            return
        if SIGNAL_BELL_CHANNEL_ID and int(message.channel.id) != SIGNAL_BELL_CHANNEL_ID:
            # Keeps random DISBOARD messages elsewhere from triggering the system.
            return
        if not self._is_disboard_success(message):
            return

        next_bump_at = datetime.now(timezone.utc) + timedelta(hours=SIGNAL_BELL_COOLDOWN_HOURS)
        bumper_id = self._get_bumper_id(message)
        bumper_name = await self._fetch_member_name(message.guild, bumper_id) if bumper_id else None

        log = self._insert_log(
            guild_id=int(message.guild.id),
            channel_id=int(message.channel.id),
            bumper_id=bumper_id,
            bumper_name=bumper_name,
            bump_message_id=int(message.id),
            next_bump_at=next_bump_at,
        )
        log_id = str(log.get("id") or "")

        description = (
            ":steam_locomotive: **The Signal Bell rings across the station...**\n"
            "*Railbound has successfully broadcast its signal to distant travelers.*\n\n"
            f"Next signal available <t:{int(next_bump_at.timestamp())}:R>."
        )
        if bumper_id:
            description += f"\n\n<@{bumper_id}>, choose your reward for your active OC:"
            view = SignalRewardView(self, log_id=log_id, bumper_id=bumper_id)
        else:
            description += "\n\nI could not detect who bumped, so no reward button was attached."
            view = None

        await message.channel.send(description, view=view)

    @tasks.loop(minutes=2)
    async def ready_check(self):
        await self.bot.wait_until_ready()
        if not SIGNAL_BELL_CHANNEL_ID:
            return

        now = datetime.now(timezone.utc).isoformat()
        try:
            res = (
                self.sb()
                .table("signal_bell_logs")
                .select("*")
                .eq("ready_announced", False)
                .lte("next_bump_at", now)
                .order("next_bump_at", desc=False)
                .limit(5)
                .execute()
            )
            rows = getattr(res, "data", None) or []
        except Exception:
            traceback.print_exc()
            return

        for row in rows:
            guild = self.bot.get_guild(int(row.get("guild_id") or 0))
            if not guild:
                continue

            channel_id = int(row.get("channel_id") or SIGNAL_BELL_CHANNEL_ID)
            channel = guild.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await guild.fetch_channel(channel_id)
                except Exception:
                    continue

            role_text = f"<@&{SIGNAL_BELL_ROLE_ID}>\n" if SIGNAL_BELL_ROLE_ID else ""
            try:
                await channel.send(
                    f"{role_text}:mega: **The rails are quiet once more.**\n"
                    "*The Signal Bell is ready to ring again.*"
                )
                self.sb().table("signal_bell_logs").update({"ready_announced": True}).eq("id", row["id"]).execute()
            except Exception:
                traceback.print_exc()

    @ready_check.before_loop
    async def before_ready_check(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(SignalBell(bot))
