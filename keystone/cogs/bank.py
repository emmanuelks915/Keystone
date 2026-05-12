import os
import traceback
import discord
from discord import app_commands
from discord.ext import commands

from services.currency_service import get_primary_currency, ensure_wallet
from services.oc_service import get_active_character
from services.economy_service import apply_company_transaction

LEDGER_CHANNEL_ID = int(os.getenv("LEDGER_CHANNEL_ID", "1473718167929880791"))


def _parse_dev_ids() -> set[int]:
    raw = (os.getenv("DEV_USER_IDS") or "").strip()
    if not raw:
        return set()
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


def _is_dev(user_id: int) -> bool:
    return user_id in _parse_dev_ids()


def _has_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False
    if isinstance(interaction.user, discord.Member):
        return interaction.user.guild_permissions.administrator
    return False


class BankCog(commands.GroupCog, group_name="bank", group_description="Company / bank accounts"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    def sb(self):
        sb = getattr(self.bot, "supabase", None)
        if sb is None:
            raise RuntimeError("Supabase is not configured on the bot.")
        return sb

    def _staff_ok(self, interaction: discord.Interaction) -> bool:
        return _has_admin(interaction) or _is_dev(interaction.user.id)

    async def _private_err(self, interaction: discord.Interaction, msg: str):
        if interaction.response.is_done():
            return await interaction.followup.send(msg, ephemeral=True)
        return await interaction.response.send_message(msg, ephemeral=True)

    async def _public_ok(self, interaction: discord.Interaction, content=None, embed=None):
        if interaction.response.is_done():
            return await interaction.followup.send(content=content, embed=embed)
        return await interaction.response.send_message(content=content, embed=embed)

    async def _post_ledger(self, interaction: discord.Interaction, embed: discord.Embed):
        """Best-effort: post to ledger channel without blocking command."""
        if not interaction.guild:
            return
        channel = interaction.guild.get_channel(LEDGER_CHANNEL_ID)
        if channel is None:
            try:
                channel = await interaction.guild.fetch_channel(LEDGER_CHANNEL_ID)
            except Exception:
                return
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            try:
                await channel.send(embed=embed)
            except Exception:
                pass

    async def _bank_autocomplete(self, interaction: discord.Interaction, current: str):
        if not interaction.guild:
            return []
        sb = self.sb()
        guild_id = int(interaction.guild.id)
        q = (current or "").lower().strip()

        res = sb.table("companies").select("company_id,name").eq("guild_id", guild_id).execute()
        rows = getattr(res, "data", None) or []

        out: list[app_commands.Choice[str]] = []
        for r in rows:
            name = str(r.get("name") or "")
            if q and q not in name.lower():
                continue
            out.append(app_commands.Choice(name=name[:100], value=str(r["company_id"])))
        return out[:25]

    def _role_rank(self, role: str | None) -> int:
        r = (role or "").upper()
        return {"OWNER": 3, "MANAGER": 2, "TELLER": 1}.get(r, 0)

    def _require_rank(self, have: int, need: int) -> bool:
        return have >= need

    def _get_member_rank(self, sb, company_id: str, discord_id: int) -> int:
        res = (
            sb.table("company_members")
            .select("role")
            .eq("company_id", company_id)
            .eq("discord_id", int(discord_id))
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        if not rows:
            return 0
        return self._role_rank(rows[0].get("role"))

    def _get_bank_name(self, sb, bank_id: str) -> str:
        res = sb.table("companies").select("name").eq("company_id", bank_id).limit(1).execute()
        rows = getattr(res, "data", None) or []
        return str(rows[0].get("name") or "Bank") if rows else "Bank"

    def _ensure_company_wallet(self, sb, company_id: str, currency_id: str):
        """
        Ensure company_wallets row exists WITHOUT throwing duplicates AND WITHOUT overwriting balance.
        IMPORTANT: Do NOT include 'balance' in the upsert payload.
        """
        sb.table("company_wallets").upsert(
            {"company_id": company_id, "currency_id": currency_id},
            on_conflict="company_id,currency_id",
        ).execute()

    # ─────────────────────────────────────────────────────────────
    # /bank create, /bank list, /bank balance
    # ─────────────────────────────────────────────────────────────

    @app_commands.command(name="create", description="Staff: Create a bank/company")
    @app_commands.describe(name="Bank name")
    async def create(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer()

        if not self._staff_ok(interaction):
            return await self._private_err(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private_err(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        name = name.strip()
        if not name:
            return await self._private_err(interaction, "Name can’t be empty.")

        try:
            ins = sb.table("companies").insert({"guild_id": guild_id, "name": name}).execute()
            row = (getattr(ins, "data", None) or [None])[0]
            if not row:
                return await self._private_err(interaction, "Failed to create bank.")

            # Auto-add creator as OWNER
            sb.table("company_members").insert(
                {"company_id": row["company_id"], "discord_id": int(interaction.user.id), "role": "OWNER"}
            ).execute()

            ledger = discord.Embed(
                title="Ledger • Bank Created",
                description=f"🏦 **{name}**",
                color=discord.Color.green(),
            )
            ledger.add_field(name="By", value=f"`{interaction.user}`", inline=False)
            ledger.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, ledger)

            return await self._public_ok(interaction, content=f"🏦 Created bank **{name}**")

        except Exception as e:
            print(f"[bank create] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error creating bank.")

    @app_commands.command(name="list", description="List banks/companies in this server")
    async def list(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if not interaction.guild:
            return await self._private_err(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            res = sb.table("companies").select("company_id,name").eq("guild_id", guild_id).execute()
            rows = getattr(res, "data", None) or []
            if not rows:
                return await self._public_ok(
                    interaction,
                    content="No banks yet. Staff can create one with `/bank create`.",
                )

            rows.sort(key=lambda r: str(r.get("name") or ""))

            embed = discord.Embed(title="Banks", color=discord.Color.blurple())
            embed.description = "\n".join([f"- **{r['name']}**" for r in rows])
            return await self._public_ok(interaction, embed=embed)

        except Exception as e:
            print(f"[bank list] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error listing banks.")

    @app_commands.command(name="balance", description="Show a bank's balance (primary currency)")
    @app_commands.describe(bank="Which bank")
    @app_commands.autocomplete(bank=_bank_autocomplete)
    async def balance(self, interaction: discord.Interaction, bank: str):
        await interaction.response.defer()

        if not interaction.guild:
            return await self._private_err(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            cur = get_primary_currency(sb, guild_id)
            self._ensure_company_wallet(sb, bank, cur["currency_id"])

            res = (
                sb.table("company_wallets")
                .select("balance")
                .eq("company_id", bank)
                .eq("currency_id", cur["currency_id"])
                .limit(1)
                .execute()
            )
            rows = getattr(res, "data", None) or []
            bal = int(rows[0]["balance"]) if rows else 0

            bname = self._get_bank_name(sb, bank)
            emoji = cur.get("emoji") or ""

            embed = discord.Embed(
                title=f"🏦 {bname} • Balance",
                description=f"{emoji} **{cur['name']}**: `{bal}`",
                color=discord.Color.dark_teal(),
            )
            return await self._public_ok(interaction, embed=embed)

        except Exception as e:
            print(f"[bank balance] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error getting bank balance.")

    # ─────────────────────────────────────────────────────────────
    # /bank deposit, withdraw, transfer (atomic via RPC)
    # ─────────────────────────────────────────────────────────────

    @app_commands.command(name="deposit", description="Deposit from your active OC into a bank")
    @app_commands.describe(bank="Which bank", amount="Amount", reason="Optional note")
    @app_commands.autocomplete(bank=_bank_autocomplete)
    async def deposit(self, interaction: discord.Interaction, bank: str, amount: int, reason: str | None = None):
        await interaction.response.defer()

        if not interaction.guild:
            return await self._private_err(interaction, "Use this in a server, not DMs.")
        if amount <= 0:
            return await self._private_err(interaction, "Amount must be > 0.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        actor_id = int(interaction.user.id)

        try:
            cur = get_primary_currency(sb, guild_id)
            sender = get_active_character(sb, actor_id)
            if not sender:
                return await self._private_err(interaction, "No active OC set. Use `/oc select <name>`.")

            ensure_wallet(sb, sender["character_id"], cur["currency_id"])
            self._ensure_company_wallet(sb, bank, cur["currency_id"])

            row = apply_company_transaction(
                sb,
                guild_id=guild_id,
                currency_id=cur["currency_id"],
                tx_type="DEPOSIT",
                amount=int(amount),
                actor_discord_id=actor_id,
                from_character_id=sender["character_id"],
                to_company_id=bank,
                reason=reason,
            )

            bname = self._get_bank_name(sb, bank)
            emoji = cur.get("emoji") or ""

            msg = f"🏦 **{sender['name']}** deposited {emoji} `{amount}` **{cur['name']}** into **{bname}**"
            if reason:
                msg += f"\n📝 _{reason}_"

            ledger = discord.Embed(
                title="Ledger • Bank Deposit",
                description=f"{emoji} **+{amount} {cur['ticker']}** → **{bname}**",
                color=discord.Color.green(),
            )
            ledger.add_field(name="From", value=f"**{sender['name']}** (`{interaction.user}`)", inline=False)
            ledger.add_field(name="Bank Balance", value=f"`{row.get('to_company_balance')}`", inline=True)
            ledger.add_field(name="OC Balance", value=f"`{row.get('from_character_balance')}`", inline=True)
            if reason:
                ledger.add_field(name="Reason", value=reason, inline=False)
            ledger.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, ledger)

            return await self._public_ok(interaction, content=msg)

        except RuntimeError as ex:
            if str(ex) == "INSUFFICIENT_FUNDS":
                return await self._private_err(interaction, "❌ Not enough funds.")
            raise
        except Exception as e:
            print(f"[bank deposit] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error depositing.")

    @app_commands.command(name="withdraw", description="Withdraw from a bank into your active OC (requires teller+)")
    @app_commands.describe(bank="Which bank", amount="Amount", reason="Optional note")
    @app_commands.autocomplete(bank=_bank_autocomplete)
    async def withdraw(self, interaction: discord.Interaction, bank: str, amount: int, reason: str | None = None):
        await interaction.response.defer()

        if not interaction.guild:
            return await self._private_err(interaction, "Use this in a server, not DMs.")
        if amount <= 0:
            return await self._private_err(interaction, "Amount must be > 0.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        actor_id = int(interaction.user.id)

        try:
            rank = self._get_member_rank(sb, bank, actor_id)
            if not self._staff_ok(interaction) and not self._require_rank(rank, 1):
                return await self._private_err(interaction, "❌ You must be a bank member (TELLER+) to withdraw.")

            cur = get_primary_currency(sb, guild_id)
            receiver = get_active_character(sb, actor_id)
            if not receiver:
                return await self._private_err(interaction, "No active OC set. Use `/oc select <name>`.")

            ensure_wallet(sb, receiver["character_id"], cur["currency_id"])
            self._ensure_company_wallet(sb, bank, cur["currency_id"])

            row = apply_company_transaction(
                sb,
                guild_id=guild_id,
                currency_id=cur["currency_id"],
                tx_type="WITHDRAW",
                amount=int(amount),
                actor_discord_id=actor_id,
                from_company_id=bank,
                to_character_id=receiver["character_id"],
                reason=reason,
            )

            bname = self._get_bank_name(sb, bank)
            emoji = cur.get("emoji") or ""

            msg = f"🏦 **{receiver['name']}** withdrew {emoji} `{amount}` **{cur['name']}** from **{bname}**"
            if reason:
                msg += f"\n📝 _{reason}_"

            ledger = discord.Embed(
                title="Ledger • Bank Withdraw",
                description=f"{emoji} **-{amount} {cur['ticker']}** ← **{bname}**",
                color=discord.Color.orange(),
            )
            ledger.add_field(name="To", value=f"**{receiver['name']}** (`{interaction.user}`)", inline=False)
            ledger.add_field(name="Bank Balance", value=f"`{row.get('from_company_balance')}`", inline=True)
            ledger.add_field(name="OC Balance", value=f"`{row.get('to_character_balance')}`", inline=True)
            if reason:
                ledger.add_field(name="Reason", value=reason, inline=False)
            ledger.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, ledger)

            return await self._public_ok(interaction, content=msg)

        except RuntimeError as ex:
            if str(ex) == "INSUFFICIENT_FUNDS":
                return await self._private_err(interaction, "❌ Bank has insufficient funds.")
            raise
        except Exception as e:
            print(f"[bank withdraw] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error withdrawing.")

    @app_commands.command(name="transfer", description="Transfer between banks (requires manager+)")
    @app_commands.describe(from_bank="From bank", to_bank="To bank", amount="Amount", reason="Optional note")
    @app_commands.autocomplete(from_bank=_bank_autocomplete, to_bank=_bank_autocomplete)
    async def transfer(self, interaction: discord.Interaction, from_bank: str, to_bank: str, amount: int, reason: str | None = None):
        await interaction.response.defer()

        if not interaction.guild:
            return await self._private_err(interaction, "Use this in a server, not DMs.")
        if amount <= 0:
            return await self._private_err(interaction, "Amount must be > 0.")
        if from_bank == to_bank:
            return await self._private_err(interaction, "Choose two different banks.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        actor_id = int(interaction.user.id)

        try:
            rank = self._get_member_rank(sb, from_bank, actor_id)
            if not self._staff_ok(interaction) and not self._require_rank(rank, 2):
                return await self._private_err(interaction, "❌ You must be MANAGER+ on the source bank to transfer.")

            cur = get_primary_currency(sb, guild_id)
            self._ensure_company_wallet(sb, from_bank, cur["currency_id"])
            self._ensure_company_wallet(sb, to_bank, cur["currency_id"])

            row = apply_company_transaction(
                sb,
                guild_id=guild_id,
                currency_id=cur["currency_id"],
                tx_type="TRANSFER",
                amount=int(amount),
                actor_discord_id=actor_id,
                from_company_id=from_bank,
                to_company_id=to_bank,
                reason=reason,
            )

            from_name = self._get_bank_name(sb, from_bank)
            to_name = self._get_bank_name(sb, to_bank)
            emoji = cur.get("emoji") or ""

            msg = f"🏦 Transferred {emoji} `{amount}` **{cur['name']}** from **{from_name}** → **{to_name}**"
            if reason:
                msg += f"\n📝 _{reason}_"

            ledger = discord.Embed(
                title="Ledger • Bank Transfer",
                description=f"{emoji} **{amount} {cur['ticker']}** • **{from_name}** → **{to_name}**",
                color=discord.Color.gold(),
            )
            ledger.add_field(name="By", value=f"`{interaction.user}`", inline=False)
            ledger.add_field(name=f"{from_name} Balance", value=f"`{row.get('from_company_balance')}`", inline=True)
            ledger.add_field(name=f"{to_name} Balance", value=f"`{row.get('to_company_balance')}`", inline=True)
            if reason:
                ledger.add_field(name="Reason", value=reason, inline=False)
            ledger.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, ledger)

            return await self._public_ok(interaction, content=msg)

        except RuntimeError as ex:
            if str(ex) == "INSUFFICIENT_FUNDS":
                return await self._private_err(interaction, "❌ Source bank has insufficient funds.")
            raise
        except Exception as e:
            print(f"[bank transfer] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error transferring.")

    # ─────────────────────────────────────────────────────────────
    # Membership management (staff-only for now)
    # ─────────────────────────────────────────────────────────────

    @app_commands.command(name="addmember", description="Staff: Add a member to a bank")
    @app_commands.describe(bank="Which bank", user="Who to add", role="OWNER / MANAGER / TELLER")
    @app_commands.autocomplete(bank=_bank_autocomplete)
    async def addmember(self, interaction: discord.Interaction, bank: str, user: discord.Member, role: str):
        await interaction.response.defer()

        if not self._staff_ok(interaction):
            return await self._private_err(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private_err(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        role_u = (role or "").upper().strip()
        if role_u not in ("OWNER", "MANAGER", "TELLER"):
            return await self._private_err(interaction, "Role must be OWNER, MANAGER, or TELLER.")

        try:
            sb.table("company_members").upsert(
                {"company_id": bank, "discord_id": int(user.id), "role": role_u},
                on_conflict="company_id,discord_id",
            ).execute()

            bname = self._get_bank_name(sb, bank)

            ledger = discord.Embed(
                title="Ledger • Bank Member Added",
                description=f"🏦 **{bname}**",
                color=discord.Color.blue(),
            )
            ledger.add_field(name="User", value=f"`{user}`", inline=False)
            ledger.add_field(name="Role", value=role_u, inline=True)
            ledger.add_field(name="By", value=f"`{interaction.user}`", inline=False)
            ledger.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, ledger)

            return await self._public_ok(interaction, content=f"✅ Added {user.mention} as **{role_u}** to **{bname}**.")

        except Exception as e:
            print(f"[bank addmember] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error adding member.")

    @app_commands.command(name="removemember", description="Staff: Remove a member from a bank")
    @app_commands.describe(bank="Which bank", user="Who to remove")
    @app_commands.autocomplete(bank=_bank_autocomplete)
    async def removemember(self, interaction: discord.Interaction, bank: str, user: discord.Member):
        await interaction.response.defer()

        if not self._staff_ok(interaction):
            return await self._private_err(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private_err(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        try:
            sb.table("company_members").delete().eq("company_id", bank).eq("discord_id", int(user.id)).execute()
            bname = self._get_bank_name(sb, bank)

            ledger = discord.Embed(
                title="Ledger • Bank Member Removed",
                description=f"🏦 **{bname}**",
                color=discord.Color.red(),
            )
            ledger.add_field(name="User", value=f"`{user}`", inline=False)
            ledger.add_field(name="By", value=f"`{interaction.user}`", inline=False)
            ledger.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, ledger)

            return await self._public_ok(interaction, content=f"🗑️ Removed {user.mention} from **{bname}**.")

        except Exception as e:
            print(f"[bank removemember] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error removing member.")


async def setup(bot: commands.Bot):
    await bot.add_cog(BankCog(bot))