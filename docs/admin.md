# The admin panel

`/admin` in the web app. It monitors the pipeline, edits the source registry, and
manages the people who can do those things.

It is the only part of the system that writes, and the only part with any concept
of a user. Everything else — the scrapers, the API, the public site — has neither.

## Getting in

There is no sign-up page, and there will not be one. A panel that can create its
own first admin is a panel a stranger can claim. The first account is made from a
shell on the server:

```bash
lake admin create-user you@example.org      # prompts for the password
lake admin list-users
lake admin reset-password you@example.org   # the way back in when locked out
```

Once you are in, you can create and disable other admins from the Users tab.

The password is prompted for, never taken as an argument — an argument lands in
the shell history, in `ps` output, and in any log that records a command line.

## What protects it

| | |
|---|---|
| **Password storage** | Argon2id (memory-hard, the PHC winner). The plaintext is never stored, never logged, and never leaves the request that carried it. Minimum 12 characters — a length floor is the single most effective password rule. |
| **Sessions** | A 256-bit random token in an `httpOnly`, `SameSite=Lax` cookie. The database stores only a SHA-256 of it, so a leaked dump — a stray backup, a `SELECT *` in a log — is not a way in. |
| **Revocation** | A session is a row, not a signed cookie. Disabling a user deletes their rows and they are signed out everywhere, immediately. A signed cookie cannot be taken back before it expires. |
| **Expiry** | Seven days, enforced on read. A sweeper that stops running must never become a session that never ends. |
| **Login attempts** | Rate-limited to `LAKE_API_RATE_LOGIN_PER_MIN` (default 10) per IP. Generous for a human, useless for a dictionary. |
| **Enumeration** | A wrong password and an unknown account return the same message, in the same time. A form that tells them apart is a way to find out who has an account. |
| **Lockout** | You cannot disable your own account. The last admin standing is always someone who can still log in. |

None of this replaces the network boundary. The API still binds to localhost and
is still meant to sit behind Tailscale or an authenticating proxy. The login is a
second lock on the same door, not a reason to open the first one.

## Editing the source registry

The Sources tab edits `configs/sources.yaml` — a git-tracked file that normally
changes only through review. Editing it from a browser bypasses that, so three
things stand in for the missing pull request:

1. **It is validated before it is written.** The module path has to actually
   import, the schedule has to be one the systemd timers know, the SLA has to be a
   positive number, ids have to be unique. A file that would break a scraper is
   refused, and the reason comes back to the form. This is checked as you type.

2. **The previous version is backed up** to `configs/backups/sources.<ts>.yaml`
   before the new one lands. That is the undo, and it is listed in the tab.

3. **The change is audited.** The Audit tab records who saved, when, and the
   entire previous content of the file. That is what stands in for the commit a
   browser edit does not produce.

The write itself is atomic — a temp file in the same directory, fsync, then
`os.replace` — so a scraper booting mid-save sees the whole old file or the whole
new one, never half of either.

Two things it will not do:

- **It will not accept a literal secret.** `api_key: sk-live-...` is refused. Put
  the value in `/etc/lake/lake.env` and reference it as `${env:VAR_NAME}` — the
  indirection exists precisely so a key never enters a git-tracked file.
- **It will not sync the catalog.** Saving writes the file; `lake sync-sources`
  pushes it into Postgres. The panel says so after every save.

## Browsing the data

The **Data** tab browses the read-only replica at three levels, because there are
three things a dataset can be:

| | |
|---|---|
| a raw DuckDB table | `gdp_annual` |
| a statistical table inside one | `seki_indicators:TABEL1_1` — *Uang Beredar dan Faktor-Faktor yang Mempengaruhinya* |
| a single series inside that | `seki_indicators:TABEL1_1~Aktiva Dalam Negeri Bersih` |

There are 4,030 of the last two, so the tab lists the raw tables and lets you
search for the rest by name. Opening a series reads its 280 rows, not the 970,700
in the table it lives in — the dataset's own filter is applied in SQL, and the
columns it is defined by are shown as *fixed by this dataset* rather than as
filter boxes that could only ever return everything or nothing.

A series name does not identify a series: twenty-three of SEKI's are called
*Lainnya*. Every one is shown with the table it came from, which is the only thing
telling them apart.

The important thing about it is where the work happens. `seki_indicators` is
970,700 rows and roughly 24 MB of JSON; a browser that fetched all of it to sort
it in JavaScript would not be slow, it would be broken. So the page, the sort, and
the filters are compiled into **one SQL query**, and DuckDB returns the twenty-five
rows the screen is actually showing. The table renders and tracks state; the
database does the work. Twenty-five rows cross the wire whether the table holds a
thousand or a billion.

Nothing you type is ever spliced into that SQL:

- **Column and table names** are looked up in the real catalog, and the catalog's
  own copy of the name is what goes into the query. A name that is not there
  raises before any SQL exists.
- **Filter values** are bound parameters — not quoted, not escaped, *bound*. There
  is no string of user input anywhere in the SQL text. Searching for
  `x' OR '1'='1` matches zero rows, because it is a string, not code. Searching for
  `%` matches a literal percent sign.

The engine underneath is read-only with external access disabled, so even a
successful injection could not write or read a file. The above is the layer that
means one never gets that far.

## What it monitors

The Overview answers the three questions that matter when something is wrong:

- **What is stale** — a source past its freshness SLA. This is the check that
  catches a scraper which silently stopped being scheduled: it never fails,
  because it never runs, so `OnFailure=` structurally cannot see it.
- **What failed** — the error, the class, the attempt, the run.
- **What went quiet** — succeeding, but every file it fetches is byte-identical to
  one already held. The *source* stopped publishing. That is not a scraper bug and
  it is a different fix; `file_observations.was_new` is the column that tells them
  apart.

Storage shows what has actually landed on the NAS, per source, and the Parquet
datasets built from it. Settings shows the runtime configuration — with every
secret reported only as *set* or *not set*, because a panel that can display an
API key is a panel that can leak one.

## Housekeeping

```bash
lake admin sweep-sessions   # delete expired session rows
```

Cosmetic only: `resolve()` already refuses an expired session whether or not
anything has deleted the row. This keeps the table small, not the system safe.
