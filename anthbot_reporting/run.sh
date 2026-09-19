#!/usr/bin/with-contenv bashio
set -euo pipefail

export ANTHBOT_DB_PATH="/data/anthbot_reporting.sqlite3"
export ANTHBOT_ADMIN_TOKEN="$(bashio::config 'admin_token')"
export ANTHBOT_STORE_ENABLED="$(bashio::config 'store_enabled')"
export ANTHBOT_STRIPE_SECRET_KEY="$(bashio::config 'stripe_secret_key')"
export ANTHBOT_STRIPE_WEBHOOK_SECRET="$(bashio::config 'stripe_webhook_secret')"
export ANTHBOT_STORE_LICENSE_SECRET="$(bashio::config 'store_license_secret')"
export ANTHBOT_STRIPE_AUTOMATIC_TAX="$(bashio::config 'stripe_automatic_tax')"

bashio::log.info "Starting ANTHBOT Reporting Server on port 8080"
exec uvicorn entrypoint:app \
  --host 0.0.0.0 \
  --port 8080 \
  --proxy-headers \
  --no-access-log
