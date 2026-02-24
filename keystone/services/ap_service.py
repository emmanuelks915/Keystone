# services/ap_service.py
from __future__ import annotations

from services.db import get_supabase_client


def ensure_wallet(oc_id: str) -> None:
    sb = get_supabase_client()
    sb.table("oc_wallets").upsert(
        {"oc_id": oc_id, "ap_balance": 0},
        on_conflict="oc_id",
    ).execute()


def get_ap(oc_id: str) -> int:
    sb = get_supabase_client()

    res = (
        sb.table("oc_wallets")
        .select("ap_balance")
        .eq("oc_id", oc_id)
        .limit(1)
        .execute()
    )
    rows = getattr(res, "data", None) or []

    if not rows:
        ensure_wallet(oc_id)
        return 0

    return int(rows[0].get("ap_balance") or 0)


def set_ap(oc_id: str, new_balance: int) -> int:
    sb = get_supabase_client()
    new_balance = int(new_balance)

    if new_balance < 0:
        raise ValueError("AP cannot go below 0.")

    sb.table("oc_wallets").upsert(
        {"oc_id": oc_id, "ap_balance": new_balance},
        on_conflict="oc_id",
    ).execute()

    return new_balance


def add_ap(oc_id: str, delta: int) -> int:
    current = get_ap(oc_id)
    return set_ap(oc_id, current + int(delta))


def require_ap(oc_id: str, cost: int) -> None:
    cost = int(cost or 0)
    if cost < 0:
        raise ValueError("AP cost must be >= 0.")

    bal = get_ap(oc_id)
    if bal < cost:
        raise ValueError(f"Not enough AP. Need {cost}, you have {bal}.")


def charge_ap(oc_id: str, cost: int) -> int:
    """
    Store-safe purchase helper:
    - validates cost
    - checks balance
    - deducts
    Returns the new balance.
    """
    cost = int(cost or 0)
    if cost < 0:
        raise ValueError("AP cost must be >= 0.")

    bal = get_ap(oc_id)
    if bal < cost:
        raise ValueError(f"Not enough AP. Need {cost}, you have {bal}.")

    return set_ap(oc_id, bal - cost)
