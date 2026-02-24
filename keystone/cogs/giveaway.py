import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import re
import random
import os
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Set
from io import BytesIO

CLAIM_WINDOW_HOURS = 48  # how long winners have to claim by reacting

# 🔹 Guild-scope: Skyfall RP only
SKYFALL_GUILD_ID = 1374730886234374235
SKYFALL_GUILD = discord.Object(id=SKYFALL_GUILD_ID)

# Rate-limit safety for entry display edits
ENTRY_DISPLAY_COOLDOWN_SECONDS = 45  # per giveaway (debounce + cooldown)


class GiveawayManager:
    """Handles giveaway storage and state management"""
    def __init__(self):
        # key = giveaway message id
        self.active_giveaways: Dict[int, Dict] = {}
        self.entry_messages: Dict[int, int] = {}

        # current participant set (kept in memory)
        self.participants: Dict[int, Set[int]] = {}

        # manual overrides
        self.manual_additions: Dict[int, Set[int]] = {}
        self.manual_exclusions: Dict[int, Set[int]] = {}

        # claim tracking (key = giveaway message id)
        self.claims: Dict[int, Dict] = {}

        # debounce bookkeeping
        self._entry_update_scheduled: Set[int] = set()
        self._last_entry_update_at: Dict[int, datetime] = {}

        # one-time “hydrate from reactions” bookkeeping
        self._hydrating: Set[int] = set()


manager = GiveawayManager()


