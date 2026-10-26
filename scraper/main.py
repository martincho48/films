"""TMDB scraper for Cloud Run Job.

Full monthly download of the TMDB catalog for the configured countries.
All filters and trimming rules are in config.toml (see comments there).

1. /discover/movie per country over the whole release-date range. A range with
   more results than TMDB returns (10 000) is split in half until it fits.
2. /movie/{id}?append_to_response=credits,keywords for every unique film,
   credits trimmed to top N cast and selected crew jobs, everything else as is.
3. Output to GCS, uploaded continuously in parts:

    gs://<BUCKET>/raw/tmdb/dt=YYYY-MM-DD/run=YYYY-MM-DDTHH-MM-SSZ/
        lists/part-0001.jsonl      # raw discover pages + query that produced them
        details/part-0001.jsonl    # one trimmed movie detail per line, plus _fetched_at
        manifest.json              # written last = run is complete

   key=value folders are read by BigQuery as columns (hive partitioning).

Environment variables:
    TMDB_TOKEN        TMDB API Read Access Token (injected from Secret Manager)
    BUCKET            target GCS bucket name
    COUNTRIES, YEAR_FROM, YEAR_TO, MIN_RUNTIME, CAST_TOP_N, MAX_MOVIES
                      optional overrides of config.toml for test runs
"""

import gzip
import json
import logging
import os
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from google.cloud import storage
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://api.themoviedb.org/3"
CONFIG_PATH = Path(__file__).with_name("config.toml")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("tmdb-scraper")


def load_config() -> dict:
    cfg = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    disc, trim = cfg["discover"], cfg["trim"]

    # Optional overrides for test runs
    if os.getenv("COUNTRIES"):
        disc["countries"] = [c.strip().upper() for c in os.environ["COUNTRIES"].split(",") if c.strip()]
    if os.getenv("YEAR_FROM"):
        disc["year_from"] = int(os.environ["YEAR_FROM"])
    disc["year_to"] = int(os.getenv("YEAR_TO", date.today().year + disc["years_ahead"]))
    if os.getenv("MIN_RUNTIME"):
        disc["min_runtime"] = int(os.environ["MIN_RUNTIME"])
    if os.getenv("CAST_TOP_N"):
        trim["cast_top_n"] = int(os.environ["CAST_TOP_N"])
    cfg["max_movies"] = int(os.getenv("MAX_MOVIES", "0")) or None
    return cfg


class RateLimiter:
    """Spaces requests evenly across all threads (at most N per second)."""

    def __init__(self, per_second: float):
        self.interval = 1 / per_second
        self.lock = threading.Lock()
        self.next_at = time.monotonic()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next_at)
            self.next_at = at + self.interval
        time.sleep(max(0.0, at - now))


class TmdbClient:
    """Thin TMDB client, one requests.Session per thread, shared rate limit."""

    def __init__(self, token: str, limiter: RateLimiter):
        self.token = token
        self.limiter = limiter
        self._local = threading.local()

    def _session(self) -> requests.Session:
        if not hasattr(self._local, "session"):
            session = requests.Session()
            session.headers.update({"accept": "application/json", "Authorization": f"Bearer {self.token}"})
            # Retry on rate limit (429) and server errors, respects Retry-After header
            retry = Retry(
                total=6,
                backoff_factor=1,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["GET"],
                respect_retry_after_header=True,
            )
            session.mount("https://", HTTPAdapter(max_retries=retry))
            self._local.session = session
        return self._local.session

    def get(self, path: str, params: dict) -> dict:
        self.limiter.wait()
        response = self._session().get(f"{BASE_URL}{path}", params=params, timeout=30)
        response.raise_for_status()
        return response.json()


class PartWriter:
    """Collects JSON lines and uploads them to GCS in numbered parts."""

    def __init__(self, bucket: storage.Bucket, prefix: str, per_part: int, use_gzip: bool):
        self.bucket, self.prefix, self.per_part, self.use_gzip = bucket, prefix, per_part, use_gzip
        self.lines: list[str] = []
        self.files: list[str] = []
        self.count = 0

    def add(self, obj: dict) -> None:
        self.lines.append(json.dumps(obj, ensure_ascii=False))
        self.count += 1
        if len(self.lines) >= self.per_part:
            self.flush()

    def flush(self) -> None:
        if not self.lines:
            return
        name = f"{self.prefix}/part-{len(self.files) + 1:04d}.jsonl" + (".gz" if self.use_gzip else "")
        payload = ("\n".join(self.lines) + "\n").encode("utf-8")
        if self.use_gzip:
            payload = gzip.compress(payload)
        self.bucket.blob(name).upload_from_string(payload, content_type="application/x-ndjson")
        log.info("uploaded gs://%s/%s (%d lines, %.1f MB)", self.bucket.name, name, len(self.lines), len(payload) / 1e6)
        self.files.append(name)
        self.lines = []


