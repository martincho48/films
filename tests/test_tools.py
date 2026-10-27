"""Tests of search_movies without the model: SQL against real BigQuery (small
queries, free tier) and input validation (no query at all).

    pytest tests/test_tools.py
"""

from datetime import date

import pytest

from tools import MIN_VOTES, run_tool, search_movies


def test_genres_all_must_match():
    df = search_movies(genres=["Science Fiction", "Comedy"], limit=50)
    assert len(df) > 0
    assert all({"Science Fiction", "Comedy"} <= set(g) for g in df.genres)


def test_years():
    df = search_movies(year_from=1990, year_to=1999, limit=50)
    assert len(df) > 0
    assert df.year.between(1990, 1999).all()


def test_rating_needs_enough_votes():
    df = search_movies(min_rating=8, limit=50)
    assert len(df) > 0
    assert (df.vote_average >= 8).all()
    assert (df.vote_count >= MIN_VOTES).all()


def test_person_any_case():
    df = search_movies(person="christopher nolan", limit=50)
    assert any("Christopher Nolan" in d for d in df.directors)


def test_person_without_diacritics():
    # data have 'Krzysztof Kieślowski'; the model or the user may write it without 'ś'
    df = search_movies(person="Kieslowski", limit=50)
    assert "A Short Film About Love" in set(df.title)


def test_person_match_column_shows_who_and_role():
    df = search_movies(person="Nolan", limit=50)
    assert any("Christopher Nolan (" in m and "režie" in m for m in df["match"])
    # 'Nolan' is also a first name (Nolan North), the match column makes that visible
    assert any("Nolan North" in m for m in df["match"])


def test_now_playing_has_cinema_columns():
    df = search_movies(cinema="now_playing", limit=50)
    assert len(df) > 0
    assert {"premiere", "rerelease"} <= set(df.columns)


def test_new_only_drops_rereleases():
    all_ = search_movies(cinema="now_playing", limit=50)
    new = search_movies(cinema="now_playing", new_only=True, limit=50)
    assert all_.rerelease.any(), "no re-release in cinemas, test data changed"
    assert not new.rerelease.any()


def test_upcoming_is_from_today():
    df = search_movies(cinema="upcoming", limit=50)
    assert len(df) > 0
    assert (df.premiere >= date.today()).all()


def test_premiere_range():
    df = search_movies(cinema="upcoming", country="US", premiere_to="2099-12-31", limit=50)
    assert len(df) > 0
    assert (df.premiere <= date(2099, 12, 31)).all()


@pytest.mark.parametrize("args", [
    {"genres": ["Komedie"]},
    {"cinema": "soon"},
    {"country": "DE"},
    {"premiere_from": "2026-11-01"},                     # premiere without cinema
    {"cinema": "upcoming", "premiere_from": "next week"},
    {"cinema": "upcoming", "premiere_from": "2026-12-01", "premiere_to": "2026-11-01"},
    {"year_from": 2000, "year_to": 1990},
    {"sort_by": "title"},
])
def test_invalid_input_rejected(args):
    with pytest.raises(ValueError):
        search_movies(**args)


def test_unknown_parameter_rejected():
    # the model made up 'genre' once; it must fail, not be silently dropped
    with pytest.raises(TypeError):
        run_tool("search_movies", {"genre": "Horror"})
