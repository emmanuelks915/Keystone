from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable

from postgrest.exceptions import APIError


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_config(val: Any) -> dict[str, Any]:
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


class JobRunner:
    """
    Background scheduled job runner that polls scheduled_jobs and runs due jobs.

    - Uses asyncio.to_thread() because Supabase client is synchronous.
    - Uses a per-job lock to avoid overlapping runs.
    """

    def __init__(
        self,
        *,
        bot,
        poll_seconds: int = 30,
        claim_batch_size: int = 25,
    ):
        self.bot = bot
        self.poll_seconds = int(poll_seconds)
        self.claim_batch_size = int(claim_batch_size)

        self._task: asyncio.Task | None = None
        self._stopping = False
        self._job_locks: dict[str, asyncio.Lock] = {}

        # handlers: job_type -> async function(job_row)
        self._handlers: dict[str, Callable[[dict[str, Any]], Awaitable[str | None]]] = {}

    def sb(self):
        sb = getattr(self.bot, "supabase", None)
        if sb is None:
            raise RuntimeError("Supabase is not configured on the bot.")
        return sb

    def register(self, job_type: str, handler: Callable[[dict[str, Any]], Awaitable[str | None]]):
        self._handlers[job_type.upper().strip()] = handler

    def start(self):
        if self._task and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._loop(), name="keystone_job_runner")

    async def stop(self):
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass

    async def _loop(self):
        await asyncio.sleep(2)  # small boot delay so bot finishes init
        while not self._stopping:
            try:
                due = await self._fetch_due_jobs()
                for job in due:
                    await self._run_one(job)
            except Exception as e:
                print(f"[job_runner] loop error: {e}")
            await asyncio.sleep(self.poll_seconds)

    async def _fetch_due_jobs(self) -> list[dict[str, Any]]:
        def _do():
            sb = self.sb()
            # Pull a small batch of due jobs
            res = (
                sb.table("scheduled_jobs")
                .select("*")
                .eq("enabled", True)
                .lte("next_run_at", _utc_now_iso())
                .order("next_run_at", desc=False)
                .limit(self.claim_batch_size)
                .execute()
            )
            return getattr(res, "data", None) or []

        return await asyncio.to_thread(_do)

    async def _run_one(self, job: dict[str, Any]):
        job_id = str(job.get("job_id"))
        if not job_id:
            return

        lock = self._job_locks.get(job_id)
        if lock is None:
            lock = asyncio.Lock()
            self._job_locks[job_id] = lock

        if lock.locked():
            return  # already running

        async with lock:
            job_type = str(job.get("job_type") or "").upper().strip()
            handler = self._handlers.get(job_type)
            if handler is None:
                await self._mark_error(job_id, f"No handler registered for job_type={job_type}")
                return

            # Run handler
            try:
                detail = await handler(job)
                await self._mark_ok(job, detail=detail)
            except Exception as e:
                await self._mark_error(job_id, str(e))

    async def _mark_ok(self, job: dict[str, Any], *, detail: str | None):
        job_id = str(job["job_id"])
        interval = int(job.get("interval_seconds") or 0)
        if interval <= 0:
            interval = 3600

        now_iso = _utc_now_iso()

        # compute next_run_at based on now (simple + stable)
        def _do():
            sb = self.sb()
            # Update job row
            sb.table("scheduled_jobs").update(
                {
                    "last_run_at": now_iso,
                    "last_status": "OK",
                    "last_error": None,
                    "next_run_at": datetime.now(timezone.utc)
                    .replace(microsecond=0)
                    .isoformat(),  # ensure string
                }
            ).eq("job_id", job_id).execute()

            # Set next_run_at = now + interval using server time-ish: do client compute
            next_iso = (datetime.now(timezone.utc).timestamp() + interval)
            next_run_at = datetime.fromtimestamp(next_iso, tz=timezone.utc).isoformat()

            sb.table("scheduled_jobs").update({"next_run_at": next_run_at}).eq("job_id", job_id).execute()

            sb.table("scheduled_job_runs").insert(
                {"job_id": job_id, "status": "OK", "detail": detail}
            ).execute()

        await asyncio.to_thread(_do)

    async def _mark_error(self, job_id: str, err: str):
        now_iso = _utc_now_iso()

        def _do():
            sb = self.sb()
            sb.table("scheduled_jobs").update(
                {
                    "last_run_at": now_iso,
                    "last_status": "ERROR",
                    "last_error": err[:1000],
                    # push next_run_at forward a bit so it doesn't spam
                    "next_run_at": datetime.fromtimestamp(
                        datetime.now(timezone.utc).timestamp() + 300,
                        tz=timezone.utc,
                    ).isoformat(),
                }
            ).eq("job_id", job_id).execute()

            sb.table("scheduled_job_runs").insert(
                {"job_id": job_id, "status": "ERROR", "detail": err[:1000]}
            ).execute()

        await asyncio.to_thread(_do)