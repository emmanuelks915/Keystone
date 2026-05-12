from __future__ import annotations

from typing import Any

from services.currency_service import get_primary_currency, ensure_wallet
from services.economy_service import transfer


def _get_stipend_settings(sb, guild_id: int) -> dict[str, Any]:
    res = sb.table("stipend_settings").select("*").eq("guild_id", int(guild_id)).limit(1).execute()
    rows = getattr(res, "data", None) or []
    if rows:
        return rows[0]
    # default row if not configured yet
    return {
        "guild_id": int(guild_id),
        "enabled": False,
        "base_amount": 0,
        "max_recipients": 500,
    }


def _get_role_bonuses(sb, guild_id: int) -> dict[int, int]:
    res = sb.table("stipend_role_bonuses").select("role_id,bonus_amount").eq("guild_id", int(guild_id)).execute()
    rows = getattr(res, "data", None) or []
    out: dict[int, int] = {}
    for r in rows:
        try:
            out[int(r["role_id"])] = int(r.get("bonus_amount") or 0)
        except Exception:
            continue
    return out


def _find_character_ids_for_guild(sb, guild_id: int, currency_id: str) -> list[str]:
    """
    Best-effort: find characters "in this guild".
    Prefer characters.guild_id if your schema has it.
    Otherwise: find all wallets in primary currency (good enough for a single-guild bot).
    """
    # 1) characters.guild_id exists?
    try:
        r = sb.table("characters").select("character_id").eq("guild_id", int(guild_id)).execute()
        rows = getattr(r, "data", None) or []
        ids = [str(x["character_id"]) for x in rows if x.get("character_id")]
        if ids:
            return list(dict.fromkeys(ids))
    except Exception:
        pass

    # 2) fallback: all wallets in this currency
    r = sb.table("wallets").select("character_id").eq("currency_id", currency_id).limit(1000).execute()
    rows = getattr(r, "data", None) or []
    ids = [str(x["character_id"]) for x in rows if x.get("character_id")]
    return list(dict.fromkeys(ids))


def _get_character_owner_discord_id(sb, character_id: str) -> int | None:
    """
    Your schema column name varies. We try a few common ones.
    """
    candidate_cols = ["discord_id", "owner_discord_id", "user_discord_id", "player_discord_id"]
    for col in candidate_cols:
        try:
            res = sb.table("characters").select(col).eq("character_id", character_id).limit(1).execute()
            rows = getattr(res, "data", None) or []
            if rows and rows[0].get(col) is not None:
                return int(rows[0][col])
        except Exception:
            continue
    return None


async def run_stipend_job(
    *,
    bot,
    sb,
    guild_id: int,
    actor_discord_id: int,
    reason: str | None = None,
) -> dict[str, int]:
    """
    Pays stipend (base + role bonuses) to characters.
    Uses economy_service.transfer() (RPC atomic) as MINT with a stipend reason.

    Returns summary:
      {paid_total, recipients, skipped, base_amount}
    """
    settings = _get_stipend_settings(sb, guild_id)
    if not bool(settings.get("enabled")):
        raise RuntimeError("Stipends are disabled")

    base_amount = int(settings.get("base_amount") or 0)
    if base_amount <= 0:
        raise RuntimeError("Base stipend must be > 0")

    max_recipients = int(settings.get("max_recipients") or 500)
    if max_recipients <= 0:
        max_recipients = 500

    cur = get_primary_currency(sb, guild_id)
    currency_id = cur["currency_id"]

    role_bonuses = _get_role_bonuses(sb, guild_id)

    # Discord guild (for role checks)
    guild = bot.get_guild(int(guild_id))
    if guild is None:
        # If bot isn't in guild cache, stipend still works without bonuses
        role_bonuses = {}

    character_ids = _find_character_ids_for_guild(sb, guild_id, currency_id)[:max_recipients]

    paid_total = 0
    recipients = 0
    skipped = 0

    for cid in character_ids:
        # Always ensure wallet exists
        ensure_wallet(sb, cid, currency_id)

        payout = base_amount

        # Apply role bonus if possible
        if role_bonuses and guild is not None:
            owner_id = _get_character_owner_discord_id(sb, cid)
            if owner_id:
                member = guild.get_member(owner_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(owner_id)
                    except Exception:
                        member = None

                if member is not None:
                    # add all matching bonuses (stacking)
                    member_role_ids = {r.id for r in member.roles}
                    for rid, bonus in role_bonuses.items():
                        if rid in member_role_ids:
                            payout += int(bonus)

        if payout <= 0:
            skipped += 1
            continue

        transfer(
            sb,
            guild_id=guild_id,
            currency_id=currency_id,
            amount=int(payout),
            tx_type="mint",
            actor_discord_id=int(actor_discord_id),
            from_character_id=None,
            to_character_id=cid,
            reason=(reason or "stipend") + f" | base={base_amount}",
        )

        paid_total += payout
        recipients += 1

    return {
        "paid_total": int(paid_total),
        "recipients": int(recipients),
        "skipped": int(skipped),
        "base_amount": int(base_amount),
    }