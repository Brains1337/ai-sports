"""HTTP fetching with retry, disk cache, and call-budget accounting.

Shared because every provider here is rate-limited or quota-limited in some
way: the FantasyPros free tier is licensed for non-production use, CFBD's
lower tiers meter calls per month, and Yahoo throttles aggressively.
Uncached loops over positions x weeks x scoring formats burn a monthly quota
in one afternoon.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException
from sqlalchemy import text
from urllib3.util.retry import Retry

#: Statuses worth retrying. Everything else is permanent.
RETRY_STATUS = (408, 429, 500, 502, 503, 504)

#: Client errors that will NEVER succeed on retry. Retrying a 401/403/404
#: burns a metered quota and turns a clear failure into a slow one, so these
#: raise immediately with the response body attached — the body usually says
#: exactly what is wrong (wrong path, key not provisioned, season not
#: available on this tier).
FAIL_FAST_STATUS = (400, 401, 403, 404, 405, 410, 422)


class PermanentHTTPError(RuntimeError):
    """A 4xx that retrying cannot fix."""

    def __init__(self, status: int, url: str, body: str) -> None:
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status} (permanent) for {url}\n  body: {body[:600]}")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def content_hash(data: Any) -> str:
    """Stable hash of a JSON-able payload, for change detection."""
    blob = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


class CallBudget:
    """Counts requests so a quota-limited provider can't be blown silently.

    CFBD disables the key for the rest of the month when the cap is hit, so
    running out is a multi-week outage, not a transient error. Exceeding the
    soft limit raises rather than degrading quietly.
    """

    def __init__(self, name: str, limit: int | None = None) -> None:
        self.name = name
        self.limit = limit
        self.used = 0
        self.from_cache = 0

    def spend(self, n: int = 1) -> None:
        self.used += n
        if self.limit is not None and self.used > self.limit:
            raise RuntimeError(
                f"{self.name}: call budget exceeded ({self.used} > {self.limit}). "
                "Raise the limit or widen the cache window."
            )

    def as_meta(self) -> dict[str, Any]:
        return {
            f"{self.name}_calls": self.used,
            f"{self.name}_cache_hits": self.from_cache,
            f"{self.name}_call_limit": self.limit,
        }


class JsonClient:
    """A cached, retrying JSON GET client for one provider."""

    def __init__(
        self,
        *,
        base_url: str,
        headers: dict[str, str] | None = None,
        cache_dir: str | Path | None = None,
        use_cache: bool = True,
        refresh_cache: bool = False,
        cache_ttl_seconds: int | None = None,
        request_delay: float = 0.0,
        max_attempts: int = 5,
        timeout: tuple[int, int] = (10, 90),
        budget: CallBudget | None = None,
        user_agent: str = "ai-sports/1.0",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Accept": "application/json", "User-Agent": user_agent}
        self.headers.update(headers or {})
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.use_cache = use_cache
        self.refresh_cache = refresh_cache
        self.cache_ttl_seconds = cache_ttl_seconds
        self.request_delay = request_delay
        self.max_attempts = max_attempts
        self.timeout = timeout
        self.budget = budget or CallBudget("http")

        self.session = requests.Session()
        adapter = HTTPAdapter(
            max_retries=Retry(
                total=3,
                connect=3,
                read=3,
                status=3,
                backoff_factor=1,
                status_forcelist=list(RETRY_STATUS),
                allowed_methods=frozenset(["GET"]),
                respect_retry_after_header=True,
                raise_on_status=False,
            )
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # -- cache -------------------------------------------------------------

    def _cache_file(self, path: str, params: dict | None) -> Path | None:
        """Cache key must include base_url.

        It previously did not, which produced a false positive in
        probe_base_url: once one base URL succeeded and cached the response,
        probing a *different* base URL for the same path read that cache and
        reported OK for a host that actually returns 403.
        """
        if not self.cache_dir:
            return None
        slug = path.strip("/").replace("/", "_")
        digest = content_hash({"base": self.base_url, "params": params or {}})[:12]
        return self.cache_dir / f"{slug}__{digest}.json"

    def _cache_get(self, f: Path | None) -> Any | None:
        if not f or not self.use_cache or self.refresh_cache or not f.exists():
            return None
        if self.cache_ttl_seconds is not None:
            age = time.time() - f.stat().st_mtime
            if age > self.cache_ttl_seconds:
                return None
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _cache_put(self, f: Path | None, data: Any) -> None:
        if not f:
            return
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    # -- fetch -------------------------------------------------------------

    def get(
        self,
        path: str,
        params: dict | None = None,
        *,
        max_attempts: int | None = None,
    ) -> Any:
        cache_file = self._cache_file(path, params)
        cached = self._cache_get(cache_file)
        if cached is not None:
            self.budget.from_cache += 1
            return cached

        url = f"{self.base_url}/{path.lstrip('/')}"
        attempts = max_attempts or self.max_attempts
        last: Exception | None = None

        for attempt in range(1, attempts + 1):
            self.budget.spend()
            try:
                resp = self.session.get(
                    url, headers=self.headers, params=params, timeout=self.timeout
                )
            except RequestException as exc:
                # Transport-level failure (DNS, connection reset, timeout).
                last = exc
                if attempt >= attempts:
                    raise
                delay = min(60, 2**attempt)
                print(f"  {path} transport error: {exc}; retry in ~{delay:.0f}s")
                time.sleep(delay + random.uniform(0, 1))
                continue

            # Fail fast on permanent client errors. Retrying these was a bug:
            # HTTPError subclasses RequestException, so a 403 fell into the
            # generic retry path and backed off repeatedly on a request that
            # could never succeed.
            if resp.status_code in FAIL_FAST_STATUS:
                raise PermanentHTTPError(resp.status_code, resp.url, resp.text)

            if resp.status_code in RETRY_STATUS and attempt < attempts:
                delay = self._retry_after(resp, attempt)
                print(
                    f"  HTTP {resp.status_code} on {path} "
                    f"({attempt}/{attempts}); sleeping ~{delay:.0f}s"
                )
                time.sleep(delay + random.uniform(0, 1))
                continue

            try:
                resp.raise_for_status()
            except RequestException as exc:
                raise PermanentHTTPError(
                    resp.status_code, resp.url, resp.text
                ) from exc

            data = resp.json()
            self._cache_put(cache_file, data)
            if self.request_delay:
                time.sleep(self.request_delay)
            return data

        raise last if last else RuntimeError(f"{path}: exhausted attempts")

    @staticmethod
    def _retry_after(resp: requests.Response, attempt: int) -> float:
        hdr = resp.headers.get("Retry-After")
        if hdr:
            try:
                return min(float(hdr), 300)
            except ValueError:
                pass
        return min(60, 2**attempt)

    def probe_base_url(
        self, path: str, params: dict | None, candidates: list[str]
    ) -> str | None:
        """Find which base URL a key actually works against.

        The repo used ``/public/v2/json`` while the current FantasyPros docs
        say ``/v2/json``. Rather than guess, try each and report the winner.

        Single attempt per candidate: a wrong path returns 403/404, which no
        amount of backoff will fix, and retrying made this probe slow enough
        to look like a hang.
        """
        original = self.base_url
        for cand in candidates:
            self.base_url = cand.rstrip("/")
            try:
                self.get(path, params, max_attempts=1)
                print(f"  base_url OK -> {cand}")
                return self.base_url
            except PermanentHTTPError as exc:
                print(f"  base_url {cand} -> HTTP {exc.status}")
                if exc.body:
                    print(f"      body: {exc.body[:300]}")
            except Exception as exc:  # noqa: BLE001 - probing on purpose
                print(f"  base_url {cand} -> {type(exc).__name__}: {exc}")
        self.base_url = original
        return None


def record_source(
    conn: Any,
    *,
    source_name: str,
    source_key: str,
    payload: Any,
    status: str = "ok",
    meta: dict | None = None,
) -> None:
    """Append a fetch-audit row, ignoring exact duplicates.

    ``ux_sources_name_key_fetched`` is unique on
    ``(source_name, source_key, fetched_at)``, so a same-instant retry would
    otherwise abort the transaction.
    """
    conn.execute(
        text(
            """
            insert into sources
                (source_name, source_key, fetched_at, content_hash, status, meta)
            values
                (:source_name, :source_key, :fetched_at, :content_hash,
                 :status, cast(:meta as jsonb))
            on conflict do nothing
            """
        ),
        {
            "source_name": source_name,
            "source_key": source_key,
            "fetched_at": utcnow(),
            "content_hash": content_hash(payload),
            "status": status,
            "meta": json.dumps(meta or {}, default=str),
        },
    )


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
