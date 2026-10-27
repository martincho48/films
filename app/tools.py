"""Tools the agent can call. Each tool = Python function with SQL over gold.movies
+ a declaration (name, description, parameters) for Gemini.

The model only sees the declarations and returns which tool to call with which
arguments. Query results go straight to the UI, never back to the model.

Combinations are handled by parameters, not by more tools: search_movies has
optional filters and the SQL is assembled from fixed fragments, values always
go in as query parameters.
"""

from datetime import date

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

# A film first released more than this many years ago is a re-release when it shows up
# in cinemas. 1 year would also catch festival films that reach cinemas a year later.
RERELEASE_YEARS = 2

# Rating of a film with a handful of votes says nothing; applies to min_rating and sort_by=rating
MIN_VOTES = 50

# Fixed column names per country; never built from user input
CINEMA = {
    "CZ": {"now_playing": "in_cinema_cz", "upcoming": "upcoming_cz", "premiere": "release_date_cz"},
    "US": {"now_playing": "in_cinema_us", "upcoming": "upcoming_us", "premiere": "release_date_us"},
}

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


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def search_movies(
    genres: list[str] | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    min_rating: float | None = None,
    person: str | None = None,
    cinema: str | None = None,
    country: str = "CZ",
    premiere_from: str | None = None,
    premiere_to: str | None = None,
    new_only: bool = False,
    sort_by: str | None = None,
    limit: int = 20,
) -> pd.DataFrame:
    if country not in CINEMA:
        raise ValueError(f"unknown country {country!r}")
    if cinema not in (None, "now_playing", "upcoming"):
        raise ValueError(f"unknown cinema {cinema!r}")
    for g in genres or []:
        if g not in GENRES:
            raise ValueError(f"unknown genre {g!r}")
    p_from, p_to = _date(premiere_from), _date(premiere_to)
    if (p_from or p_to) and not cinema:
        raise ValueError("premiere_from/premiere_to need cinema")
    if p_from and p_to and p_from > p_to:
        raise ValueError(f"premiere_from {premiere_from} is after premiere_to {premiere_to}")
    if year_from and year_to and year_from > year_to:
        raise ValueError(f"year_from {year_from} is after year_to {year_to}")
    # TMDB 'upcoming' also lists re-releases of old films (e.g. Rocky 1976) and films
    # already out, so upcoming always means premiere from today on
    if cinema == "upcoming":
        p_from = max(p_from or date.today(), date.today())
    sort_by = sort_by or ("premiere" if cinema == "upcoming" else "popularity")
    if sort_by == "premiere" and not cinema:
        sort_by = "year"
    limit = max(1, min(int(limit), MAX_LIMIT))

    premiere = CINEMA[country]["premiere"]
    # release_date = first release anywhere (TMDB), i.e. the year the film was made
    rerelease = f"IFNULL(release_date < DATE_SUB(CURRENT_DATE(), INTERVAL {RERELEASE_YEARS} YEAR), FALSE)"
    # people of the film with their role; names compared lowercase and without diacritics
    # on both sides, so 'Kieslowski' finds 'Kieślowski' and 'sverak' finds 'Svěrák'
    people = """(
        SELECT d AS name, 'režie' AS role FROM UNNEST(directors) d
        UNION ALL SELECT w, 'scénář' FROM UNNEST(writers) w
        UNION ALL SELECT c.name, 'herec' FROM UNNEST(top_cast) c)"""
    fold = lambda x: rf"REGEXP_REPLACE(NORMALIZE(LOWER({x}), NFD), r'\p{{M}}', '')"  # noqa: E731
    person_match = f"{fold('name')} LIKE CONCAT('%', {fold('@person')}, '%')"

    where = ["in_catalog"]
    params = [bigquery.ScalarQueryParameter("limit", "INT64", limit)]
    if genres:
        # all listed genres must match ("sci-fi comedy" = both)
        where.append("(SELECT COUNT(DISTINCT g) FROM UNNEST(genres) g WHERE g IN UNNEST(@genres)) = ARRAY_LENGTH(@genres)")
        params.append(bigquery.ArrayQueryParameter("genres", "STRING", sorted(set(genres))))
    if year_from:
        where.append("EXTRACT(YEAR FROM release_date) >= @year_from")
        params.append(bigquery.ScalarQueryParameter("year_from", "INT64", int(year_from)))
    if year_to:
        where.append("EXTRACT(YEAR FROM release_date) <= @year_to")
        params.append(bigquery.ScalarQueryParameter("year_to", "INT64", int(year_to)))
    if min_rating is not None:
        where.append("vote_average >= @min_rating")
        params.append(bigquery.ScalarQueryParameter("min_rating", "FLOAT64", float(min_rating)))
    if min_rating is not None or sort_by == "rating":
        where.append(f"vote_count >= {MIN_VOTES}")
    if person:
        # part of the name: 'Nolan' finds Christopher Nolan and also Nolan North
        where.append(f"EXISTS (SELECT 1 FROM {people} WHERE {person_match})")
        params.append(bigquery.ScalarQueryParameter("person", "STRING", person.strip()))
    if cinema:
        where.append(CINEMA[country][cinema])
    if p_from:
        where.append(f"{premiere} >= @premiere_from")
        params.append(bigquery.ScalarQueryParameter("premiere_from", "DATE", p_from))
    if p_to:
        where.append(f"{premiere} <= @premiere_to")
        params.append(bigquery.ScalarQueryParameter("premiere_to", "DATE", p_to))
    if new_only:
        where.append(f"NOT {rerelease}")

    order = {
        "popularity": "popularity DESC",
        "rating": "vote_average DESC, vote_count DESC",
        "premiere": f"{premiere}, popularity DESC",
        "year": "release_date DESC",
    }
    if sort_by not in order:
        raise ValueError(f"unknown sort_by {sort_by!r}")

    cinema_cols = f"{premiere} AS premiere, {rerelease} AS rerelease," if cinema else ""
    # who matched person and in which role, e.g. 'Christopher Nolan (režie, scénář)'
    match_col = f"""ARRAY_TO_STRING(ARRAY(
            SELECT FORMAT('%s (%s)', name, STRING_AGG(DISTINCT role, ', '))
            FROM {people} WHERE {person_match} GROUP BY name), '; ') AS `match`,""" if person else ""
    sql = f"""
        SELECT title, EXTRACT(YEAR FROM release_date) AS year, {cinema_cols} {match_col}
               vote_average, vote_count, genres, directors, overview
        FROM {MOVIES}
        WHERE {' AND '.join(where)}
        ORDER BY {order[sort_by]}
        LIMIT @limit
    """
    return _query(sql, params)


