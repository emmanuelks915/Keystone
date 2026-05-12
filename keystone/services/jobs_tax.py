from __future__ import annotations

import math
from typing import Any

from services.currency_service import get_primary_currency
from services.economy_service import get_balance, apply_company_transaction


def _get_tax_settings(sb, guild_id: int) -> dict[str, Any]:
    res = sb.table("tax_settings").select("*").eq("guild_id", guild_id).limit(1).execute()
    rows = getattr(res, "data", None) or []
    return rows[0] if rows else {
        "enabled": False,
        "treasury_company_id": None,
        "rate_percent": 0,
        "flat_amount": 0,
        "min_balance": 0,
    }


def _find_character_ids_for_guild(sb, guild_id: int, currency_id: str) -> list[str]:
    # Prefer characters.guild_id if exists
    try:
        r = sb.table("characters").select("character_id").eq("guild_id", guild_id).execute()
        rows = getattr(r, "data", None) or []
        ids = [str(x["character_id"]) for x in rows if x.get("character_id")]
        if ids:
            return list(dict.fromkeys(ids))
    except Exception:
        pass

    # Else: from transactions in this guild
    ids: list[str] = []
    try:
        r = (
            sb.table("transactions")
            .select("from_character_id,to_character_id")
            .eq("guild_id", guild_id)
            .limit(1000)
            .execute()
        )
        rows = getattr(r, "data", None) or []
        for row in rows:
            fc = row.get("from_character_id")
            tc = row.get("to_character_id")
            if fc:
                ids.append(str(fc))
            if tc:
                ids.append(str(tc))
        ids = list(dict.fromkeys([x for x in ids if x]))
        if ids:
            return ids
    except Exception:
        pass

    # Else: all wallets in primary currency
    r = sb.table("wallets").select("character_id").eq("currency_id", currency_id).limit(1000).execute()
    rows = getattr(r, "data", None) or []
    ids = [str(x["character_id"]) for x in rows if x.get("character_id")]
    return list(dict.fromkeys(ids))


def run_tax_job(sb, *, guild_id: int, actor_discord_id: int, reason: str | None = None) -> dict[str, int]:
    """
    Runs the same logic as /tax run, but callable from the job runner.
    Returns summary counts: {total, charged, skipped}
    """
    s = _get_tax_settings(sb, guild_id)
    if not bool(s.get("enabled")):
        raise RuntimeError("Taxes are disabled")

    treasury = s.get("treasury_company_id")
    if not treasury:
        raise RuntimeError("Treasury is not set")

    cur = get_primary_currency(sb, guild_id)
    rate = float(s.get("rate_percent") or 0)
    flat = int(s.get("flat_amount") or 0)
    min_bal = int(s.get("min_balance") or 0)

    ids = _find_character_ids_for_guild(sb, guild_id, cur["currency_id"])

    # fetch names only if you want later; we just need IDs
    total = 0
    charged = 0
    skipped = 0

    for cid in ids:
        bal = get_balance(sb, cid, cur["currency_id"])
        if bal < min_bal:
            skipped += 1
            continue

        tax = int(math.floor(bal * (rate / 100.0))) + flat
        if tax <= 0 or bal < tax:
            skipped += 1
            continue

        try:
            apply_company_transaction(
                sb,
                guild_id=guild_id,
                currency_id=cur["currency_id"],
                tx_type="DEPOSIT",
                amount=int(tax),
                actor_discord_id=int(actor_discord_id),
                from_character_id=cid,
                to_company_id=treasury,
                reason=reason or "scheduled tax",
            )
        except RuntimeError:
            skipped += 1
            continue

        total += tax
        charged += 1

    return {"total": total, "charged": charged, "skipped": skipped}