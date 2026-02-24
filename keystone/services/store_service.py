from __future__ import annotations

from services.db import get_supabase_client


def _normalize_item(row: dict) -> dict:
    """
    Normalizes catalog rows so the rest of the bot can always rely on:
      - token_cost (int)
      - thral_cost (int)

    With your current schema, token_cost/thral_cost exist as real columns.
    We DO NOT use price_ap for items anymore (that was causing token ↔ thral confusion).
    """
    meta = row.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}

    # Prefer real columns; allow meta fallback only if needed
    token_cost = row.get("token_cost")
    if token_cost is None:
        token_cost = meta.get("token_cost", 0)

    thral_cost = row.get("thral_cost")
    if thral_cost is None:
        thral_cost = meta.get("thral_cost", 0)

    row["token_cost"] = int(token_cost or 0)
    row["thral_cost"] = int(thral_cost or 0)
    row["meta"] = meta
    return row


def build_item_payload(
    *,
    item_name: str,
    token_cost: int = 0,
    thral_cost: int = 0,
    effect: str = "—",
    duration: str | None = None,
    for_sale: bool = True,
    active: bool = True,
    doc_url: str | None = None,
) -> dict:
    """
    Build payload for item_catalog INSERT/UPDATE based on your schema.
    Writes to real columns: token_cost + thral_cost.
    Also mirrors into meta for forward compatibility.
    """
    def _safe_int(v) -> int:
        try:
            return int(v or 0)
        except Exception:
            return 0

    token_i = _safe_int(token_cost)
    thral_i = _safe_int(thral_cost)

    return {
        "item_name": (item_name or "").strip(),
        "token_cost": token_i,
        "thral_cost": thral_i,
        "effect": (effect or "—").strip(),
        "duration": (duration.strip() if duration else None),
        "for_sale": bool(for_sale),
        "active": bool(active),
        "doc_url": (doc_url.strip() if doc_url else None),
        "meta": {
            "token_cost": token_i,
            "thral_cost": thral_i,
        },
    }


# =========================
# Items (Tokens / Thral)
# =========================
def list_items_for_sale() -> list[dict]:
    sb = get_supabase_client()
    res = (
        sb.table("item_catalog")
        .select("*")
        .eq("for_sale", True)
        .eq("active", True)
        .execute()
    )
    rows = getattr(res, "data", None) or []
    return [_normalize_item(r) for r in rows]


def get_item_by_name(item_name: str) -> dict | None:
    sb = get_supabase_client()
    res = (
        sb.table("item_catalog")
        .select("*")
        .eq("item_name", item_name.strip())
        .limit(1)
        .execute()
    )
    rows = getattr(res, "data", None) or []
    return _normalize_item(rows[0]) if rows else None


# =========================
# Skills (AP) - unchanged
# =========================
def list_skills_for_sale() -> list[dict]:
    sb = get_supabase_client()
    res = (
        sb.table("skill_catalog")
        .select("skill_id, skill_name, description, cost_ap, active, doc_url, for_sale")
        .eq("for_sale", True)
        .eq("active", True)
        .execute()
    )
    return getattr(res, "data", None) or []


def get_skill_by_name(skill_name: str) -> dict | None:
    sb = get_supabase_client()
    res = (
        sb.table("skill_catalog")
        .select("skill_id, skill_name, description, cost_ap, active, doc_url, for_sale")
        .eq("skill_name", skill_name.strip())
        .limit(1)
        .execute()
    )
    rows = getattr(res, "data", None) or []
    return rows[0] if rows else None