class Giveaway(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guild_id = int(os.getenv("GUILD_ID", 0)) or None

        self.giveaway_check.start()

        # Reload any active giveaways from Supabase on startup
        bot.loop.create_task(self._load_active_giveaways())

        print(f"🎉 Giveaway system initialized for guild {self.guild_id}")

    def cog_unload(self):
        self.giveaway_check.cancel()
        print("🔴 Giveaway system stopped")

    # === Supabase helpers ===

    def _get_supabase_client(self):
        """Shared helper to get Supabase client (same logic as in logger)."""
        try:
            from cogs.oc_register import get_supabase_client  # type: ignore
            return get_supabase_client()
        except Exception:
            from supabase import create_client  # type: ignore

            url = (os.getenv("SUPABASE_URL") or "").strip()
            key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
            if not url or not key:
                raise RuntimeError("Supabase creds missing")
            return create_client(url, key)

    async def _save_active_giveaway(self, giveaway_id: int):
        """Upsert an active giveaway row in Supabase so it survives restarts."""
        try:
            supabase = self._get_supabase_client()
            g = manager.active_giveaways.get(giveaway_id)
            if not g:
                return

            data = {
                "message_id": giveaway_id,
                "guild_id": g["guild_id"],
                "channel_id": g["channel_id"],
                "item": g["item"],
                "winners": g["winners"],
                "host_id": g.get("host_id", 0),
                "end_time": g["end_time"].isoformat(),
            }

            supabase.table("giveaways_active").upsert(
                data, on_conflict="message_id"
            ).execute()
        except Exception as e:
            print(f"⚠️ Failed to save active giveaway {giveaway_id}: {e}")

    async def _delete_active_giveaway(self, giveaway_id: int):
        """Remove a giveaway from the active table when it’s finished/stopped."""
        try:
            supabase = self._get_supabase_client()
            supabase.table("giveaways_active").delete().eq(
                "message_id", giveaway_id
            ).execute()
        except Exception as e:
            print(f"⚠️ Failed to delete active giveaway {giveaway_id}: {e}")

    async def _load_active_giveaways(self):
        """On bot startup, reload active giveaways from Supabase into memory."""
        await self.bot.wait_until_ready()
        try:
            supabase = self._get_supabase_client()
            now_iso = datetime.now(timezone.utc).isoformat()

            res = (
                supabase.table("giveaways_active")
                .select("*")
                .gt("end_time", now_iso)
                .execute()
            )

            rows = getattr(res, "data", res)
            for row in rows:
                try:
                    msg_id = int(row["message_id"])
                    end_time_raw = row["end_time"]
                    if isinstance(end_time_raw, str) and end_time_raw.endswith("Z"):
                        end_time_raw = end_time_raw.replace("Z", "+00:00")
                    end_time = datetime.fromisoformat(end_time_raw)

                    manager.active_giveaways[msg_id] = {
                        "item": row["item"],
                        "end_time": end_time,
                        "winners": int(row["winners"]),
                        "channel_id": int(row["channel_id"]),
                        "message_id": msg_id,
                        "guild_id": int(row["guild_id"]),
                        "host_id": int(row.get("host_id", 0)),
                    }

                    manager.participants.setdefault(msg_id, set())
                    manager.manual_additions.setdefault(msg_id, set())
                    manager.manual_exclusions.setdefault(msg_id, set())

                    print(f"🔁 Restored active giveaway {msg_id} ({row['item']})")

                    # One-time hydrate from reactions so we don’t miss entrants after restart
                    # (this is a single scan, not a loop)
                    await self._hydrate_participants_from_reactions(msg_id)

                    # Then update the entry display (debounced)
                    self._schedule_entry_display_update(msg_id)

                    # small sleep to be gentle if multiple giveaways restore
                    await asyncio.sleep(1.0)

                except Exception as inner:
                    print(f"⚠️ Error restoring giveaway row {row}: {inner}")

        except Exception as e:
            print(f"⚠️ Failed to load active giveaways: {e}")

    # === Supabase audit log helper ===
    def _log_giveaway_action(
        self,
        *,
        staff_id: int,
        action: str,               # "stop" | "add" | "remove" | "reroll" | etc.
        message_id: int,
        item_name: str = "",
        target_ids: Optional[List[int]] = None,
        amount: Optional[int] = None,
        extra_context: str = ""
    ):
        """Writes an audit row to the `giveaway_logs` table."""
        try:
            supabase = self._get_supabase_client()
            receivers = ",".join(str(x) for x in (target_ids or []))
            ctx = f"action={action}; message_id={message_id}; {extra_context}".strip("; ")

            (
                supabase.table("giveaway_logs")
                .insert(
                    {
                        "staff": str(staff_id),
                        "receiver": receivers,
                        "item_name": item_name or "",
                        "amount": amount if amount is not None else 0,
                        "context": ctx,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )
                .execute()
            )
        except Exception as e:
            print(f"[giveaway_logs] log fail: {e}")

    def _extract_previous_winner_ids(self, embed: discord.Embed) -> Set[int]:
        ids: Set[int] = set()
        try:
            for field in embed.fields:
                if field.name.strip().lower().startswith("🎊 winners") or field.name.strip().lower().startswith("🔁 reroll"):
                    for m in re.finditer(r"<@!?(?P<id>\d+)>", field.value or ""):
                        try:
                            ids.add(int(m.group("id")))
                        except:
                            pass
        except:
            pass
        return ids

    # ======================
    # Background loops
    # ======================

    @tasks.loop(minutes=1.0)
    async def giveaway_check(self):
        now = datetime.now(timezone.utc)
        completed = []

        for giveaway_id, giveaway in list(manager.active_giveaways.items()):
            if now >= giveaway["end_time"]:
                await self._finalize_giveaway(giveaway_id)
                completed.append(giveaway_id)

        for giveaway_id in completed:
            manager.active_giveaways.pop(giveaway_id, None)
            manager.entry_messages.pop(giveaway_id, None)
            manager.participants.pop(giveaway_id, None)
            manager.manual_additions.pop(giveaway_id, None)
            manager.manual_exclusions.pop(giveaway_id, None)
            manager._entry_update_scheduled.discard(giveaway_id)
            manager._last_entry_update_at.pop(giveaway_id, None)
            manager._hydrating.discard(giveaway_id)
            # NOTE: we do NOT clear manager.claims here so claims can still happen

    # ======================
    # Anti-spam entry display updates (debounced)
    # ======================

    def _schedule_entry_display_update(self, giveaway_id: int):
        """Debounce updates so we don’t spam message edits."""
        if giveaway_id in manager._entry_update_scheduled:
            return
        manager._entry_update_scheduled.add(giveaway_id)
        self.bot.loop.create_task(self._debounced_entry_update(giveaway_id))

    async def _debounced_entry_update(self, giveaway_id: int):
        try:
            last = manager._last_entry_update_at.get(giveaway_id)
            now = datetime.now(timezone.utc)
            if last:
                elapsed = (now - last).total_seconds()
                if elapsed < ENTRY_DISPLAY_COOLDOWN_SECONDS:
                    await asyncio.sleep(ENTRY_DISPLAY_COOLDOWN_SECONDS - elapsed)

            await self._update_entry_display(giveaway_id)
            manager._last_entry_update_at[giveaway_id] = datetime.now(timezone.utc)
        finally:
            manager._entry_update_scheduled.discard(giveaway_id)

    async def _hydrate_participants_from_reactions(self, giveaway_id: int):
        """
        One-time scan of 🎉 reaction users for a giveaway message.
        This is used on restore or when staff runs refresh, NOT in a loop.
        """
        if giveaway_id in manager._hydrating:
            return
        manager._hydrating.add(giveaway_id)
        try:
            giveaway = manager.active_giveaways.get(giveaway_id)
            if not giveaway:
                return

            channel = self.bot.get_channel(giveaway["channel_id"])
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                return

            try:
                message = await channel.fetch_message(giveaway["message_id"])
            except Exception:
                return

            reaction = next((r for r in message.reactions if str(r.emoji) == "🎉"), None)
            if not reaction:
                return

            ids: Set[int] = set()
            try:
                async for u in reaction.users(limit=None):
                    if not u.bot:
                        ids.add(u.id)
            except discord.HTTPException as e:
                # If discord blocks temporarily, just skip; staff can refresh later
                if getattr(e, "status", None) == 429:
                    return
                print(f"⚠️ Hydrate reaction scan error: {e}")
                return

            base = manager.participants.setdefault(giveaway_id, set())
            base.update(ids)

        finally:
            manager._hydrating.discard(giveaway_id)

    async def _update_entry_display(self, giveaway_id: int):
        """
        Show entrants safely without spamming Discord.
        Participants are: (participants ∪ manual_additions) − manual_exclusions
        """
        try:
            giveaway = manager.active_giveaways.get(giveaway_id)
            if not giveaway:
                return

            channel = self.bot.get_channel(giveaway["channel_id"])
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                return

            manual_add = manager.manual_additions.setdefault(giveaway_id, set())
            manual_excl = manager.manual_exclusions.setdefault(giveaway_id, set())
            base = manager.participants.setdefault(giveaway_id, set())

            participant_ids = (set(base) | set(manual_add)) - set(manual_excl)
            manager.participants[giveaway_id] = set(participant_ids)

            embed = discord.Embed(
                title=f"🎟️ Entries for {giveaway['item']}",
                color=discord.Color.blurple(),
                timestamp=giveaway["end_time"],
            )

            if not participant_ids:
                embed.description = "No entries yet! React with 🎉 to join."
            else:
                lines = [f"• <@{uid}>" for uid in sorted(participant_ids)]
                total = len(lines)

                chunks: List[str] = []
                current = ""
                for line in lines:
                    if len(current) + len(line) + 1 > 1024:
                        chunks.append(current.rstrip("\n"))
                        current = ""
                    current += line + "\n"
                if current:
                    chunks.append(current.rstrip("\n"))

                embed.add_field(
                    name=f"Total Entries: {total}",
                    value="All entrants listed below.",
                    inline=False,
                )

                # If too long, do NOT auto-send files (that also spams).
                # Staff can use /giveaway_refresh_entries (which can export if you want later).
                if len(chunks) > 25:
                    embed.set_field_at(
                        0,
                        name=f"Total Entries: {total}",
                        value="Entrant list too long to display in embeds.",
                        inline=False,
                    )
                else:
                    for i, chunk in enumerate(chunks, start=1):
                        embed.add_field(name=f"Entries ({i}/{len(chunks)})", value=chunk, inline=False)

            embed.set_footer(text="Updated on join/leave • Ends at")

            if giveaway_id in manager.entry_messages:
                try:
                    entry_msg = await channel.fetch_message(manager.entry_messages[giveaway_id])
                    await entry_msg.edit(embed=embed)
                except discord.NotFound:
                    new_msg = await channel.send(embed=embed)
                    manager.entry_messages[giveaway_id] = new_msg.id
            else:
                new_msg = await channel.send(embed=embed)
                manager.entry_messages[giveaway_id] = new_msg.id

        except discord.HTTPException as e:
            # Don’t spiral on 429
            if getattr(e, "status", None) == 429:
                return
            print(f"⚠️ Error updating entries: {str(e)}")
        except Exception as e:
            print(f"⚠️ Error updating entries: {str(e)}")

    # =======================
    # Reaction listeners
    # =======================

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        # guild lock
        if payload.guild_id != SKYFALL_GUILD_ID:
            return

        # Ignore bot's own reactions
        if self.bot.user and payload.user_id == self.bot.user.id:
            return

        # 🎉 = entry join
        if str(payload.emoji) == "🎉":
            giveaway = manager.active_giveaways.get(payload.message_id)
            if not giveaway:
                return

            base = manager.participants.setdefault(payload.message_id, set())
            base.add(payload.user_id)
            # If staff excluded them, keep them excluded
            # (manual_exclusions wins)
            self._schedule_entry_display_update(payload.message_id)
            return

        # 🎁 = claim prize
        if str(payload.emoji) != "🎁":
            return

        claim_info = manager.claims.get(payload.message_id)
        if not claim_info:
            return

        # too late?
        if datetime.now(timezone.utc) > claim_info["deadline"]:
            return

        if payload.user_id not in claim_info["winners"]:
            return

        if payload.user_id in claim_info["claimed"]:
            return

        claim_info["claimed"].add(payload.user_id)

        channel = self.bot.get_channel(claim_info["channel_id"]) or self.bot.get_channel(payload.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        guild = channel.guild
        try:
            member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
        except Exception:
            member = None

        mention = member.mention if member else f"<@{payload.user_id}>"
        await channel.send(f"✅ {mention} has claimed their **{claim_info['item']}**!")

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        # guild lock
        if payload.guild_id != SKYFALL_GUILD_ID:
            return

        if str(payload.emoji) != "🎉":
            return

        giveaway = manager.active_giveaways.get(payload.message_id)
        if not giveaway:
            return

        base = manager.participants.setdefault(payload.message_id, set())
        base.discard(payload.user_id)
        self._schedule_entry_display_update(payload.message_id)

    # =======================
    # Claim setup + finalize
    # =======================

    async def _setup_claims(
        self,
        *,
        message: discord.Message,
        channel: discord.TextChannel,
        item: str,
        winners: List[discord.Member],
    ):
        """Create claim record + update embed to show claim instructions."""
        if not winners:
            return

        deadline = datetime.now(timezone.utc) + timedelta(hours=CLAIM_WINDOW_HOURS)
        manager.claims[message.id] = {
            "item": item,
            "winners": {m.id for m in winners},
            "claimed": set(),
            "channel_id": channel.id,
            "deadline": deadline,
        }

        # Add claim instructions to the giveaway embed
        try:
            embed = message.embeds[0]
        except IndexError:
            return

        claim_text = (
            f"Winners: react with 🎁 on **this message** within "
            f"{CLAIM_WINDOW_HOURS} hours to claim."
        )
        embed.add_field(name="🎁 Claim Prize", value=claim_text, inline=False)
        await message.edit(embed=embed)

        # Add the claim reaction
        try:
            await message.add_reaction("🎁")
        except discord.Forbidden:
            pass

    async def _finalize_giveaway(self, giveaway_id: int):
        try:
            giveaway = manager.active_giveaways.get(giveaway_id)
            if not giveaway:
                return

            channel = self.bot.get_channel(giveaway["channel_id"])
            if not isinstance(channel, discord.TextChannel):
                return

            # Best-effort hydration right before final draw (one-time scan)
            # Useful if bot missed raw reaction events during downtime
            await self._hydrate_participants_from_reactions(giveaway_id)

            # delete entry message if it exists
            if giveaway_id in manager.entry_messages:
                try:
                    msg = await channel.fetch_message(manager.entry_messages[giveaway_id])
                    await msg.delete()
                except discord.NotFound:
                    pass
                manager.entry_messages.pop(giveaway_id, None)

            message = await channel.fetch_message(giveaway["message_id"])

            base = manager.participants.get(giveaway_id, set())
            manual_add = manager.manual_additions.get(giveaway_id, set())
            manual_excl = manager.manual_exclusions.get(giveaway_id, set())

            participants = list((set(base) | set(manual_add)) - set(manual_excl))

            if participants:
                members: List[discord.Member] = []
                for user_id in participants:
                    try:
                        member = channel.guild.get_member(user_id) or await channel.guild.fetch_member(user_id)
                        if member and not member.bot:
                            members.append(member)
                    except:
                        continue

                if members:
                    winners_count = min(giveaway["winners"], len(members))
                    draw = random.sample(members, winners_count)
                    winner_mentions = ", ".join(m.mention for m in draw)

                    embed = message.embeds[0]
                    embed.color = discord.Color.green()
                    embed.add_field(
                        name="🎊 Winners",
                        value=winner_mentions,
                        inline=False,
                    )
                    await message.edit(embed=embed)

                    # UI-friendly announcement: include item name
                    announce_embed = discord.Embed(
                        title=f"🎉 {giveaway['item']} – Winners!",
                        description=winner_mentions,
                        color=discord.Color.green(),
                        timestamp=datetime.now(timezone.utc),
                    )
                    await channel.send(embed=announce_embed)

                    # Set up claim flow
                    await self._setup_claims(
                        message=message,
                        channel=channel,
                        item=giveaway["item"],
                        winners=draw,
                    )
                    return

            # no valid winners
            embed = message.embeds[0]
            embed.color = discord.Color.red()
            await message.edit(embed=embed)

        except Exception as e:
            print(f"⚠️ Error finalizing giveaway: {str(e)}")
        finally:
            # Always try to remove from active table in Supabase
            try:
                await self._delete_active_giveaway(giveaway_id)
            except Exception:
                pass

    # =======================
    # Slash commands (guild)
    # =======================

    @app_commands.command(
        name="giveaway_start",
        description="Start a new giveaway",
    )
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(
        item="The prize for the giveaway",
        duration="How long the giveaway should run (e.g. 1h, 30m, 2d)",
        winners="Number of winners to draw (1-20)",
        channel="Channel to post in (default: current)",
    )
    async def giveaway_start(
        self,
        interaction: discord.Interaction,
        item: str,
        duration: str,
        winners: app_commands.Range[int, 1, 20] = 1,
        channel: Optional[discord.TextChannel] = None,
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            match = re.match(r"^(\d+)([mhd])$", duration.lower())
            if not match:
                return await interaction.followup.send(
                    "❌ Use duration like 30m, 1h, 2d", ephemeral=True
                )

            amount, unit = int(match.group(1)), match.group(2)
            time_units = {"m": 60, "h": 3600, "d": 86400}
            end_time = datetime.now(timezone.utc) + timedelta(
                seconds=amount * time_units[unit]
            )

            announce_channel = channel or interaction.channel
            if not isinstance(announce_channel, discord.TextChannel):
                return await interaction.followup.send("❌ Please pick a text channel.", ephemeral=True)

            embed = discord.Embed(
                title=f"🎉 {item} Giveaway!",
                description=f"React with 🎉 to enter!\nEnds: <t:{int(end_time.timestamp())}:R>",
                color=discord.Color.gold(),
                timestamp=end_time,
            )
            embed.add_field(name="Hosted by", value=interaction.user.mention)
            embed.add_field(name="Winners", value=str(winners))

            message = await announce_channel.send(embed=embed)
            await message.add_reaction("🎉")

            manager.active_giveaways[message.id] = {
                "item": item,
                "end_time": end_time,
                "winners": winners,
                "channel_id": announce_channel.id,
                "message_id": message.id,
                "guild_id": interaction.guild.id,
                "host_id": interaction.user.id,
            }

            manager.participants.setdefault(message.id, set())
            manager.manual_additions.setdefault(message.id, set())
            manager.manual_exclusions.setdefault(message.id, set())

            # persist active giveaway to Supabase
            await self._save_active_giveaway(message.id)

            # create the entry display once (debounced)
            self._schedule_entry_display_update(message.id)

            await interaction.followup.send(
                f"✅ Giveaway started in {announce_channel.mention}", ephemeral=True
            )

        except discord.Forbidden:
            await interaction.followup.send(
                "❌ Missing permissions in that channel", ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

    # ===== Admin commands: STOP / ADD / REMOVE / REROLL =====

    @app_commands.command(
        name="giveaway_stop",
        description="Stop a running giveaway immediately",
    )
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(
        message_id="The message ID of the giveaway to stop",
        channel="Channel that contains the giveaway message (defaults to current channel)",
    )
    async def giveaway_stop(
        self,
        interaction: discord.Interaction,
        message_id: str,
        channel: Optional[discord.TextChannel] = None,
    ):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                "❌ You need Manage Server to stop a giveaway.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        try:
            target_channel = channel or interaction.channel
            if not isinstance(target_channel, discord.TextChannel):
                return await interaction.followup.send("❌ Please run this in or specify a text channel.", ephemeral=True)

            try:
                mid = int(message_id)
            except ValueError:
                return await interaction.followup.send(
                    "❌ `message_id` must be a number.", ephemeral=True
                )

            giveaway = manager.active_giveaways.get(mid)
            if not giveaway:
                try:
                    _ = await target_channel.fetch_message(mid)
                except discord.NotFound:
                    return await interaction.followup.send(
                        "❌ I can't find a giveaway with that message ID here.",
                        ephemeral=True,
                    )
                manager.active_giveaways[mid] = {
                    "item": "Giveaway",
                    "end_time": datetime.now(timezone.utc),
                    "winners": 1,
                    "channel_id": target_channel.id,
                    "message_id": mid,
                    "guild_id": interaction.guild.id,
                    "host_id": interaction.user.id,
                }
                manager.participants.setdefault(mid, set())
                manager.manual_additions.setdefault(mid, set())
                manager.manual_exclusions.setdefault(mid, set())

            manager.active_giveaways[mid]["end_time"] = datetime.now(timezone.utc)
            item_name = manager.active_giveaways.get(mid, {}).get("item", "")

            await self._finalize_giveaway(mid)

            manager.active_giveaways.pop(mid, None)
            manager.entry_messages.pop(mid, None)
            manager.participants.pop(mid, None)
            manager.manual_additions.pop(mid, None)
            manager.manual_exclusions.pop(mid, None)

            self._log_giveaway_action(
                staff_id=interaction.user.id,
                action="stop",
                message_id=mid,
                item_name=item_name,
                target_ids=[],
                amount=0,
                extra_context=f"channel_id={target_channel.id}",
            )

            await interaction.followup.send(
                "🛑 Giveaway stopped and finalized.", ephemeral=True
            )

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

    @app_commands.command(
        name="giveaway_add",
        description="Add entrants to a giveaway",
    )
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(
        message_id="The message ID of the giveaway",
        member="Single member to add",
        role="Or a role whose members should be added",
        channel="Channel containing the giveaway (defaults to current channel)",
    )
    async def giveaway_add(
        self,
        interaction: discord.Interaction,
        message_id: str,
        member: Optional[discord.Member] = None,
        role: Optional[discord.Role] = None,
        channel: Optional[discord.TextChannel] = None,
    ):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                "❌ You need Manage Server to add entrants.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        try:
            if not member and not role:
                return await interaction.followup.send(
                    "❌ Provide either a `member` or a `role`.", ephemeral=True
                )

            target_channel = channel or interaction.channel
            if not isinstance(target_channel, discord.TextChannel):
                return await interaction.followup.send("❌ Please run this in or specify a text channel.", ephemeral=True)

            try:
                mid = int(message_id)
            except ValueError:
                return await interaction.followup.send(
                    "❌ `message_id` must be a number.", ephemeral=True
                )

            try:
                await target_channel.fetch_message(mid)
            except discord.NotFound:
                return await interaction.followup.send(
                    "❌ I can't find that message in the specified channel.",
                    ephemeral=True,
                )

            manual_add = manager.manual_additions.setdefault(mid, set())
            manual_excl = manager.manual_exclusions.setdefault(mid, set())
            manager.participants.setdefault(mid, set())

            added_mentions: List[str] = []
            added_ids: List[int] = []

            if member and not member.bot:
                if member.id not in manual_add:
                    manual_add.add(member.id)
                    manual_excl.discard(member.id)
                    added_mentions.append(member.mention)
                    added_ids.append(member.id)

            if role:
                count = 0
                for m in role.members:
                    if not m.bot and m.id not in manual_add:
                        manual_add.add(m.id)
                        manual_excl.discard(m.id)
                        count += 1
                        added_ids.append(m.id)
                if count:
                    added_mentions.append(f"{count} from {role.mention}")

            self._schedule_entry_display_update(mid)

            item_name = manager.active_giveaways.get(mid, {}).get("item", "")
            self._log_giveaway_action(
                staff_id=interaction.user.id,
                action="add",
                message_id=mid,
                item_name=item_name,
                target_ids=added_ids,
                amount=len(added_ids),
                extra_context=f"channel_id={target_channel.id}; role={(role.id if role else 'None')}",
            )

            if added_mentions:
                await interaction.followup.send(
                    f"✅ Added: {', '.join(added_mentions)}", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "⚠️ No entrants added (they may already be in).", ephemeral=True
                )

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

    @app_commands.command(
        name="giveaway_remove",
        description="Remove a member from a giveaway (simplified).",
    )
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(
        member="The member to remove from the giveaway",
        item="Name (or part) of the giveaway prize (optional)",
        channel="Channel containing the giveaway (defaults to current channel)",
    )
    async def giveaway_remove(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        item: Optional[str] = None,
        channel: Optional[discord.TextChannel] = None,
    ):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                "❌ You need Manage Server to remove entrants.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            target_channel = channel or interaction.channel
            if not isinstance(target_channel, discord.TextChannel):
                return await interaction.followup.send(
                    "❌ Please run this in or specify a text channel.",
                    ephemeral=True,
                )

            item_query = (item or "").lower().strip()
            candidates: List[tuple[int, Dict]] = []

            for mid, data in manager.active_giveaways.items():
                if data.get("channel_id") != target_channel.id:
                    continue

                giveaway_item = str(data.get("item", ""))
                if item_query and item_query not in giveaway_item.lower():
                    continue

                candidates.append((mid, data))

            if not candidates:
                if item:
                    return await interaction.followup.send(
                        f"❌ I couldn't find any active giveaway in {target_channel.mention} matching `{item}`.",
                        ephemeral=True,
                    )
                else:
                    return await interaction.followup.send(
                        f"❌ I couldn't find any active giveaway in {target_channel.mention}.",
                        ephemeral=True,
                    )

            mid, data = max(candidates, key=lambda pair: pair[0])

            manual_add = manager.manual_additions.setdefault(mid, set())
            manual_excl = manager.manual_exclusions.setdefault(mid, set())
            base = manager.participants.setdefault(mid, set())

            manual_excl.add(member.id)
            manual_add.discard(member.id)
            base.discard(member.id)

            self._schedule_entry_display_update(mid)

            item_name = data.get("item", "")
            self._log_giveaway_action(
                staff_id=interaction.user.id,
                action="remove",
                message_id=mid,
                item_name=item_name,
                target_ids=[member.id],
                amount=1,
                extra_context=f"channel_id={target_channel.id}",
            )

            await interaction.followup.send(
                f"✅ Removed {member.mention} from the **{item_name or 'giveaway'}** in {target_channel.mention}.",
                ephemeral=True,
            )

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

    @app_commands.command(
        name="giveaway_reroll",
        description="Reroll winners for an existing giveaway message",
    )
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(
        message_id="The message ID of the original giveaway embed",
        winners="Number of winners to draw (1-20)",
        channel="Channel containing the giveaway message (defaults to current channel)",
        exclude_previous="Exclude the previous winners if present in the embed",
    )
    async def giveaway_reroll(
        self,
        interaction: discord.Interaction,
        message_id: str,
        winners: app_commands.Range[int, 1, 20] = 1,
        channel: Optional[discord.TextChannel] = None,
        exclude_previous: bool = True,
    ):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                "❌ You need Manage Server to reroll.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        try:
            target_channel = channel or interaction.channel
            if not isinstance(target_channel, discord.TextChannel):
                return await interaction.followup.send(
                    "❌ Please run this in or specify a text channel.", ephemeral=True
                )

            try:
                msg_id_int = int(message_id)
            except ValueError:
                return await interaction.followup.send(
                    "❌ `message_id` must be a number.", ephemeral=True
                )

            try:
                message = await target_channel.fetch_message(msg_id_int)
            except discord.NotFound:
                return await interaction.followup.send(
                    "❌ I couldn't find a message with that ID in the specified channel.",
                    ephemeral=True,
                )

            if not message.embeds:
                return await interaction.followup.send(
                    "❌ That message doesn't appear to be a giveaway embed.",
                    ephemeral=True,
                )

            reaction = next((r for r in message.reactions if str(r.emoji) == "🎉"), None)
            if not reaction:
                return await interaction.followup.send(
                    "❌ I can't find any 🎉 reaction on that message.", ephemeral=True
                )

            users = [user async for user in reaction.users() if not user.bot]
            if not users:
                return await interaction.followup.send(
                    "⚠️ No valid entrants found on that message.", ephemeral=True
                )

            prior_winners: Set[int] = set()
            if exclude_previous:
                prior_winners = self._extract_previous_winner_ids(message.embeds[0])

            members: List[discord.Member] = []
            for u in users:
                try:
                    member = await target_channel.guild.fetch_member(u.id)
                    if u.id not in prior_winners:
                        members.append(member)
                except:
                    continue

            if not members:
                return await interaction.followup.send(
                    "⚠️ No eligible entrants to reroll from (after exclusions).",
                    ephemeral=True,
                )

            pick = min(winners, len(members))
            new_winners = random.sample(members, pick)
            winner_mentions = ", ".join(m.mention for m in new_winners)

            embed = message.embeds[0]
            embed.add_field(
                name=f"🔁 Reroll ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})",
                value=winner_mentions,
                inline=False,
            )
            await message.edit(embed=embed)

            await target_channel.send(
                f"🔁 New winner(s) for this giveaway: {winner_mentions} — congrats!"
            )

            await self._setup_claims(
                message=message,
                channel=target_channel,
                item=(embed.title or "Giveaway").replace("🎉", "").replace("Giveaway!", "").strip(),
                winners=new_winners,
            )

            item_name = ""
            g = manager.active_giveaways.get(message.id)
            if g:
                item_name = g.get("item", "") or item_name
            else:
                if message.embeds and message.embeds[0].title:
                    title = message.embeds[0].title
                    item_name = title.replace("🎉", "").replace("Giveaway!", "").strip()

            self._log_giveaway_action(
                staff_id=interaction.user.id,
                action="reroll",
                message_id=message.id,
                item_name=item_name,
                target_ids=[m.id for m in new_winners],
                amount=len(new_winners),
                extra_context=f"channel_id={target_channel.id}",
            )

            await interaction.followup.send("✅ Reroll complete!", ephemeral=True)

        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I don't have permission to read/edit that message or view reactions in that channel.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Error during reroll: {str(e)}", ephemeral=True)

    # ========= Staff utility: refresh entrants once (no loop) =========

    @app_commands.command(
        name="giveaway_refresh_entries",
        description="One-time refresh of entrant list from 🎉 reactions (no spam).",
    )
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(
        message_id="The giveaway message ID to refresh entrants for",
        channel="Channel containing the giveaway message (defaults to current channel)",
    )
    async def giveaway_refresh_entries(
        self,
        interaction: discord.Interaction,
        message_id: str,
        channel: Optional[discord.TextChannel] = None,
    ):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                "❌ You need Manage Server to refresh entries.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        try:
            target_channel = channel or interaction.channel
            if not isinstance(target_channel, discord.TextChannel):
                return await interaction.followup.send("❌ Please run this in or specify a text channel.", ephemeral=True)

            try:
                mid = int(message_id)
            except ValueError:
                return await interaction.followup.send("❌ `message_id` must be a number.", ephemeral=True)

            if mid not in manager.active_giveaways:
                return await interaction.followup.send(
                    "❌ That giveaway is not in the active list (it may have ended or not been restored).",
                    ephemeral=True,
                )

            await self._hydrate_participants_from_reactions(mid)
            self._schedule_entry_display_update(mid)

            count = len(manager.participants.get(mid, set()) | manager.manual_additions.get(mid, set())) - len(manager.manual_exclusions.get(mid, set()))
            await interaction.followup.send(f"✅ Refreshed entrants. Current count: **{max(count, 0)}**", ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)


async def setup(bot: commands.Bot):
    cog = Giveaway(bot)
    await bot.add_cog(cog)
    print("✅ Giveaway system loaded!")
