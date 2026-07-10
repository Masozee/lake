# worldbank_gdp

**Schedule:** daily, 06:00 UTC ±15 min · **Owner:** data-team · **SLA:** 30h

GDP in current US dollars for every country, from the World Bank indicator API.

## Upstream

```
https://api.worldbank.org/v2/country/all/indicator/NY.GDP.MKTP.CD?format=json&per_page=20000
```

Response is a two-element array: `[metadata, [records...]]`. Pagination lives in
`metadata.pages`; we walk it and store one file per page, exactly as received.

No API key. No documented rate limit; we send a descriptive `User-Agent` and
jitter the schedule by up to 15 minutes anyway.

## Quirks

* **Aggregate rows have an empty `countryiso3code`.** `World`, `Euro area`, and
  income-band groupings all arrive with `""`. The parser drops them. If you ever
  need them, they are still in `raw/` — that is the point of keeping raw immutable.
* **`value` is legitimately `null`** for country-years with no reported figure.
  Do not coerce it to zero. A missing GDP and a GDP of zero are different facts.
* **The data barely changes.** A daily schedule against an annual indicator means
  most runs will record `was_new = false`. That is correct and expected: it proves
  we checked. It is not a reason to reduce the schedule.
* `date` is a year, as a string.

## Files

```
raw/source=worldbank_gdp/year=2026/month=07/day=09/run=.../
    worldbank_gdp_20260709_001.json
    worldbank_gdp_20260709_001.json.meta.json
    _MANIFEST.json
```

## Processed

`processed/dataset=gdp_annual/year=*/`, partitioned by `year`.

| column | type | note |
|---|---|---|
| `country_iso3` | `VARCHAR(3)` | uppercased; aggregates excluded |
| `country_name` | `VARCHAR` | |
| `year` | `INTEGER` | partition key |
| `gdp_usd` | `DOUBLE` | nullable — see quirks |
| `indicator` | `VARCHAR` | always `NY.GDP.MKTP.CD` |

```bash
lake transform gdp_annual
```

## Refreshing the test fixture

```bash
curl -s 'https://api.worldbank.org/v2/country/all/indicator/NY.GDP.MKTP.CD?format=json&per_page=4' \
  > tests/fixtures/worldbank_gdp_page1.json
```

Capture the real bytes. A hand-edited approximation will pass the tests on the
day upstream changes the shape, which is precisely the day you need it to fail.
