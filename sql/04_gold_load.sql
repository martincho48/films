-- GOLD: načtení posledního úplného běhu ze silver.movies.
-- Automaticky: BigQuery Scheduled Query "gold-load" každý den. Když je poslední běh už
-- načtený, skript hned skončí, takže gold se mění jen jednou za každý nový běh scraperu.
-- Ručně: bq query --use_legacy_sql=false --location=us-central1 se vstupem z tohoto souboru
--   (Windows: přes cmd /c a přesměrování vstupu kvůli BOM, viz README krok 1.8).
-- Pozor: v tomto souboru nepoužívat znaky menší/větší než, svislou čáru ani ampersand.
-- Při aktualizaci Scheduled Query přes bq.cmd je Windows cmd bere jako přesměrování.
--
-- Příznaky zdrojů:
--   is_top_rated                          mění jen běh full (weekly top 10k nestahuje)
--   in_cinema_*, upcoming_*, is_new_release  mění každý běh (full i weekly obsahují kina a novinky)
--   in_catalog                            film je aspoň v jednom zdroji

DECLARE v_run STRING;
DECLARE v_run_at TIMESTAMP;
DECLARE v_is_full BOOL;

-- Poslední úplný běh z malé tabulky manifestů (stejná logika jako v silver.movies),
-- aby denní kontrola nemusela číst detaily filmů
SET (v_run, v_run_at, v_is_full) = (
  SELECT AS STRUCT run, SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H-%M-%SZ', run), mode = 'full'
  FROM `films-512811.bronze.tmdb_manifests`
  ORDER BY run DESC
  LIMIT 1
);

-- Poslední běh už je načtený (má snapshoty), takže nic nedělat
IF EXISTS (SELECT 1 FROM `films-512811.gold.movie_snapshots` WHERE run = v_run) THEN
  SELECT FORMAT('run %s already loaded, nothing to do', v_run) AS status;
  RETURN;
END IF;

-- 1. Historie: snapshot každého filmu z tohoto běhu
INSERT INTO `films-512811.gold.movie_snapshots`
SELECT
  run_at, run, run_mode, id, title,
  status, release_date, release_date_cz, release_date_us,
  vote_average, vote_count, popularity,
  sources, is_top_rated, in_cinema_us, in_cinema_cz, upcoming_us, upcoming_cz, is_new_release
FROM `films-512811.silver.movies`;

-- 2. Katalog: aktuální stav
MERGE `films-512811.gold.movies` T
USING `films-512811.silver.movies` S
ON T.id = S.id

WHEN MATCHED THEN UPDATE SET
  imdb_id = S.imdb_id, title = S.title, original_title = S.original_title,
  original_language = S.original_language, overview = S.overview, tagline = S.tagline,
  status = S.status, release_date = S.release_date, runtime = S.runtime,
  budget = S.budget, revenue = S.revenue, popularity = S.popularity,
  vote_average = S.vote_average, vote_count = S.vote_count, homepage = S.homepage,
  poster_url = S.poster_url, origin_country = S.origin_country, genres = S.genres,
  keywords = S.keywords, production_companies = S.production_companies,
  production_countries = S.production_countries, spoken_languages = S.spoken_languages,
  collection = S.collection, directors = S.directors, writers = S.writers,
  producers = S.producers, cinematographers = S.cinematographers, composers = S.composers,
  editors = S.editors, top_cast = S.top_cast,
  release_date_cz = S.release_date_cz, release_date_us = S.release_date_us,
  is_top_rated = IF(v_is_full, S.is_top_rated, T.is_top_rated),
  in_cinema_us = S.in_cinema_us, in_cinema_cz = S.in_cinema_cz,
  upcoming_us = S.upcoming_us, upcoming_cz = S.upcoming_cz,
  is_new_release = S.is_new_release,
  in_catalog = TRUE,
  last_seen_at = S.run_at,
  updated_at = CURRENT_TIMESTAMP()

WHEN NOT MATCHED BY TARGET THEN INSERT (
  id, imdb_id, title, original_title, original_language, overview, tagline, status,
  release_date, runtime, budget, revenue, popularity, vote_average, vote_count, homepage,
  poster_url, origin_country, genres, keywords, production_companies, production_countries,
  spoken_languages, collection, directors, writers, producers, cinematographers, composers,
  editors, top_cast, release_date_cz, release_date_us,
  is_top_rated, in_cinema_us, in_cinema_cz, upcoming_us, upcoming_cz, is_new_release,
  in_catalog, first_seen_at, last_seen_at, updated_at
) VALUES (
  S.id, S.imdb_id, S.title, S.original_title, S.original_language, S.overview, S.tagline, S.status,
  S.release_date, S.runtime, S.budget, S.revenue, S.popularity, S.vote_average, S.vote_count, S.homepage,
  S.poster_url, S.origin_country, S.genres, S.keywords, S.production_companies, S.production_countries,
  S.spoken_languages, S.collection, S.directors, S.writers, S.producers, S.cinematographers, S.composers,
  S.editors, S.top_cast, S.release_date_cz, S.release_date_us,
  S.is_top_rated, S.in_cinema_us, S.in_cinema_cz, S.upcoming_us, S.upcoming_cz, S.is_new_release,
  TRUE, S.run_at, S.run_at, CURRENT_TIMESTAMP()
)

-- Film v tomto běhu chybí: už neběží v kinech / není novinka; po běhu full ani v top 10k.
-- Nemaže se, jen se vypnou příznaky.
WHEN NOT MATCHED BY SOURCE AND (
  T.in_cinema_us OR T.in_cinema_cz OR T.upcoming_us OR T.upcoming_cz OR T.is_new_release
  OR (v_is_full AND T.is_top_rated)
) THEN UPDATE SET
  in_cinema_us = FALSE, in_cinema_cz = FALSE, upcoming_us = FALSE, upcoming_cz = FALSE,
  is_new_release = FALSE,
  is_top_rated = IF(v_is_full, FALSE, T.is_top_rated),
  in_catalog = IF(v_is_full, FALSE, T.is_top_rated),
  updated_at = CURRENT_TIMESTAMP();
