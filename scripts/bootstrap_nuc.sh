#!/usr/bin/env bash
#
# Bare Debian 12 / Ubuntu 24.04 -> a running lake. Idempotent; safe to re-run.
#
#   sudo ./scripts/bootstrap_nuc.sh
#
# What it does NOT do, on purpose:
#   * mount the NAS         — edit deploy/nas-mount/mnt-nas.mount first, it has your IP in it
#   * write /etc/lake/lake.env with real secrets — it writes a template; you fill it in
#   * enable the timers     — run `make enable` once you have run `lake doctor` clean
#
set -euo pipefail

LAKE_USER=lake
LAKE_HOME=/opt/lake
LAKE_DB=lake_meta
REPO="${1:-https://github.com/your-org/lake.git}"

log() { printf '\033[36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run me with sudo"

# --- packages ----------------------------------------------------------------

log "installing packages"
apt-get update -qq
apt-get install -y --no-install-recommends \
    postgresql postgresql-client \
    python3 python3-venv python3-dev \
    git curl ca-certificates \
    nfs-common cifs-utils \
    restic zstd jq \
    unattended-upgrades fail2ban ufw

# --- service user ------------------------------------------------------------

if ! id -u "$LAKE_USER" >/dev/null 2>&1; then
    log "creating system user '$LAKE_USER' (nologin — never run scrapers as a human user)"
    useradd --system --shell /usr/sbin/nologin --home-dir "$LAKE_HOME" "$LAKE_USER"
fi

log "creating directories"
install -d -o "$LAKE_USER" -g "$LAKE_USER" -m 0755 "$LAKE_HOME" /var/log/lake /var/lib/lake
install -d -o "$LAKE_USER" -g "$LAKE_USER" -m 0700 /var/lib/lake/staging
install -d -o root -g "$LAKE_USER" -m 0750 /etc/lake

# --- code --------------------------------------------------------------------

if [[ ! -d "$LAKE_HOME/.git" ]]; then
    log "cloning $REPO"
    sudo -u "$LAKE_USER" git clone "$REPO" "$LAKE_HOME"
else
    log "repo already present, pulling"
    sudo -u "$LAKE_USER" git -C "$LAKE_HOME" pull --ff-only
fi

if [[ ! -x /usr/local/bin/uv ]]; then
    log "installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh
fi

log "installing python dependencies"
cd "$LAKE_HOME"
sudo -u "$LAKE_USER" /usr/local/bin/uv sync --frozen --extra transform --extra dashboard

# --- database ----------------------------------------------------------------
# Peer auth over a unix socket: there is no password, so there is nothing to leak.

log "creating postgres role and database"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$LAKE_USER'" | grep -q 1 \
    || sudo -u postgres createuser "$LAKE_USER"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$LAKE_DB'" | grep -q 1 \
    || sudo -u postgres createdb "$LAKE_DB" -O "$LAKE_USER"

# --- environment file --------------------------------------------------------

if [[ ! -f /etc/lake/lake.env ]]; then
    log "writing /etc/lake/lake.env"
    # The ntfy topic name IS the authentication — anyone who knows it can read
    # your alerts. Generate a random one here rather than hoping someone edits
    # a placeholder later.
    NTFY_TOPIC="lake-$(openssl rand -hex 12)"

    umask 077
    cat > /etc/lake/lake.env <<EOF
LAKE_ENV=production
LAKE_NAS_ROOT=/mnt/nas/lake
LAKE_STAGING_ROOT=/var/lib/lake/staging
LAKE_DB_DSN=postgresql+psycopg://lake@/lake_meta?host=/var/run/postgresql
LAKE_LOG_DIR=/var/log/lake
LAKE_LOG_LEVEL=INFO
LAKE_LOG_JSON=true
LAKE_ALERT_ENABLED=true
LAKE_ALERT_NTFY_URL=https://ntfy.sh/${NTFY_TOPIC}
EOF
    chown root:"$LAKE_USER" /etc/lake/lake.env
    chmod 0640 /etc/lake/lake.env

    echo
    echo "  Subscribe to your alerts:  https://ntfy.sh/${NTFY_TOPIC}"
    echo "  Treat that URL as a secret. It is in /etc/lake/lake.env."
    echo
fi

# --- logrotate ---------------------------------------------------------------

log "configuring logrotate"
cat > /etc/logrotate.d/lake <<EOF
/var/log/lake/*.jsonl {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    create 0640 $LAKE_USER $LAKE_USER
}
EOF

# --- hardening ---------------------------------------------------------------
# The 20% that matters. The NUC must never get a public IP; use Tailscale.

log "hardening ssh and firewall"
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/;
        s/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
systemctl restart ssh || systemctl restart sshd

LAN=$(ip -4 route show default | awk '{print $3}' | head -1 | sed 's/\.[0-9]*$/.0\/24/')
ufw --force default deny incoming
ufw --force default allow outgoing
ufw allow from "${LAN:-192.168.1.0/24}" to any port 22 proto tcp
ufw allow from "${LAN:-192.168.1.0/24}" to any port 8501 proto tcp comment 'dashboard, LAN only'
ufw --force enable

systemctl enable --now unattended-upgrades fail2ban

# --- done --------------------------------------------------------------------

cat <<EOF

$(log "bootstrap complete")

Next, in order:

  1. Edit /etc/lake/lake.env             (ntfy topic, any API keys)
  2. Edit deploy/nas-mount/mnt-nas.mount (your NAS address and export path)
  3. sudo make deploy                    (install systemd units)
  4. sudo systemctl enable --now mnt-nas.mount
  5. sudo touch /mnt/nas/lake/.lake_mounted   # the sentinel. ON the NAS, not under it.
  6. sudo -u lake .venv/bin/alembic upgrade head
  7. sudo -u lake .venv/bin/lake sync-sources
  8. sudo -u lake .venv/bin/lake doctor       # must be clean before you enable timers
  9. sudo make enable

Then verify:  systemctl list-timers 'lake-*'
EOF