def discover_range(client, cfg, country, start, end, lists, segments) -> list[int]:
    """All film ids for a country in [start, end]; splits the range while over the TMDB limit."""
    disc = cfg["discover"]
    params = {
        "language": disc["language"],
        "include_adult": str(disc["include_adult"]).lower(),
        "include_video": str(disc["include_video"]).lower(),
        "with_runtime.gte": disc["min_runtime"],
        "with_origin_country": country,
        "primary_release_date.gte": start.isoformat(),
        "primary_release_date.lte": end.isoformat(),
        "sort_by": "primary_release_date.asc",
    }
    first = client.get("/discover/movie", {**params, "page": 1})
    total = first["total_results"]
    limit = cfg["api"]["max_results_per_query"]

    if total > limit and start < end:
        mid = start + timedelta(days=(end - start).days // 2)
        return (discover_range(client, cfg, country, start, mid, lists, segments)
                + discover_range(client, cfg, country, mid + timedelta(days=1), end, lists, segments))
    if total > limit:
        log.warning("%s %s: %d results in a single day, only first %d downloaded", country, start, total, limit)

    ids = []
    pages = min(first["total_pages"], limit // 20)
    for page in range(1, pages + 1):
        data = first if page == 1 else client.get("/discover/movie", {**params, "page": page})
        lists.add({"country": country, "query": {**params, "page": page}, "response": data})
        ids.extend(m["id"] for m in data.get("results", []))

    segments.append({"country": country, "from": start.isoformat(), "to": end.isoformat(), "total_results": total})
    if total:
        log.info("%s %s..%s: %d films, %d pages", country, start, end, total, pages)
    return ids


def trim_credits(movie: dict, trim: dict) -> dict:
    credits = movie.get("credits") or {}
    cast = sorted(credits.get("cast", []), key=lambda c: c.get("order", 0))[: trim["cast_top_n"]]
    jobs = set(trim["crew_jobs"])
    crew = [c for c in credits.get("crew", []) if c.get("job") in jobs]
    return {**movie, "credits": {"cast": cast, "crew": crew}}


def fetch_detail(client: TmdbClient, movie_id: int, cfg: dict) -> dict:
    data = client.get(
        f"/movie/{movie_id}",
        {"language": cfg["discover"]["language"], "append_to_response": "credits,keywords"},
    )
    return {**trim_credits(data, cfg["trim"]), "_fetched_at": datetime.now(timezone.utc).isoformat()}


def main() -> None:
    cfg = load_config()
    disc, out = cfg["discover"], cfg["output"]
    client = TmdbClient(os.environ["TMDB_TOKEN"].strip(), RateLimiter(cfg["api"]["max_requests_per_second"]))
    bucket = storage.Client().bucket(os.environ["BUCKET"])

    started = datetime.now(timezone.utc)
    run_prefix = f"{out['prefix']}/dt={started:%Y-%m-%d}/run={started:%Y-%m-%dT%H-%M-%SZ}"
    log.info("run %s | countries %s | years %d..%d", run_prefix, ",".join(disc["countries"]), disc["year_from"], disc["year_to"])

    # 1. Lists: all film ids per country
    lists = PartWriter(bucket, f"{run_prefix}/lists", out["list_pages_per_part"], out["gzip"])
    segments: list[dict] = []
    per_country: dict[str, int] = {}
    movie_ids: list[int] = []
    for country in disc["countries"]:
        ids = discover_range(client, cfg, country, date(disc["year_from"], 1, 1), date(disc["year_to"], 12, 31), lists, segments)
        per_country[country] = len(set(ids))
        movie_ids.extend(ids)
        log.info("%s: %d films", country, per_country[country])
    lists.flush()

    # Unique ids across countries (co-productions), keep first-seen order
    movie_ids = list(dict.fromkeys(movie_ids))
    if cfg["max_movies"]:
        movie_ids = movie_ids[: cfg["max_movies"]]
    log.info("unique films for details: %d", len(movie_ids))

    # 2. Details in parallel, uploaded in parts
    details = PartWriter(bucket, f"{run_prefix}/details", out["details_per_part"], out["gzip"])
    failures = []
    with ThreadPoolExecutor(max_workers=cfg["api"]["workers"]) as pool:
        futures = {pool.submit(fetch_detail, client, movie_id, cfg): movie_id for movie_id in movie_ids}
        for done, future in enumerate(as_completed(futures), start=1):
            try:
                details.add(future.result())
            except requests.RequestException as err:
                # Skip failed film, never reuse data from another film
                log.warning("movie %s failed: %s", futures[future], err)
                failures.append({"id": futures[future], "error": str(err)})
            if done % 1000 == 0:
                log.info("details %d/%d", done, len(movie_ids))
    details.flush()

    # 3. Manifest last: its presence marks the run as complete
    finished = datetime.now(timezone.utc)
    manifest = {
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_s": round((finished - started).total_seconds()),
        "config": cfg,
        "films_per_country": per_country,
        "unique_movie_ids": len(movie_ids),
        "list_pages": lists.count,
        "details_ok": details.count,
        "details_failed": failures,
        "segments": segments,
        "files": lists.files + details.files,
    }
    bucket.blob(f"{run_prefix}/manifest.json").upload_from_string(
        json.dumps(manifest, ensure_ascii=False, indent=2), content_type="application/json"
    )
    log.info("done in %ds: %d details, %d failed", manifest["duration_s"], details.count, len(failures))


if __name__ == "__main__":
    main()
