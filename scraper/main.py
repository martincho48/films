"""TMDB scraper for Cloud Run Job.

Downloads films from three sources, merges them by id and fetches the detail
of every film. All sources, filters and trimming rules are in config.toml.

    top_rated      /movie/top_rated (global top 10k)               mode full
    cinema         /movie/now_playing + /movie/upcoming per region  modes full, weekly
    new_releases   /discover/movie per origin country,              modes full, weekly
                   release date in [today - days_back, today + days_ahead]

Detail: /movie/{id}?append_to_response=credits,keywords,release_dates,
credits trimmed to top N cast and selected crew jobs, everything else as is,
plus _sources (which lists the film came from) and _fetched_at.

Output to GCS, uploaded continuously in parts:

    gs://<BUCKET>/tmdb/mode=full|weekly/run=YYYY-MM-DDTHH-MM-SSZ/
        lists/part-0001.jsonl      # raw list pages + source and query that produced them
        details/part-0001.jsonl    # one trimmed movie detail per line
        manifest.json              # written last = run is complete

   key=value folders are read by BigQuery as columns (hive partitioning).

Environment variables:
    TMDB_TOKEN        TMDB API Read Access Token (injected from Secret Manager)
    BUCKET            target GCS bucket name
    MODE              full | weekly (default weekly)
    MAX_MOVIES, TOP_RATED_PAGES   optional limits for test runs
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


class SafetyStop(RuntimeError):
    """Raised by a safety limit; the run ends without manifest.json."""


def load_config() -> dict:
    cfg = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg["mode"] = os.getenv("MODE", "weekly")
    if cfg["mode"] not in cfg["modes"]:
        raise ValueError(f"unknown MODE {cfg['mode']!r}, expected one of {list(cfg['modes'])}")

    # Optional limits for test runs
    if os.getenv("TOP_RATED_PAGES"):
        cfg["top_rated"]["pages"] = int(os.environ["TOP_RATED_PAGES"])
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
    """Thin TMDB client, one requests.Session per thread, shared rate limit and deadline."""

    def __init__(self, token: str, limiter: RateLimiter, deadline: float):
        self.token = token
        self.limiter = limiter
        self.deadline = deadline
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
        if time.monotonic() > self.deadline:
            raise SafetyStop("max_runtime_minutes exceeded")
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


class Collector:
    """Film ids from all list sources, with the source tags each film came from."""

    def __init__(self, lists: PartWriter):
        self.lists = lists
        self.sources: dict[int, list[str]] = {}
        self.per_source: dict[str, int] = {}

    def add_page(self, tag: str, query: dict, data: dict) -> None:
        self.lists.add({"source": tag, "query": query, "response": data})
        for movie in data.get("results", []):
            tags = self.sources.setdefault(movie["id"], [])
            if tag not in tags:
                tags.append(tag)
                self.per_source[tag] = self.per_source.get(tag, 0) + 1


def fetch_pages(client, collector, tag, path, params, max_pages) -> None:
    """All pages of a list endpoint (up to max_pages and the TMDB limit of 500)."""
    page, total_pages = 1, 1
    while page <= min(total_pages, max_pages, 500):
        query = {**params, "page": page}
        data = client.get(path, query)
        collector.add_page(tag, query, data)
        total_pages = data.get("total_pages", 0)
        page += 1
    log.info("%s: %d films, %d pages", tag, collector.per_source.get(tag, 0), page - 1)


def discover_range(client, cfg, collector, country, start, end, segments) -> None:
    """New releases of one origin country in [start, end]; splits the range while over the TMDB limit."""
    nr = cfg["new_releases"]
    params = {
        "language": cfg["api"]["language"],
        "include_adult": str(nr["include_adult"]).lower(),
        "include_video": str(nr["include_video"]).lower(),
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
        discover_range(client, cfg, collector, country, start, mid, segments)
        discover_range(client, cfg, collector, country, mid + timedelta(days=1), end, segments)
        return
    if total > limit:
        log.warning("%s %s: %d results in a single day, only first %d downloaded", country, start, total, limit)

    tag = f"new_release:{country}"
    pages = min(first["total_pages"], limit // 20)
    for page in range(1, pages + 1):
        data = first if page == 1 else client.get("/discover/movie", {**params, "page": page})
        collector.add_page(tag, {**params, "page": page}, data)
    segments.append({"country": country, "from": start.isoformat(), "to": end.isoformat(), "total_results": total})


def trim_credits(movie: dict, trim: dict) -> dict:
    credits = movie.get("credits") or {}
    cast = sorted(credits.get("cast", []), key=lambda c: c.get("order", 0))[: trim["cast_top_n"]]
    jobs = set(trim["crew_jobs"])
    crew = [c for c in credits.get("crew", []) if c.get("job") in jobs]
    return {**movie, "credits": {"cast": cast, "crew": crew}}


def fetch_detail(client: TmdbClient, movie_id: int, sources: list[str], cfg: dict) -> dict:
    data = client.get(
        f"/movie/{movie_id}",
        {"language": cfg["api"]["language"], "append_to_response": cfg["api"]["append_to_response"]},
    )
    return {
        **trim_credits(data, cfg["trim"]),
        "_sources": sources,
        "_fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> None:
    cfg = load_config()
    mode, out, api = cfg["mode"], cfg["output"], cfg["api"]
    sources_enabled = cfg["modes"][mode]

    deadline = time.monotonic() + cfg["safety"]["max_runtime_minutes"] * 60
    client = TmdbClient(os.environ["TMDB_TOKEN"].strip(), RateLimiter(api["max_requests_per_second"]), deadline)
    bucket = storage.Client().bucket(os.environ["BUCKET"])

    started = datetime.now(timezone.utc)
    run_prefix = f"{out['prefix']}/mode={mode}/run={started:%Y-%m-%dT%H-%M-%SZ}"
    log.info("run %s | sources %s", run_prefix, ",".join(sources_enabled))

    # 1. Lists from all enabled sources
    lists = PartWriter(bucket, f"{run_prefix}/lists", out["list_pages_per_part"], out["gzip"])
    collector = Collector(lists)
    segments: list[dict] = []

    if "top_rated" in sources_enabled:
        fetch_pages(client, collector, "top_rated", "/movie/top_rated",
                    {"language": api["language"]}, cfg["top_rated"]["pages"])

    if "cinema" in sources_enabled:
        for region in cfg["cinema"]["regions"]:
            for kind in ("now_playing", "upcoming"):
                fetch_pages(client, collector, f"{kind}:{region}", f"/movie/{kind}",
                            {"language": api["language"], "region": region}, 500)

    if "new_releases" in sources_enabled:
        nr = cfg["new_releases"]
        today = started.date()
        start, end = today - timedelta(days=nr["days_back"]), today + timedelta(days=nr["days_ahead"])
        for country in nr["countries"]:
            discover_range(client, cfg, collector, country, start, end, segments)
            log.info("new_release:%s: %d films", country, collector.per_source.get(f"new_release:{country}", 0))
    lists.flush()

    movie_ids = list(collector.sources)
    if cfg["max_movies"]:
        movie_ids = movie_ids[: cfg["max_movies"]]
    log.info("unique films for details: %d", len(movie_ids))

    # 2. Details in parallel, uploaded in parts
    details = PartWriter(bucket, f"{run_prefix}/details", out["details_per_part"], out["gzip"])
    failures = []
    consecutive_failures = 0
    pool = ThreadPoolExecutor(max_workers=api["workers"])
    futures = {pool.submit(fetch_detail, client, mid, collector.sources[mid], cfg): mid for mid in movie_ids}
    try:
        for done, future in enumerate(as_completed(futures), start=1):
            try:
                details.add(future.result())
                consecutive_failures = 0
            except requests.RequestException as err:
                # Skip failed film, never reuse data from another film
                log.warning("movie %s failed: %s", futures[future], err)
                failures.append({"id": futures[future], "error": str(err)})
                consecutive_failures += 1
                if consecutive_failures >= cfg["safety"]["max_consecutive_failures"]:
                    raise SafetyStop(f"{consecutive_failures} consecutive detail failures")
            if done % 1000 == 0:
                log.info("details %d/%d", done, len(movie_ids))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    details.flush()

    # 3. Manifest last: its presence marks the run as complete
    finished = datetime.now(timezone.utc)
    manifest = {
        "mode": mode,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_s": round((finished - started).total_seconds()),
        "config": cfg,
        "films_per_source": collector.per_source,
        "unique_movie_ids": len(movie_ids),
        "list_pages": lists.count,
        "details_ok": details.count,
        "details_failed": failures,
        "new_release_segments": segments,
        "files": lists.files + details.files,
    }
    # Single line, so BigQuery can read manifests as newline-delimited JSON
    bucket.blob(f"{run_prefix}/manifest.json").upload_from_string(
        json.dumps(manifest, ensure_ascii=False), content_type="application/json"
    )
    log.info("done in %ds: %d details, %d failed", manifest["duration_s"], details.count, len(failures))


if __name__ == "__main__":
    try:
        main()
    except SafetyStop as err:
        log.error("safety stop: %s (run left without manifest.json)", err)
        sys.exit(1)
