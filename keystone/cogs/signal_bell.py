# cogs/signal_bell.py

import os
import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta, timezone

DISBOARD_BOT_ID = 302050872383242240

SIGNAL_BELL_CHANNEL_ID = int(os.getenv("SIGNAL_BELL_CHANNEL_ID", "0"))
SIGNAL_BELL_ROLE_ID = int(os.getenv("SIGNAL_BELL_ROLE_ID", "0"))
SIGNAL_BELL_REWARD_AMOUNT = int(os.getenv("SIGNAL_BELL_REWARD_AMOUNT", "1"))


class SignalBell(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.ready_check.start()

    def cog_unload(self):
        self.ready_check.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.id != DISBOARD_BOT_ID:
            return

        content = message.content.lower()
        embed_text = " ".join(
            [
                e.title or "" for e in message.embeds
            ] + [
                e.description or "" for e in message.embeds
            ]
        ).lower()

        combined = f"{content} {embed_text}"

        if "bump done" not in combined and "bumped" not in combined:
            return

        next_bump_at = datetime.now(timezone.utc) + timedelta(hours=2)

        channel = message.channel

        await channel.send(
            ":steam_locomotive: **The Signal Bell rings across the station...**\n"
            "*Railbound has successfully broadcast its signal to distant travelers.*\n\n"
            f"Next signal available <t:{int(next_bump_at.timestamp())}:R>."
        )

        # Supabase logging + rewards will go here next.

    @tasks.loop(minutes=5)
    async def ready_check(self):
        # Later: check latest bump log and ping when cooldown expires.
        pass

    @ready_check.before_loop
    async def before_ready_check(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(SignalBell(bot))