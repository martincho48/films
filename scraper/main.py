"""TMDB scraper for Cloud Run Job.

Downloads raw TMDB responses (discover, upcoming, movie details) and stores
them unchanged as JSON in GCS under a timestamped run folder:

    gs://<BUCKET>/raw/tmdb/dt=YYYY-MM-DD/run_YYYY-MM-DDTHH-MM-SSZ/
        discover_page_001.json
        upcoming_page_001.json
        movie_details.jsonl
        manifest.json

Config via environment variables:
    TMDB_TOKEN        TMDB API Read Access Token (injected from Secret Manager)
    BUCKET            target GCS bucket name
    DISCOVER_PAGES    number of /discover/movie pages (20 films each), default 1
    UPCOMING_PAGES    number of /movie/upcoming pages (20 films each), default 1
    LANGUAGE          TMDB language, default en-US
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import requests
from google.cloud import storage
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://api.themoviedb.org/3"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("tmdb-scraper")


def make_session(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({"accept": "application/json", "Authorization": f"Bearer {token}"})

    # Retry on rate limit (429) and server errors, respects Retry-After header
    retry = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def get_json(session: requests.Session, path: str, params: dict) -> dict:
    response = session.get(f"{BASE_URL}{path}", params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def fetch_list_pages(session: requests.Session, path: str, pages: int, params: dict) -> list[dict]:
    results = []
    for page in range(1, pages + 1):
        data = get_json(session, path, {**params, "page": page})
        results.append(data)
        log.info("%s page %d/%d: %d films", path, page, data.get("total_pages", 0), len(data.get("results", [])))
        if page >= data.get("total_pages", 0):
            break
    return results


def fetch_details(session: requests.Session, movie_ids: list[int], language: str) -> tuple[list[dict], list[dict]]:
    details, failures = [], []
    for i, movie_id in enumerate(movie_ids, start=1):
        try:
            data = get_json(
                session,
                f"/movie/{movie_id}",
                {"language": language, "append_to_response": "credits,keywords"},
            )
            details.append(data)
        except requests.RequestException as err:
            # Skip failed film, never reuse data from a previous iteration
            log.warning("movie %s failed: %s", movie_id, err)
            failures.append({"id": movie_id, "error": str(err)})

        # TMDB allows roughly 40-50 requests/s, stay well below
        time.sleep(0.05)
        if i % 20 == 0:
            log.info("details %d/%d", i, len(movie_ids))
    return details, failures


def upload(bucket: storage.Bucket, path: str, content: str, content_type: str) -> None:
    bucket.blob(path).upload_from_string(content, content_type=content_type)
    log.info("uploaded gs://%s/%s", bucket.name, path)


def main() -> None:
    token = os.environ["TMDB_TOKEN"].strip()
    bucket_name = os.environ["BUCKET"]
    discover_pages = int(os.getenv("DISCOVER_PAGES", "1"))
    upcoming_pages = int(os.getenv("UPCOMING_PAGES", "1"))
    language = os.getenv("LANGUAGE", "en-US")

    started = datetime.now(timezone.utc)
    run_prefix = f"raw/tmdb/dt={started:%Y-%m-%d}/run_{started:%Y-%m-%dT%H-%M-%SZ}"

    session = make_session(token)
    bucket = storage.Client().bucket(bucket_name)

    discover = fetch_list_pages(
        session,
        "/discover/movie",
        discover_pages,
        {"language": language, "sort_by": "popularity.desc", "include_adult": "false"},
    )
    upcoming = fetch_list_pages(session, "/movie/upcoming", upcoming_pages, {"language": language})

    # Unique ids across both sources, keep first-seen order
    movie_ids = list(dict.fromkeys(m["id"] for page in discover + upcoming for m in page.get("results", [])))
    log.info("unique films for details: %d", len(movie_ids))

    details, failures = fetch_details(session, movie_ids, language)

    for n, page in enumerate(discover, start=1):
        upload(bucket, f"{run_prefix}/discover_page_{n:03d}.json", json.dumps(page, ensure_ascii=False), "application/json")
    for n, page in enumerate(upcoming, start=1):
        upload(bucket, f"{run_prefix}/upcoming_page_{n:03d}.json", json.dumps(page, ensure_ascii=False), "application/json")

    jsonl = "\n".join(json.dumps(d, ensure_ascii=False) for d in details)
    upload(bucket, f"{run_prefix}/movie_details.jsonl", jsonl, "application/x-ndjson")

    manifest = {
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "language": language,
        "discover_pages": len(discover),
        "upcoming_pages": len(upcoming),
        "unique_movie_ids": len(movie_ids),
        "details_ok": len(details),
        "details_failed": failures,
    }
    upload(bucket, f"{run_prefix}/manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2), "application/json")

    log.info("done: %d details, %d failed", len(details), len(failures))


if __name__ == "__main__":
    main()
