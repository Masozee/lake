# Deployment

Target: Debian 12 or Ubuntu 24.04 LTS on an Intel NUC, with a NAS on the LAN.

## Why systemd and a venv, not Docker

One machine, one team, a Python-only workload that needs NAS access and a local
Postgres socket. A container would add a network namespace, a volume-permission
puzzle, and an image registry to run — to solve isolation problems this system
does not have.

systemd already provides: restart policy, dependency ordering, resource limits,
`ProtectSystem=strict`, journald, and `OnFailure=`. Use Podman for *one scraper*
if it needs Playwright and a pile of system libraries. Do not containerise the
platform for that.

| | Chosen | Why not the others |
|---|---|---|
| Orchestrator | **systemd timers** | cron has no status, no logs, no `Persistent=`. Airflow costs 2 GB and a DBA. Dagster is excellent and too heavy here. Prefect when you outgrow this — see below. |
| Isolation | **`uv` venv + systemd sandboxing** | Docker/Podman solve a problem we don't have. |
| Metadata | **Postgres on local SSD** | SQLite over NFS lies about locking. |
| Query | **DuckDB** | out-of-core, handles tens of GB. Spark is for a cluster you don't have. |

**When to move to Prefect:** more than ~10 sources, or real inter-task
dependencies (`scrape → validate → transform → publish` where a failure at step 2
must stop step 3). Prefect 3's `serve()` needs no server infrastructure; keep
systemd running the worker. Until then, timers are less to go wrong.

## Bootstrap

```bash
sudo ./scripts/bootstrap_nuc.sh https://github.com/your-org/lake.git
```

Installs packages, creates the `lake` system user (`nologin`), clones the repo,
builds the venv, creates the Postgres role and database, writes
`/etc/lake/lake.env` with a random ntfy topic, configures logrotate, and hardens
SSH and the firewall.

It deliberately does **not** mount the NAS or enable the timers. Do those by hand,
in this order:

```bash
# 1. point the mount unit at your NAS
sudo vim deploy/nas-mount/mnt-nas.mount     # What=192.168.1.50:/volume1/lake

# 2. install units
sudo make deploy

# 3. mount, and create the sentinel ON the NAS
sudo systemctl enable --now mnt-nas.mount
sudo touch /mnt/nas/lake/.lake_mounted

# 4. schema and registry
cd /opt/lake
sudo -u lake .venv/bin/alembic upgrade head
sudo -u lake .venv/bin/lake sync-sources

# 5. must be clean before you enable anything
sudo -u lake .venv/bin/lake doctor

# 6. go
sudo make enable
systemctl list-timers 'lake-*'
```

## Mounting the NAS safely

Use a systemd `.mount` unit, not `/etc/fstab`. The filename must match the mount
point: `/mnt/nas` → `mnt-nas.mount`.

The payoff is `Requires=mnt-nas.mount` in the scraper units. A scraper will **not
start** when the NAS is down, instead of happily writing into an empty `/mnt/nas`
on the root disk. That silent failure — the NUC's SSD filling for three weeks
because nobody noticed the NAS was unmounted — is the most common way this
architecture dies.

Two guards, because one is not enough:

1. `Requires=mnt-nas.mount` at the systemd level.
2. A `.lake_mounted` sentinel file, which exists only on the NAS, checked in code
   before every write. In production a `st_dev` comparison against `/` is armed too.

Mount options that matter:

```
rw,vers=4.2,hard,noatime,rsize=1048576,wsize=1048576,timeo=600,retrans=2,_netdev
```

* `hard`, never `soft`. A soft mount returns an I/O error on timeout and lets a
  write half-complete. A hard mount blocks. Blocking is recoverable; corruption
  is not. `TimeoutStartSec=` on the service catches a genuine hang.
* `vers=4.2` — no portmapper, better locking.
* `rsize`/`wsize` of 1 MiB; the 128 KiB default wastes a gigabit link.

For SMB, put credentials in `/etc/lake/smb-credentials` (`root:root`, `0600`) and
reference them with `credentials=`. Never in the unit file or `/etc/fstab` — both
are world-readable.

## Secrets

`/etc/lake/lake.env`, owned `root:lake`, mode `0640`. Loaded by systemd's
`EnvironmentFile=`, so it never appears in the process table, `ps`, or shell
history.

The best secret is the one that does not exist. Postgres uses a **unix socket
with peer auth**:

```
LAKE_DB_DSN=postgresql+psycopg://lake@/lake_meta?host=/var/run/postgresql
```

No password. Nothing to leak.

For per-source API keys, reference them from `configs/sources.yaml` as
`${env:MY_API_KEY}` — the registry expands them at use time, never at load time,
so a cached config that gets logged holds only the placeholder. `structlog`
redacts any key whose name looks like a secret.

Graduate to `systemd-creds` (TPM-sealed) or `sops` + `age` (encrypted, committable)
past about five secrets. Vault is another service to keep alive on a NUC; it is
not worth it here.

## Restart policy

**Scrapers are `Type=oneshot` with no `Restart=`.** Retrying a scrape at the
process level is an infinite loop against someone else's server. Retry belongs at
the job level: `lake-retry.timer` runs every 30 minutes, finds failed runs under
the attempt cap that have not since succeeded, and starts a new run with
`attempt=N+1`. Bounded, durable, and visible in the catalog.

Long-running services — the dashboard, and a Prefect worker if you add one — do
get `Restart=on-failure` with `StartLimitBurst=5`.

Postgres and the NAS mount get `Restart=always`.

## `Persistent=true`

On every timer. It is the single most important line in `deploy/systemd/`.

A NUC's real failure mode is being switched off — a power cut, an unplugged
cable, a reboot during an upgrade. Without `Persistent=true`, a 06:00 run that
was missed is simply lost, forever, silently. With it, the run fires at the next
boot.

## Hardening

The 20% that matters:

* **The NUC never gets a public IP.** No port forwarding. Remote access via
  Tailscale or WireGuard.
* `ufw default deny incoming`; SSH and 8501 open to the LAN only.
* SSH: keys only, no root login.
* `unattended-upgrades` and `fail2ban` enabled.
* Postgres `listen_addresses = 'localhost'`.
* Every unit runs as `lake` — a `nologin` system user — under
  `ProtectSystem=strict` with an explicit `ReadWritePaths=`. Scrapers parse
  untrusted PDF, Excel, and XML with libraries that have a CVE history; the
  sandbox is why a parser RCE stays contained.
* **Immutable snapshots on the NAS that the `lake` user cannot delete.** This is
  worth more than everything else on this list. If the NUC is compromised, the
  snapshots survive.

## Backups — 3-2-1

* **Metadata catalog**: `pg_dump` nightly to the NAS, plus restic offsite. Raw
  files are often re-downloadable; the run history, checksums, and lineage are
  not. If you back up one thing, back up this.
* **`processed/`**: restic offsite. Small, high value.
* **`raw/`**: NAS RAID plus snapshots. Offsite only for sources that publish a
  rolling window and whose data genuinely disappears.

**Restore-test quarterly.** `./scripts/restore_metadata.sh` restores into a
scratch database and prints row counts. Write the date in `docs/runbook.md`. An
untested backup is a hope.
