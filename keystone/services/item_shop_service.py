# services/item_shop_service.py
from __future__ import annotations

from typing import Any, Optional

from services.db import get_supabase_client


def _get_oc_by_name(sb, oc_name: str) -> Optional[dict[str, Any]]:
    res = (
        sb.table("ocs")
        .select("oc_id, oc_name, owner_discord_id, avatar_url")
        .eq("oc_name", oc_name)
        .limit(1)
        .execute()
    )
    rows = getattr(res, "data", None) or []
    return rows[0] if rows else None


def _get_item_by_key(sb, item_key: str) -> Optional[dict[str, Any]]:
    # item_key can be item_id or item_name depending on your schema.
    # If you use item_id, swap `.eq("item_id", item_key)`
    res = (
        sb.table("items")
        .select("item_id, item_name, description, token_cost, thral_cost, is_active")
        .eq("item_name", item_key)
        .limit(1)
        .execute()
    )
    rows = getattr(res, "data", None) or []
    return rows[0] if rows else None


def _get_token_balance(sb, oc_id: str) -> int:
    res = sb.table("token_wallets").select("balance").eq("oc_id", oc_id).limit(1).execute()
    rows = getattr(res, "data", None) or []
    return int(rows[0].get("balance") or 0) if rows else 0


def _set_token_balance(sb, oc_id: str, new_balance: int) -> int:
    new_balance = max(0, int(new_balance))
    sb.table("token_wallets").upsert({"oc_id": oc_id, "balance": new_balance}, on_conflict="oc_id").execute()
    return new_balance


def _get_thral_balance(sb, oc_id: str) -> int:
    res = sb.table("thral_wallets").select("balance").eq("oc_id", oc_id).limit(1).execute()
    rows = getattr(res, "data", None) or []
    return int(rows[0].get("balance") or 0) if rows else 0


def _set_thral_balance(sb, oc_id: str, new_balance: int) -> int:
    new_balance = max(0, int(new_balance))
    sb.table("thral_wallets").upsert({"oc_id": oc_id, "balance": new_balance}, on_conflict="oc_id").execute()
    return new_balance


def _grant_inventory(sb, oc_id: str, item_id: str, qty: int) -> None:
    # Adjust to match your inventory schema (inventory table name / columns)
    sb.table("inventory").upsert(
        {"oc_id": oc_id, "item_id": item_id, "quantity": int(qty)},
        on_conflict="oc_id,item_id",
    ).execute()

    # If your inventory uses "amount" not "quantity", swap the field name.


def buy_item(oc_name: str, item_key: str, qty: int, *, by_discord_id: int) -> dict[str, Any]:
    """
    Returns a dict containing item info + new balances.
    Raises ValueError with a user-friendly message on failure.
    """
    sb = get_supabase_client()

    qty = int(qty)
    if qty <= 0:
        raise ValueError("Quantity must be 1 or higher.")

    oc = _get_oc_by_name(sb, oc_name.strip())
    if not oc:
        raise ValueError("OC not found.")

    item = _get_item_by_key(sb, item_key.strip())
    if not item:
        raise ValueError("Item not found.")
    if not item.get("is_active", True):
        raise ValueError("That item is not currently available.")

    token_cost = int(item.get("token_cost") or 0) * qty
    thral_cost = int(item.get("thral_cost") or 0) * qty

    if token_cost <= 0 and thral_cost <= 0:
        raise ValueError("This item has no price set. Staff must set token_cost and/or thral_cost.")

    # Check balances
    token_bal = _get_token_balance(sb, oc["oc_id"])
    thral_bal = _get_thral_balance(sb, oc["oc_id"])

    if token_cost > 0 and token_bal < token_cost:
        raise ValueError(f"Not enough Tokens. Need {token_cost}, you have {token_bal}.")
    if thral_cost > 0 and thral_bal < thral_cost:
        raise ValueError(f"Not enough Thral. Need {thral_cost}, you have {thral_bal}.")

    # Deduct (simple, deterministic)
    if token_cost > 0:
        token_bal = _set_token_balance(sb, oc["oc_id"], token_bal - token_cost)
    if thral_cost > 0:
        thral_bal = _set_thral_balance(sb, oc["oc_id"], thral_bal - thral_cost)

    # Grant item
    _grant_inventory(sb, oc["oc_id"], item["item_id"], qty)

    # Optional: shop log table if you want it
    # sb.table("shop_logs").insert({...}).execute()

    return {
        "oc": oc,
        "item": item,
        "qty": qty,
        "token_spent": token_cost,
        "thral_spent": thral_cost,
        "token_new_balance": token_bal,
        "thral_new_balance": thral_bal,
    }
