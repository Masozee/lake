# Runbook

What to do when something is wrong. Written before it was needed.

**Last restore test:** _never — do one, then write the date here._

---

## Start here, always

```bash
sudo -u lake /opt/lake/.venv/bin/lake doctor
```

It checks, in order: the NAS mount, the database, the source registry, alerting.
Most incidents are one of those four, and `doctor` names which.

Then:

```bash
lake status                       # last 20 runs across all sources
systemctl list-timers 'lake-*'    # is anything even scheduled?
```

---

## "I got an alert: source X failed"

```bash
journalctl -u lake-scrape@X.service -n 100 --no-pager
psql lake_meta -c "
  SELECT r.logical_date, r.attempt, e.error_class, e.error_message
  FROM runs r JOIN run_errors e USING (run_id)
  WHERE r.source_id = 'X' ORDER BY e.occurred_at DESC LIMIT 5"
```

Read `error_class` first — it tells you which of these you have.

| `error_class` | What it means | What to do |
|---|---|---|
| `ConnectError`, `ReadTimeout` | Network or upstream is down | Nothing. `lake-retry.timer` picks it up within 30 min. |
| `HTTPStatusError` 5xx | Upstream is broken | Nothing. Same as above. |
| `HTTPStatusError` 404 | The URL moved | Fix `configs/sources.yaml`, `lake sync-sources`, re-run. Never retried automatically, by design. |
| `HTTPStatusError` 403/429 | You are being blocked or throttled | Check the `User-Agent`. Increase `RandomizedDelaySec`. Talk to the source owner. |
| `ValidationFailed` | Upstream returned something that is not the data | See "the file is not what it claims" below. |
| `NasNotMountedError` | The NAS is gone | See "the NAS is unmounted" below. |
| `ChecksumMismatch` | Bytes changed between memory and disk | Disk or NAS is failing. Check `dmesg`, SMART, the NAS logs. Rare and serious. |

Re-run one date by hand once you have fixed the cause:

```bash
sudo -u lake /opt/lake/.venv/bin/lake scrape X --logical-date 2026-07-09 --force
```

---

## "I got an alert: N sources are stale"

Stale means *no successful run within the freshness SLA*. This is the alert that
`OnFailure=` cannot produce, because nothing failed — nothing ran.

```bash
psql lake_meta -c "SELECT * FROM v_freshness WHERE is_stale"
systemctl list-timers 'lake-*' --all
```

Ask, in order:

1. **Is the timer enabled?** `systemctl is-enabled lake-daily.timer`.
   Someone may have disabled it during an incident and never re-enabled it.
2. **Was the NUC off?** `last reboot`. With `Persistent=true` the run should have
   fired at boot — if it did not, the timer is not enabled.
3. **Did the source get disabled?** `grep -A2 'source_id: X' configs/sources.yaml`.
   `enabled: false` plus a stale alert means someone turned it off and forgot.
4. **Is it actually failing?** `lake status -s X`. If runs exist and fail, this is
   the previous section, not this one.

---

## "The source is succeeding but nothing is new"

The dashboard's *Quiet sources* panel, or:

```sql
SELECT r.source_id, max(o.observed_at) AS last_seen,
       count(*) FILTER (WHERE o.was_new) AS new_files
FROM file_observations o JOIN runs r USING (run_id)
WHERE o.observed_at > now() - interval '30 days'
GROUP BY 1 HAVING count(*) FILTER (WHERE o.was_new) = 0;
```

This is **not a bug in the scraper.** We fetched, the bytes were byte-identical to
what we already hold, and we correctly declined to write them again. Upstream has
stopped publishing.

Distinguishing this from a broken scraper is the entire reason
`file_observations.was_new` exists. Do not "fix" it by disabling the checksum
check. Go and ask the data owner why they stopped publishing.

---

## "The file is not what it claims" (`ValidationFailed`)

Almost always: upstream returned HTTP 200 with an HTML error page, and we were
about to save it as `report.xlsx`. The structural gate caught it.

```bash
ls -la /mnt/nas/lake/quarantine/source=X/year=*/month=*/day=*/
jq . /mnt/nas/lake/quarantine/source=X/.../_FAILURE_*.json
head -c 400 /mnt/nas/lake/quarantine/source=X/.../partial_*/*
```

If the head shows `<!DOCTYPE html>`, the URL is dead or you are being redirected
to a login page. Fix the URL. Nothing reached `raw/`; there is nothing to clean up.

---

## "The NAS is unmounted"

Scrapers refuse to run — by design. The alternative is silently filling the NUC's
root disk for three weeks and discovering it when Postgres cannot write.

```bash
systemctl status mnt-nas.mount
mountpoint /mnt/nas          # exit 0 if mounted
ls /mnt/nas/lake/.lake_mounted
ping -c3 192.168.1.50
sudo systemctl restart mnt-nas.mount
```

If the mount succeeds but `.lake_mounted` is missing, someone deleted the
sentinel. Recreate it **on the NAS**:

