FROM ghcr.io/home-assistant/base-python:3.13-alpine3.24-2026.06.1

ARG BUILD_VERSION=dev
ARG BUILD_ARCH=local

LABEL \
    io.hass.name="ANTHBOT Reporting Server" \
    io.hass.description="ANTHBOT Map reporting backend" \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ANTHBOT_DB_PATH=/data/anthbot_reporting.sqlite3

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip3 install --no-cache-dir -r requirements.txt

COPY app.py ./app.py
COPY asgi.py ./asgi.py
COPY presence_api.py ./presence_api.py
COPY presence_dashboard.html ./presence_dashboard.html
COPY entrypoint.py ./entrypoint.py
COPY diagnostics_dedupe.py ./diagnostics_dedupe.py
COPY migration_api.py ./migration_api.py
COPY voice_packs.json ./voice_packs.json
COPY store_api.py ./store_api.py
COPY assets ./assets
COPY store_accounts.py ./store_accounts.py
COPY store.html ./store.html
COPY public_site.html ./public_site.html
COPY public_site_i18n.js ./public_site_i18n.js
COPY public_terms.html ./public_terms.html
COPY public_refunds.html ./public_refunds.html
COPY public_privacy.html ./public_privacy.html
COPY store_success.html ./store_success.html
COPY store_admin.html ./store_admin.html
COPY voice_installer.html ./voice_installer.html
COPY web_voice_installer.py ./web_voice_installer.py
COPY web_voice_installer_anthbot.py ./web_voice_installer_anthbot.py
COPY dashboard.html ./dashboard.html
COPY dashboard_login.html ./dashboard_login.html
COPY developer_agent_api.py ./developer_agent_api.py
COPY developer_agent_dashboard.py ./developer_agent_dashboard.py
COPY developer_agent_dashboard.html ./developer_agent_dashboard.html
COPY diagnostics_dashboard.py ./diagnostics_dashboard.py
COPY diagnostic_detail.html ./diagnostic_detail.html
COPY run.sh /run.sh
RUN chmod a+x /run.sh

EXPOSE 8080

CMD ["/run.sh"]
