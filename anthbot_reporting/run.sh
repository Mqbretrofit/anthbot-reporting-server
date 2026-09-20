#!/usr/bin/with-contenv bashio
set -euo pipefail

export ANTHBOT_DB_PATH="/data/anthbot_reporting.sqlite3"
export ANTHBOT_ADMIN_TOKEN="$(bashio::config 'admin_token')"
export ANTHBOT_STORE_ENABLED="$(bashio::config 'store_enabled')"
export ANTHBOT_STRIPE_SECRET_KEY="$(bashio::config 'stripe_secret_key')"
export ANTHBOT_STRIPE_WEBHOOK_SECRET="$(bashio::config 'stripe_webhook_secret')"
export ANTHBOT_STORE_LICENSE_SECRET="$(bashio::config 'store_license_secret')"
export ANTHBOT_STRIPE_AUTOMATIC_TAX="$(bashio::config 'stripe_automatic_tax')"
export ANTHBOT_PRIVACY_CONTROLLER_NAME="$(bashio::config 'privacy_controller_name')"
export ANTHBOT_PRIVACY_CONTROLLER_ADDRESS="$(bashio::config 'privacy_controller_address')"
export ANTHBOT_PRIVACY_CONTACT_EMAIL="$(bashio::config 'privacy_contact_email')"
export ANTHBOT_PRIVACY_CONTACT_PHONE="$(bashio::config 'privacy_contact_phone')"
export ANTHBOT_SITE_ANALYTICS_ENABLED="$(bashio::config 'site_analytics_enabled')"
export ANTHBOT_GOOGLE_SITE_VERIFICATION="$(bashio::config 'google_site_verification')"
export ANTHBOT_WEB_VOICE_INSTALLER_ENABLED="$(bashio::config 'web_voice_installer_enabled')"
export ANTHBOT_SMTP_HOST="$(bashio::config 'smtp_host')"
export ANTHBOT_SMTP_PORT="$(bashio::config 'smtp_port')"
export ANTHBOT_SMTP_USERNAME="$(bashio::config 'smtp_username')"
export ANTHBOT_SMTP_PASSWORD="$(bashio::config 'smtp_password')"
export ANTHBOT_SMTP_FROM_EMAIL="$(bashio::config 'smtp_from_email')"
export ANTHBOT_SMTP_FROM_NAME="$(bashio::config 'smtp_from_name')"
export ANTHBOT_SMTP_STARTTLS="$(bashio::config 'smtp_starttls')"
export ANTHBOT_SMTP_SSL="$(bashio::config 'smtp_ssl')"

bashio::log.info "Starting ANTHBOT Reporting Server on port 8080"
exec uvicorn entrypoint:app \
  --host 0.0.0.0 \
  --port 8080 \
  --proxy-headers \
  --no-access-log
