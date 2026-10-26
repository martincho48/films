-- SILVER: vyčištěné filmy z posledního úplného běhu scraperu (view, nic neukládá).
-- Spuštění (Windows: přes cmd kvůli BOM):
--   cmd /c "bq query --use_legacy_sql=false --location=us-central1 < sql/02_silver_movies.sql"
--
-- Řádky:   jen poslední úplný běh (má manifest), každý film jednou.
-- Filtr:   top 10k projde vždy. Novinky a kina jen s popisem, plakátem
--          a runtime >= 40 nebo neznámým (chystané filmy délku často nemají).
-- Sloupce: 0 a prázdné texty -> NULL, typy, seznamy objektů -> seznamy jmen,
--          štáb rozdělený podle rolí, premiéra v kinech CZ a US, příznaky zdrojů.

CREATE OR REPLACE VIEW `films-512811.silver.movies`
OPTIONS (description = 'Cleaned TMDB movies from the latest complete scraper run.')
AS
WITH latest_run AS (
  SELECT mode, run
  FROM `films-512811.bronze.tmdb_manifests`
  ORDER BY run DESC          -- run = YYYY-MM-DDTHH-MM-SSZ, textové řazení = časové
  LIMIT 1
),

raw AS (
  SELECT d.*
  FROM `films-512811.bronze.tmdb_details` d
  JOIN latest_run USING (mode, run)
  WHERE d._FILE_NAME LIKE '%/details/%'   -- bez řádků z lists/
    AND d.id IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY d.id ORDER BY d._fetched_at DESC) = 1
),

cleaned AS (
  SELECT
    id,
    imdb_id,
    title,
    original_title,
    original_language,
    NULLIF(TRIM(overview), '') AS overview,
    NULLIF(TRIM(tagline), '') AS tagline,
    status,
    SAFE.PARSE_DATE('%Y-%m-%d', NULLIF(release_date, '')) AS release_date,
    NULLIF(runtime, 0) AS runtime,                       -- 0 = neznámá délka
    NULLIF(budget, 0) AS budget,                         -- 0 = neznámá hodnota
    NULLIF(revenue, 0) AS revenue,
    popularity,
    IF(vote_count = 0, NULL, vote_average) AS vote_average,
    vote_count,
    NULLIF(homepage, '') AS homepage,
    IF(NULLIF(poster_path, '') IS NULL, NULL,
       CONCAT('https://image.tmdb.org/t/p/w500', poster_path)) AS poster_url,

    origin_country,
    ARRAY(SELECT g.name FROM UNNEST(genres) g) AS genres,
    ARRAY(SELECT k.name FROM UNNEST(keywords.keywords) k) AS keywords,
    ARRAY(SELECT c.name FROM UNNEST(production_companies) c) AS production_companies,
    ARRAY(SELECT c.iso_3166_1 FROM UNNEST(production_countries) c) AS production_countries,
    ARRAY(SELECT l.english_name FROM UNNEST(spoken_languages) l) AS spoken_languages,
    belongs_to_collection.name AS collection,

    -- Štáb podle rolí (všichni lidé v roli, bez duplicit)
    ARRAY(SELECT DISTINCT p.name FROM UNNEST(credits.crew) p
          WHERE p.job = 'Director') AS directors,
    ARRAY(SELECT DISTINCT p.name FROM UNNEST(credits.crew) p
          WHERE p.job IN ('Writer', 'Screenplay', 'Novel', 'Story')) AS writers,
    ARRAY(SELECT DISTINCT p.name FROM UNNEST(credits.crew) p
          WHERE p.job IN ('Producer', 'Executive Producer')) AS producers,
    ARRAY(SELECT DISTINCT p.name FROM UNNEST(credits.crew) p
          WHERE p.job = 'Director of Photography') AS cinematographers,
    ARRAY(SELECT DISTINCT p.name FROM UNNEST(credits.crew) p
          WHERE p.job = 'Original Music Composer') AS composers,
    ARRAY(SELECT DISTINCT p.name FROM UNNEST(credits.crew) p
          WHERE p.job = 'Editor') AS editors,

    -- Herci v pořadí titulků (scraper už ořízl na prvních 20)
    ARRAY(SELECT AS STRUCT a.name, a.`character`, a.`order`
          FROM UNNEST(credits.`cast`) a ORDER BY a.`order`) AS top_cast,

    -- Premiéra v kinech (release type 2 = limited, 3 = theatrical)
    (SELECT MIN(SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(rd.release_date, 1, 10)))
     FROM UNNEST(release_dates.results) r, UNNEST(r.release_dates) rd
     WHERE r.iso_3166_1 = 'CZ' AND rd.type IN (2, 3)) AS release_date_cz,
    (SELECT MIN(SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(rd.release_date, 1, 10)))
     FROM UNNEST(release_dates.results) r, UNNEST(r.release_dates) rd
     WHERE r.iso_3166_1 = 'US' AND rd.type IN (2, 3)) AS release_date_us,

    -- Zdroje
    _sources AS sources,
    'top_rated' IN UNNEST(_sources) AS is_top_rated,
    'now_playing:US' IN UNNEST(_sources) AS in_cinema_us,
    'now_playing:CZ' IN UNNEST(_sources) AS in_cinema_cz,
    'upcoming:US' IN UNNEST(_sources) AS upcoming_us,
    'upcoming:CZ' IN UNNEST(_sources) AS upcoming_cz,
    EXISTS(SELECT 1 FROM UNNEST(_sources) s WHERE STARTS_WITH(s, 'new_release:')) AS is_new_release,

    -- Odkud řádek je
    mode AS run_mode,
    run,
    SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H-%M-%SZ', run) AS run_at,
    SAFE_CAST(_fetched_at AS TIMESTAMP) AS fetched_at
  FROM raw
)

SELECT *
FROM cleaned
WHERE is_top_rated                                      -- top 10k bez filtru
   OR (overview IS NOT NULL                             -- novinky a kina: filtr kvality
       AND poster_url IS NOT NULL
       AND (runtime IS NULL OR runtime >= 40));
