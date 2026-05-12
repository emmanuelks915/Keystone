from __future__ import annotations

from typing import Any

from postgrest.exceptions import APIError


def _api_error_message(e: APIError) -> str:
    """
    Supabase/PostgREST error payloads are usually dicts with keys like:
    message, details, hint, code.
    """
    payload = None
    try:
        payload = e.args[0]
    except Exception:
        return str(e)

    if isinstance(payload, dict):
        msg = payload.get("message") or "Database error"
        details = payload.get("details")
        hint = payload.get("hint")
        parts = [str(msg)]
        if details:
            parts.append(str(details))
        if hint:
            parts.append(str(hint))
        return " — ".join(parts)

    return str(e)


# ─────────────────────────────────────────────────────────────
# Character wallets
# ─────────────────────────────────────────────────────────────

def get_balance(sb, character_id: str, currency_id: str) -> int:
    """Always read balance by (character_id, currency_id)."""
    res = (
        sb.table("wallets")
        .select("balance")
        .eq("character_id", character_id)
        .eq("currency_id", currency_id)
        .limit(1)
        .execute()
    )
    rows = getattr(res, "data", None) or []
    if not rows:
        return 0
    return int(rows[0].get("balance") or 0)


def apply_transaction(
    sb,
    *,
    guild_id: int,
    currency_id: str,
    from_character_id: str | None,
    to_character_id: str | None,
    amount: int,
    actor_discord_id: int,
    tx_type: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """
    Calls Supabase RPC rpc_apply_transaction (atomic).
    Returns: {tx_id, from_balance, to_balance}
    """
    tx_type = (tx_type or "").upper().strip()

    payload = {
        "p_guild_id": int(guild_id),
        "p_currency_id": currency_id,
        "p_from_character_id": from_character_id,
        "p_to_character_id": to_character_id,
        "p_amount": int(amount),
        "p_actor_discord_id": int(actor_discord_id),
        "p_tx_type": tx_type,
        "p_reason": reason,
    }

    try:
        res = sb.rpc("rpc_apply_transaction", payload).execute()
    except APIError as e:
        msg = _api_error_message(e)
        if "insufficient" in msg.lower():
            raise RuntimeError("INSUFFICIENT_FUNDS") from e
        raise RuntimeError(msg) from e

    data = getattr(res, "data", None) or []
    if not data:
        raise RuntimeError("rpc_apply_transaction returned no data")
    return data[0]


def transfer(
    sb,
    *,
    guild_id: int,
    currency_id: str,
    amount: int,
    tx_type: str,
    actor_discord_id: int,
    from_character_id: str | None,
    to_character_id: str | None,
    reason: str | None = None,
) -> dict[str, Any]:
    """
    Backwards-compatible wrapper used by your cogs.

    Supported tx_type inputs:
      - mint
      - burn
      - transfer / pay / p2p
      - setbalance / set_balance

    It maps them to RPC tx types:
      - MINT / BURN / TRANSFER / SETBALANCE
    """
    t = (tx_type or "").lower().strip()

    if t == "mint":
        rpc_type = "MINT"
    elif t == "burn":
        rpc_type = "BURN"
    elif t in ("transfer", "pay", "p2p"):
        rpc_type = "TRANSFER"
    elif t in ("setbalance", "set_balance", "set-balance", "setbal"):
        rpc_type = "SETBALANCE"
    else:
        raise RuntimeError(f"Unknown tx_type '{tx_type}'")

    return apply_transaction(
        sb,
        guild_id=guild_id,
        currency_id=currency_id,
        from_character_id=from_character_id,
        to_character_id=to_character_id,
        amount=int(amount),
        actor_discord_id=int(actor_discord_id),
        tx_type=rpc_type,
        reason=reason,
    )


# ─────────────────────────────────────────────────────────────
# Company / Bank wallets
# ─────────────────────────────────────────────────────────────

def apply_company_transaction(
    sb,
    *,
    guild_id: int,
    currency_id: str,
    amount: int,
    actor_discord_id: int,
    tx_type: str,
    reason: str | None = None,
    from_company_id: str | None = None,
    to_company_id: str | None = None,
    from_character_id: str | None = None,
    to_character_id: str | None = None,
) -> dict[str, Any]:
    """
    Calls Supabase RPC rpc_apply_company_transaction (atomic).
    Returns:
      {
        company_tx_id,
        from_company_balance,
        to_company_balance,
        from_character_balance,
        to_character_balance
      }
    """
    payload = {
        "p_guild_id": int(guild_id),
        "p_currency_id": currency_id,
        "p_amount": int(amount),
        "p_actor_discord_id": int(actor_discord_id),
        "p_tx_type": (tx_type or "").upper().strip(),

        # optional
        "p_reason": reason,
        "p_from_company_id": from_company_id,
        "p_to_company_id": to_company_id,
        "p_from_character_id": from_character_id,
        "p_to_character_id": to_character_id,
    }

    try:
        res = sb.rpc("rpc_apply_company_transaction", payload).execute()
    except APIError as e:
        msg = _api_error_message(e)
        if "insufficient" in msg.lower():
            raise RuntimeError("INSUFFICIENT_FUNDS") from e
        raise RuntimeError(msg) from e

    data = getattr(res, "data", None) or []
    if not data:
        raise RuntimeError("rpc_apply_company_transaction returned no data")
    return data[0]


def company_transfer(
    sb,
    *,
    guild_id: int,
    currency_id: str,
    amount: int,
    tx_type: str,
    actor_discord_id: int,
    reason: str | None = None,
    from_company_id: str | None = None,
    to_company_id: str | None = None,
    from_character_id: str | None = None,
    to_character_id: str | None = None,
) -> dict[str, Any]:
    """
    Friendly wrapper for bank ops.

    Accepts:
      - deposit (character -> company)
      - withdraw (company -> character)
      - transfer (company -> company)
      - mint / burn / setbalance (company or character, depending on IDs provided)
    """
    t = (tx_type or "").lower().strip()

    if t in ("deposit",):
        rpc_type = "DEPOSIT"
    elif t in ("withdraw",):
        rpc_type = "WITHDRAW"
    elif t in ("transfer", "banktransfer"):
        rpc_type = "TRANSFER"
    elif t == "mint":
        rpc_type = "MINT"
    elif t == "burn":
        rpc_type = "BURN"
    elif t in ("setbalance", "set_balance", "set-balance"):
        rpc_type = "SETBALANCE"
    else:
        raise RuntimeError(f"Unknown company tx_type '{tx_type}'")

    return apply_company_transaction(
        sb,
        guild_id=guild_id,
        currency_id=currency_id,
        amount=int(amount),
        actor_discord_id=int(actor_discord_id),
        tx_type=rpc_type,
        reason=reason,
        from_company_id=from_company_id,
        to_company_id=to_company_id,
        from_character_id=from_character_id,
        to_character_id=to_character_id,
    )