```bash
sudo touch /mnt/nas/lake/.lake_mounted
```

If `/mnt/nas` exists but is empty and `mountpoint` says no: the NAS is down and
something may have written into the underlying directory. Check:

```bash
sudo umount /mnt/nas 2>/dev/null; ls -la /mnt/nas   # must be empty
```

Anything in there was written while unmounted. Delete it — it is not in the
catalog and it is not real data.

---

## "The disk is full"

```bash
df -h /mnt/nas /var
du -sh /var/lib/lake/staging/*      # abandoned staging from crashed runs
lake sweep                          # clears staging older than 24h
lake archive --apply                # tar.zst cold partitions, 5-15x shrink
lake retention                      # DRY RUN. read it before you trust it.
lake retention --apply              # then delete
```

`lake retention` soft-deletes in the catalog before unlinking, so you can always
answer *what did we once hold?* after the bytes are gone.

If `/var` is full rather than the NAS, the usual cause is `/var/lib/lake/staging`
holding a half-downloaded yearly dump. `lake sweep` clears it.

---

## "A run is stuck in `running`"

A scraper was killed (OOM, reboot, `systemctl stop`) before it could write a
terminal status.

```sql
SELECT run_id, source_id, logical_date, started_at
FROM runs WHERE status = 'running' AND started_at < now() - interval '6 hours';
```

The run directory on the NAS will have no `_MANIFEST.json`, so every downstream
reader already ignores it. **You do not have to do anything.** After six hours
`lake retry` treats a `running` row as a corpse rather than a worker and retries
the logical date anyway — otherwise a single `kill -9` would freeze that date
forever.

To tidy the row so `lake status` reads honestly:

```sql
UPDATE runs SET status = 'failed', finished_at = now()
WHERE status = 'running' AND started_at < now() - interval '6 hours';
```

`lake sweep` reports the orphaned directories; it does not delete them, because
that is your evidence.

---

## "I need to backfill"

```bash
lake backfill bps_inflation --start 2024-01-01 --end 2026-06-01
```

Dates that already succeeded are skipped. The step size is inferred from the
source's schedule (`monthly` steps by month, not by 30 days).

To force a rebuild of dates that already succeeded, add `--force`. Be aware this
writes new run rows and, if the bytes differ, new files — it does not mutate `raw/`.

---

## "I need to prove where this number came from"

Full lineage, one join chain:

```sql
SELECT d.dataset_id, d.nas_path, d.row_count,
       r.run_id, r.logical_date, r.git_sha, r.started_at,
       f.nas_path AS source_file, f.sha256
FROM datasets d
JOIN runs r ON r.run_id = d.built_from_run
JOIN file_observations o ON o.run_id = r.run_id
JOIN files f ON f.file_id = o.file_id
WHERE d.dataset_id = 'gdp_annual';
```

`r.git_sha` is the commit that produced it. `git checkout <sha>` and replay
against the exact bytes at `f.nas_path` — `raw/` is immutable, so they are still
the bytes you fetched.

---

## Restoring the metadata catalog

Raw files are often re-downloadable. The catalog is not. Practise this.

```bash
./scripts/restore_metadata.sh                    # restore-test to a scratch DB
./scripts/restore_metadata.sh --target lake_meta # the real thing; it will ask
```

Do the restore-test **quarterly** and update the date at the top of this file.
An untested backup is a hope, not a backup.

---

## Deploying a change

```bash
cd /opt/lake
sudo -u lake git pull --ff-only
sudo -u lake .venv/bin/uv sync --frozen
sudo -u lake .venv/bin/alembic upgrade head
sudo -u lake .venv/bin/lake sync-sources     # if configs/sources.yaml changed
sudo systemctl daemon-reload                  # if deploy/systemd/ changed
sudo -u lake .venv/bin/lake doctor
```

Scrapers are `Type=oneshot`; there is no long-running process to restart. The next
timer firing picks up the new code. `runs.git_sha` records which commit ran.

---

## Adding a source

1. `mkdir src/lake/sources/new_source/` with `scraper.py`, `parser.py`, `schema.py`.
2. Add an entry to `configs/sources.yaml` pointing `module:` at the class.
3. Capture a real response into `tests/fixtures/`. Write a parser test against it.
4. `lake sync-sources && lake scrape new_source`

No new systemd unit. No scheduler edit. The dispatcher reads the YAML.

---

## Things that are working as designed

* **A 404 is never retried.** It means the URL moved or our code is wrong.
  Retrying just hammers someone else's server.
* **Scrapers have no `Restart=`.** Process-level retry of a scrape is an infinite
  loop. Retry lives at the job level, in `lake-retry.timer`.
* **`raw/` files are `0o440`.** You cannot edit them. Everything downstream is
  rebuildable; `raw/` is the one thing that must never change.
* **A run that wrote zero files still gets a manifest.** `file_count: 0` is the
  durable record that we checked and found nothing new.
* **The dashboard has no login.** It is bound to `127.0.0.1`. Reach it over
  Tailscale. Never expose port 8501.
