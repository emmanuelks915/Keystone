from __future__ import annotations

from pathlib import Path
import shutil


ROOT = Path.cwd()
MIGRATION_SQL = '-- 006_rp_post_location_fields.sql\n-- Long-term RP location storage for Citizen Registry / Last Seen.\n-- Run this in Supabase SQL Editor before deploying the bot patch.\n\nalter table if exists public.rp_posts\n  add column if not exists channel_id bigint,\n  add column if not exists channel_name text,\n  add column if not exists thread_name text,\n  add column if not exists parent_channel_id bigint,\n  add column if not exists parent_channel_name text,\n  add column if not exists location_name text,\n  add column if not exists jump_url text;\n\ncreate index if not exists idx_rp_posts_channel_id on public.rp_posts(channel_id);\ncreate index if not exists idx_rp_posts_thread_id on public.rp_posts(thread_id);\ncreate index if not exists idx_rp_posts_character_id on public.rp_posts(character_id);\ncreate index if not exists idx_rp_posts_posted_at on public.rp_posts(posted_at);\n'
HELPER_CODE = '\ndef get_rp_location_payload(message: discord.Message) -> dict[str, Any]:\n    """Build durable Discord location fields for a tracked RP post.\n\n    The tracker only saves posts from threads, but this safely handles normal\n    channels too. For threads:\n    - thread_id/thread_name = the RP thread\n    - channel_id/channel_name = the parent channel, which is usually the broader location/category\n    - location_name = parent channel name first, then thread name\n    """\n    channel = message.channel\n    parent = getattr(channel, "parent", None)\n\n    is_thread = isinstance(channel, discord.Thread)\n\n    thread_id = int(channel.id) if is_thread else None\n    thread_name = str(getattr(channel, "name", "") or "") if is_thread else None\n\n    parent_channel_id = None\n    parent_channel_name = None\n\n    if parent is not None:\n        parent_channel_id = int(getattr(parent, "id", 0) or 0) or None\n        parent_channel_name = str(getattr(parent, "name", "") or "") or None\n\n    channel_id = parent_channel_id if parent_channel_id is not None else int(getattr(channel, "id", 0) or 0)\n    channel_name = parent_channel_name or str(getattr(channel, "name", "") or "") or None\n\n    location_name = parent_channel_name or thread_name or channel_name\n\n    return {\n        "thread_id": int(thread_id or channel.id),\n        "thread_name": thread_name,\n        "channel_id": int(channel_id) if channel_id is not None else None,\n        "channel_name": channel_name,\n        "parent_channel_id": parent_channel_id,\n        "parent_channel_name": parent_channel_name,\n        "location_name": location_name,\n        "jump_url": getattr(message, "jump_url", None),\n    }\n'


def write_migration() -> None:
    database_dir = ROOT / "database"
    database_dir.mkdir(exist_ok=True)

    path = database_dir / "006_rp_post_location_fields.sql"
    path.write_text(MIGRATION_SQL, encoding="utf-8")
    print(f"Wrote {path}")


def should_skip(path: Path) -> bool:
    skip_parts = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build"}
    return bool({part.lower() for part in path.parts} & skip_parts)


def find_tracker_files() -> list[Path]:
    matches = []

    for path in ROOT.rglob("*.py"):
        if should_skip(path) or path.name.startswith("patch_") or path.name.startswith("fix_"):
            continue

        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        if (
            "async def save_tracked_post" in text
            and '.table("rp_posts").upsert' in text
            and '"thread_id": int(message.channel.id),' in text
        ):
            matches.append(path)

    return matches


def add_helper(text: str) -> str:
    if "def get_rp_location_payload(" in text:
        return text

    marker = "def thread_jump_url("
    index = text.find(marker)

    if index == -1:
        marker = "def message_jump_url("
        index = text.find(marker)

    if index == -1:
        # Fallback: put it before class RPTools
        index = text.find("class RPTools")
        if index == -1:
            raise RuntimeError("Could not find a safe helper insertion point.")

        return text[:index] + HELPER_CODE.strip() + "\n\n" + text[index:]

    # Insert before thread_jump_url/message_jump_url helpers.
    return text[:index] + HELPER_CODE.strip() + "\n\n" + text[index:]


def patch_file(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    original = text

    backup = path.with_suffix(path.suffix + ".rp_location_exact.bak")
    if not backup.exists():
        shutil.copyfile(path, backup)

    text = add_helper(text)

    old_line = '            "thread_id": int(message.channel.id),'
    new_line = '            **get_rp_location_payload(message),'

    if old_line not in text:
        print(f"{path}: thread_id payload line not found; skipping payload replacement.")
        return False

    text = text.replace(old_line, new_line, 1)

    if text != original:
        path.write_text(text, encoding="utf-8")
        print(f"Patched {path}")
        print(f"Backup saved as {backup}")
        return True

    return False


def main() -> None:
    write_migration()

    matches = find_tracker_files()

    print("")
    print("RP tracker files found:")
    if matches:
        for path in matches:
            print(f"- {path}")
    else:
        print("- none")

    patched = []
    for path in matches:
        if patch_file(path):
            patched.append(path)

    print("")
    if patched:
        print("Patched successfully:")
        for path in patched:
            print(f"- {path}")
    else:
        print("No files patched.")
        print("Make sure this script is being run from the bot repo root, not the web app repo root.")

    print("")
    print("Next steps:")
    print("1. Run database/006_rp_post_location_fields.sql in Supabase SQL Editor.")
    print("2. Restart the bot.")
    print("3. Send/edit one tracked RP post.")
    print("4. Check the rp_posts row for channel_name/location_name/jump_url.")


if __name__ == "__main__":
    main()
