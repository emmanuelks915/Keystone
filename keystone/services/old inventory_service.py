from __future__ import annotations

from services.db import get_supabase_client


def get_item_qty(oc_id: str, item_id: str) -> int:
    sb = get_supabase_client()

    res = (
        sb.table("inventories_norm")
        .select("quantity")
        .eq("oc_id", oc_id)
        .eq("item_id", item_id)
        .limit(1)            
        .execute()
    )

    rows = getattr(res, "data", None) or []
    return int(rows[0].get("quantity", 0)) if rows else 0


def set_item_qty(oc_id: str, item_id: str, qty: int) -> None:
    sb = get_supabase_client()
    qty = int(qty)

    if qty < 0:
        raise ValueError("Quantity cannot go below 0.")

    # ✅ idempotent upsert
    sb.table("inventories_norm").upsert(
        {
            "oc_id": oc_id,
            "item_id": item_id,
            "quantity": qty,
        },
        on_conflict="oc_id,item_id",
    ).execute()


def add_item_qty(oc_id: str, item_id: str, delta: int) -> int:
    current = get_item_qty(oc_id, item_id)
    new_qty = current + int(delta)

    if new_qty < 0:
        raise ValueError("Not enough quantity.")

    set_item_qty(oc_id, item_id, new_qty)
    return new_qty
