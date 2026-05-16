# Exact RP Tracker Location Patch

This patches the RP tracker cog you sent.

Your current `save_tracked_post()` writes RP rows into `rp_posts` with:

```python
"thread_id": int(message.channel.id),
```

but it does not store a readable location/channel name. This patch changes that to:

```python
**get_rp_location_payload(message),
```

which stores:

```txt
thread_id
thread_name
channel_id
channel_name
parent_channel_id
parent_channel_name
location_name
jump_url
```

## Where to run this

Run this from the **bot repo root**, not the Railbound Tools webapp repo root, unless this cog is also inside the webapp repo.

Example:

```powershell
cd C:\Users\emman\OneDrive\Documents\Keystone
```

or wherever the bot file lives.

## Commands

```powershell
Expand-Archive -Path "$env:USERPROFILE\Downloads\exact_rp_tracker_location_patch.zip" -DestinationPath . -Force
python patch_exact_rp_tracker_location.py
```

## Supabase step

Open Supabase SQL Editor and run:

```txt
database/006_rp_post_location_fields.sql
```

## Test

Restart the bot, then send/edit one tracked RP message. Check `rp_posts` and confirm the new row has:

```txt
channel_name
location_name
thread_name
jump_url
```

## Commit

```powershell
git add .
git commit -m "Store RP post location metadata"
git push
```
