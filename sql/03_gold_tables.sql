-- GOLD: finální tabulky pro aplikaci a Gemini. Vytvoří se jen jednou (IF NOT EXISTS).
-- Spuštění (Windows: přes cmd kvůli BOM):
--   cmd /c "bq query --use_legacy_sql=false --location=us-central1 < sql/03_gold_tables.sql"
-- Schéma se bere ze silver.movies (WHERE FALSE = žádná data, jen sloupce a typy).

-- Katalog: 1 řádek na film, aktuální stav. Plní se přes MERGE (sql/04_gold_load.sql).
CREATE TABLE IF NOT EXISTS `films-512811.gold.movies`
CLUSTER BY id
OPTIONS (description = 'Movie catalog for the app: one row per film, current state (MERGE after every scraper run).')
AS
SELECT
  * EXCEPT (sources, run_mode, run, run_at, fetched_at),
  TRUE AS in_catalog,                          -- film je aspoň v jednom zdroji
  CAST(NULL AS TIMESTAMP) AS first_seen_at,    -- první běh s filmem, nikdy se nepřepisuje
  CAST(NULL AS TIMESTAMP) AS last_seen_at,     -- poslední běh s filmem
  CAST(NULL AS TIMESTAMP) AS updated_at        -- kdy MERGE řádek naposledy změnil
FROM `films-512811.silver.movies`
WHERE FALSE;

-- Historie: 1 řádek na film a běh. Jen přidávat, nikdy nemazat ani nepřestavovat z raw
-- (raw drží ~5 měsíců, historie je jen tady).
CREATE TABLE IF NOT EXISTS `films-512811.gold.movie_snapshots`
PARTITION BY DATE(run_at)
CLUSTER BY id
OPTIONS (description = 'Movie history: one row per film and scraper run. Append-only, never rebuild from raw.')
AS
SELECT
  run_at, run, run_mode, id, title,
  status, release_date, release_date_cz, release_date_us,
  vote_average, vote_count, popularity,
  sources, is_top_rated, in_cinema_us, in_cinema_cz, upcoming_us, upcoming_cz, is_new_release
FROM `films-512811.silver.movies`
WHERE FALSE;
