.DEFAULT_GOAL := help
.PHONY: help install migrate test lint fmt dash doctor deploy enable timers logs \
        serve-build serve

help:  ## show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk -F':.*?## ' '{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## create the venv and install everything
	uv sync --frozen --extra dev --extra transform --extra dashboard --extra api

migrate:  ## apply database migrations
	uv run alembic upgrade head

test:  ## run the full suite
	uv run pytest -q

lint:  ## ruff + mypy
	uv run ruff check src tests dashboard migrations
	uv run ruff format --check src tests dashboard migrations
	uv run mypy src

fmt:  ## autoformat and autofix
	uv run ruff format src tests dashboard migrations
	uv run ruff check --fix src tests dashboard migrations

dash:  ## run the dashboard locally (localhost only)
	uv run streamlit run dashboard/app.py --server.address 127.0.0.1

# --- serving API + htmx UI ---------------------------------------------------

serve-build:  ## materialise the read-only serving replica from processed/
	uv run lake serve build

serve:  ## run the API + htmx UI (one process, localhost only) on :8000
	uv run lake serve run

doctor:  ## preflight: NAS, database, registry, alerting
	uv run lake doctor

# --- deployment (run on the NUC) ---------------------------------------------

deploy:  ## install systemd units
	sudo install -m 0644 deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
	sudo install -m 0644 deploy/nas-mount/mnt-nas.mount /etc/systemd/system/
	sudo systemctl daemon-reload

enable:  ## enable the mount and every timer
	sudo systemctl enable --now mnt-nas.mount
	sudo systemctl enable --now lake-daily.timer lake-weekly.timer \
		lake-monthly.timer lake-yearly.timer \
		lake-retry.timer lake-freshness.timer lake-sweep.timer lake-backup.timer

timers:  ## when does everything next fire
	systemctl list-timers 'lake-*' --all

logs:  ## follow every lake unit
	journalctl -u 'lake-*' -f -o cat
