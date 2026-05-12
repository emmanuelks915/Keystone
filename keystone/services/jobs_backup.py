import io
import zipfile
import csv
from datetime import datetime, timezone

TABLES = [
    "currencies",
    "wallets",
    "transactions",
    "characters",
    "companies",
    "company_wallets",
    "company_transactions",
    "casino_settings",
]

PAGE_SIZE = 1000


def _fetch_all(sb, table: str):
    rows = []
    offset = 0
    while True:
        res = (
            sb.table(table)
            .select("*")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        chunk = getattr(res, "data", None) or []
        if not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


def _rows_to_csv(rows):
    if not rows:
        return b""

    headers = list(rows[0].keys())

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers)
    writer.writeheader()
    for r in rows:
        writer.writerow(r)

    return buf.getvalue().encode("utf-8")


def run_backup_job(sb, *, guild_id: int, actor_discord_id: int):
    tables_data = {}
    for t in TABLES:
        try:
            tables_data[t] = _fetch_all(sb, t)
        except Exception:
            tables_data[t] = []

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"keystone_backup_{guild_id}_{stamp}.zip"

    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
        for table, rows in tables_data.items():
            z.writestr(f"{table}.csv", _rows_to_csv(rows))

    mem.seek(0)

    summary = f"📦 Weekly Backup • {stamp}"

    return {
        "filename": filename,
        "bytes": mem.read(),
        "summary": summary,
    }