# name -> (function, declaration for Gemini as JSON schema)
TOOLS = {
    "search_movies": (
        search_movies,
        {
            "description": (
                "Search the movie catalog. Combine any filters the user mentions in one call; "
                "leave out filters the user did not ask for."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "genres": {
                        "type": "array",
                        "items": {"type": "string", "enum": GENRES},
                        "description": "Genres in English, all must match. E.g. 'komedie' -> ['Comedy'], 'sci-fi horor' -> ['Science Fiction', 'Horror'].",
                    },
                    "year_from": {"type": "integer", "description": "Film made in this year or later. E.g. '90s' -> 1990."},
                    "year_to": {"type": "integer", "description": "Film made in this year or earlier. E.g. '90s' -> 1999."},
                    "min_rating": {"type": "number", "description": "Minimum average rating on a 0-10 scale."},
                    "person": {
                        "type": "string",
                        "description": (
                            "Name of a director, writer or actor exactly as the user wrote it, only converted to the "
                            "basic form (nominative): 'Nolana' -> 'Nolan', 'Tomem Hanksem' -> 'Tom Hanks'. "
                            "Do not add first names or guess who is meant."
                        ),
                    },
                    "cinema": {
                        "type": "string",
                        "enum": ["now_playing", "upcoming"],
                        "description": "Only for questions about cinemas: now_playing = in cinemas now, upcoming = cinema premiere is still ahead.",
                    },
                    "country": {
                        "type": "string",
                        "enum": list(CINEMA),
                        "description": "Cinemas of which country, only with cinema. CZ is the default when no country is mentioned.",
                    },
                    "premiere_from": {
                        "type": "string",
                        "format": "date",
                        "description": "Cinema premiere on or after this date, YYYY-MM-DD, only with cinema. Resolve relative dates like 'next week' from today's date.",
                    },
                    "premiere_to": {
                        "type": "string",
                        "format": "date",
                        "description": "Cinema premiere on or before this date, YYYY-MM-DD, only with cinema.",
                    },
                    "new_only": {
                        "type": "boolean",
                        "description": f"True = without re-releases of films older than {RERELEASE_YEARS} years. Use when the user asks for new films in cinemas.",
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": ["popularity", "rating", "premiere", "year"],
                        "description": "popularity (default) = what people watch now, rating = best rated, premiere = nearest cinema premiere first, year = newest films first.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            f"How many movies to return, 1-{MAX_LIMIT}. Only when the user asks for a specific number "
                            "(e.g. 'dej mi 5 westernů' -> 5). Otherwise leave it out."
                        ),
                    },
                },
            },
        },
    ),
}


def run_tool(name: str, args: dict) -> pd.DataFrame:
    if name not in TOOLS:
        raise ValueError(f"unknown tool {name!r}")
    return TOOLS[name][0](**args)
