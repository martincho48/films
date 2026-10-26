-- BRONZE: external tables nad raw bucketem. BigQuery čte JSONL přímo z GCS, data se nekopírují.
-- Sloupce mode a run se berou z cesty tmdb/mode=…/run=…/ (hive partitioning).
-- Spuštění (Windows: přes cmd kvůli BOM):
--   cmd /c "bq query --use_legacy_sql=false --location=us-central1 < sql/01_bronze_tmdb_external_tables.sql"

-- Detaily filmů. Schéma obsahuje jen pole, která používáme; ostatní se ignorují
-- (ignore_unknown_values). Maska *.jsonl zachytí i lists/, jejich řádky mají id = NULL
-- a filtrují se přes _FILE_NAME LIKE '%/details/%'.
CREATE OR REPLACE EXTERNAL TABLE `films-512811.bronze.tmdb_details` (
  id INT64,
  imdb_id STRING,
  title STRING,
  original_title STRING,
  original_language STRING,
  overview STRING,
  tagline STRING,
  status STRING,
  release_date STRING,
  runtime INT64,
  budget INT64,
  revenue INT64,
  popularity FLOAT64,
  vote_average FLOAT64,
  vote_count INT64,
  adult BOOL,
  homepage STRING,
  poster_path STRING,
  backdrop_path STRING,
  origin_country ARRAY<STRING>,
  genres ARRAY<STRUCT<id INT64, name STRING>>,
  production_companies ARRAY<STRUCT<id INT64, name STRING, origin_country STRING>>,
  production_countries ARRAY<STRUCT<iso_3166_1 STRING, name STRING>>,
  spoken_languages ARRAY<STRUCT<iso_639_1 STRING, english_name STRING, name STRING>>,
  belongs_to_collection STRUCT<id INT64, name STRING>,
  keywords STRUCT<keywords ARRAY<STRUCT<id INT64, name STRING>>>,
  credits STRUCT<
    `cast` ARRAY<STRUCT<id INT64, name STRING, `character` STRING, `order` INT64, gender INT64,
                        known_for_department STRING, profile_path STRING>>,
    crew ARRAY<STRUCT<id INT64, name STRING, job STRING, department STRING>>
  >,
  release_dates STRUCT<results ARRAY<STRUCT<
    iso_3166_1 STRING,
    release_dates ARRAY<STRUCT<release_date STRING, type INT64, certification STRING, note STRING>>
  >>>,
  _sources ARRAY<STRING>,
  _fetched_at STRING
)
WITH PARTITION COLUMNS (mode STRING, run STRING)
OPTIONS (
  format = 'NEWLINE_DELIMITED_JSON',
  uris = ['gs://films-512811-raw/tmdb/*.jsonl'],
  hive_partition_uri_prefix = 'gs://films-512811-raw/tmdb',
  ignore_unknown_values = TRUE,
  description = 'Raw TMDB movie details from the scraper (JSONL in GCS). Rows from lists/ have id = NULL.'
);

-- Manifesty běhů: běh je úplný, jen když má manifest.json (zapisuje se jako poslední).
CREATE OR REPLACE EXTERNAL TABLE `films-512811.bronze.tmdb_manifests` (
  started_at STRING,
  finished_at STRING,
  duration_s INT64,
  unique_movie_ids INT64,
  list_pages INT64,
  details_ok INT64
)
WITH PARTITION COLUMNS (mode STRING, run STRING)
OPTIONS (
  format = 'NEWLINE_DELIMITED_JSON',
  uris = ['gs://films-512811-raw/tmdb/*manifest.json'],
  hive_partition_uri_prefix = 'gs://films-512811-raw/tmdb',
  ignore_unknown_values = TRUE,
  description = 'One row per complete scraper run (manifest.json).'
);
