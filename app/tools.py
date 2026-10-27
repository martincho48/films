"""Tools the agent can call. Each tool = Python function with a fixed SQL query
over gold.movies + a declaration (name, description, parameters) for Gemini.

The model only sees the declarations and returns which tool to call with which
arguments. Query results go straight to the UI, never back to the model.
"""

import pandas as pd
from google.cloud import bigquery

PROJECT = "films-512811"
MOVIES = f"`{PROJECT}.gold.movies`"
MAX_BYTES_BILLED = 100 * 1024**2  # 100 MB per query, the whole table is far smaller
MAX_LIMIT = 50

# Values as they are in gold.movies.genres (TMDB, English)
GENRES = [
    "Action", "Adventure", "Animation", "Comedy", "Crime", "Documentary", "Drama",
    "Family", "Fantasy", "History", "Horror", "Music", "Mystery", "Romance",
    "Science Fiction", "Thriller", "TV Movie", "War", "Western",
]

COLUMNS = "title, release_date, vote_average, vote_count, genres, overview"

_client: bigquery.Client | None = None


def _bq() -> bigquery.Client:
    global _client
    if _client is None:
        _client = bigquery.Client(project=PROJECT)
    return _client


def _query(sql: str, params: list) -> pd.DataFrame:
    config = bigquery.QueryJobConfig(query_parameters=params, maximum_bytes_billed=MAX_BYTES_BILLED)
    # small results, plain REST is enough (no BigQuery Storage API)
    return _bq().query(sql, job_config=config).to_dataframe(create_bqstorage_client=False)


def search_by_genre(genre: str, limit: int = 20) -> pd.DataFrame:
    if genre not in GENRES:
        raise ValueError(f"unknown genre {genre!r}")
    limit = max(1, min(int(limit), MAX_LIMIT))
    sql = f"""
        SELECT {COLUMNS}
        FROM {MOVIES}
        WHERE in_catalog AND @genre IN UNNEST(genres)
        ORDER BY popularity DESC
        LIMIT @limit
    """
    return _query(sql, [
        bigquery.ScalarQueryParameter("genre", "STRING", genre),
        bigquery.ScalarQueryParameter("limit", "INT64", limit),
    ])


# name -> (function, declaration for Gemini as JSON schema)
TOOLS = {
    "search_by_genre": (
        search_by_genre,
        {
            "description": "Find movies of one genre, most popular first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "genre": {
                        "type": "string",
                        "enum": GENRES,
                        "description": "Genre in English, e.g. 'komedie' -> 'Comedy', 'sci-fi' -> 'Science Fiction'.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"How many movies to return, 1-{MAX_LIMIT}. Default 20.",
                    },
                },
                "required": ["genre"],
            },
        },
    ),
}


def run_tool(name: str, args: dict) -> pd.DataFrame:
    if name not in TOOLS:
        raise ValueError(f"unknown tool {name!r}")
    return TOOLS[name][0](**args)
