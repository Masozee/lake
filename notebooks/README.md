# notebooks

Exploration only. Throwaway by design.

**Nothing in `src/` may import from here.** A notebook is a scratchpad for looking
at data, not a place for logic. The moment a cell does something you want to run
twice, move it into `src/lake/` and give it a test.

Read `processed/` with DuckDB — it discovers the hive partitions itself:

```python
import duckdb

duckdb.sql("""
    SELECT country_iso3, year, gdp_usd
    FROM read_parquet('/mnt/nas/lake/processed/dataset=gdp_annual/**/*.parquet',
                      hive_partitioning = true)
    WHERE year >= 2020
    ORDER BY gdp_usd DESC NULLS LAST
    LIMIT 20
""").df()
```

Reading `raw/` is fine too, but skip any run directory without a `_MANIFEST.json`
that says `"status": "complete"` — those are the residue of a crashed run, and
they look exactly like real data.

Notebooks are git-ignored below this file. Do not commit outputs.
