from __future__ import annotations

import re

from app import app as fastapi_app
from developer_agent_api import (
    init_developer_agent_tables,
    router as developer_agent_router,
)
from developer_agent_dashboard import router as developer_agent_dashboard_router
from diagnostics_dashboard import router as diagnostics_dashboard_router

fastapi_app.include_router(developer_agent_router)
fastapi_app.include_router(developer_agent_dashboard_router)
fastapi_app.include_router(diagnostics_dashboard_router)

_FRAME_ANCESTORS = (
    "'self' "
    "http://192.168.8.91:8123 "
    "http://homeassistant.local:8123 "
    "https://ha.mqbretrofithungary.online"
)

_DASHBOARD_DIAGNOSTICS_SCRIPT = """
<script>
(() => {
  const processed = new WeakSet();

  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'
  }[c]));

  const fmt = (v) => {
    if (!v) return '—';
    try {
      const date = new Date(v);
      if (Number.isNaN(date.getTime())) return String(v);
      return new Intl.DateTimeFormat('hu-HU', {
        dateStyle: 'medium',
        timeStyle: 'short'
      }).format(date);
    } catch (_) {
      return String(v);
    }
  };

  const shortId = (v) => v ? String(v).slice(0, 8) + '…' : '—';

  const ensureStyle = () => {
    if (document.getElementById('anthbot-report-groups-style')) return;
    const style = document.createElement('style');
    style.id = 'anthbot-report-groups-style';
    style.textContent = `
      .report-groups{display:grid;gap:18px}
      .report-robot-group{border:1px solid #263349;border-radius:15px;background:rgba(8,16,28,.26);overflow:hidden}
      .report-robot-summary{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:14px 15px;cursor:pointer;list-style:none}
      .report-robot-summary::-webkit-details-marker{display:none}
      .report-robot-summary::before{content:'▾';color:#9fb0ca;font-size:.84rem;transition:transform .15s ease}
      .report-robot-group:not([open])>.report-robot-summary::before{transform:rotate(-90deg)}
      .report-robot-title{display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-width:0;flex:1}
      .report-robot-name{color:#eef4ff;font-weight:750;font-size:.96rem}
      .report-robot-id{color:#9fb0ca;font-size:.76rem;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;overflow-wrap:anywhere}
      .report-robot-stats{display:flex;align-items:center;justify-content:flex-end;gap:7px;flex-wrap:wrap}
      .report-stat{display:inline-flex;align-items:center;padding:3px 7px;border-radius:999px;border:1px solid #263349;background:#0e1828;color:#c8d7ed;font-size:.72rem;white-space:nowrap}
      .report-stat.error{color:#ffd7dc;border-color:rgba(255,122,138,.42)}
      .report-stat.diag{color:#baf7e7;border-color:rgba(128,224,199,.28)}
      .report-stat.integration{color:#cfe3ff;border-color:rgba(106,169,255,.32)}
      .report-robot-body{display:grid;gap:14px;padding:0 14px 14px}
      .report-group{display:grid;gap:8px}
      .report-group-head{display:flex;align-items:center;justify-content:space-between;gap:10px;padding-top:2px;color:#dce7f8;font-weight:700}
      .report-group-count{color:#9fb0ca;font-size:.78rem;font-weight:500}
      .report-empty{padding:12px;border:1px dashed #263349;border-radius:11px;color:#9fb0ca;font-size:.82rem}
      .diag-item.report-robot{border-color:rgba(128,224,199,.25)}
      .diag-item.report-integration{border-color:rgba(106,169,255,.25)}
      .diag-item.report-error{border-color:rgba(255,122,138,.38);background:linear-gradient(180deg,rgba(65,25,36,.42),#0d1626)}
      .report-identity{display:flex;gap:6px;flex-wrap:wrap;margin-top:5px}
      .report-mini{display:inline-flex;align-items:center;padding:2px 6px;border-radius:999px;border:1px solid #263349;background:#0e1828;color:#c8d7ed;font-size:.72rem}
      .report-type-robot{color:#baf7e7;border-color:rgba(128,224,199,.28)}
      .report-type-integration{color:#cfe3ff;border-color:rgba(106,169,255,.32)}
      .report-type-error{color:#ffd7dc;border-color:rgba(255,122,138,.42)}
      .report-error-line{margin-top:7px;color:#ffd7dc;font-size:.82rem;font-weight:700;line-height:1.35}
      .report-error-code{margin-top:3px;color:#ffc4cc;font-size:.76rem;font-weight:600}
      .report-event-line{margin-top:4px;color:#ffe3a7;font-size:.76rem;line-height:1.35}
      .report-raw-line{margin-top:3px;color:#9fb0ca;font-size:.72rem;line-height:1.3}
      .report-time-line{margin-top:5px;color:#9fb0ca;font-size:.72rem;line-height:1.3}
      @media(max-width:700px){
        .report-robot-summary{align-items:flex-start;flex-wrap:wrap}
        .report-robot-stats{width:100%;justify-content:flex-start;padding-left:20px}
      }
    `;
    document.head.appendChild(style);
  };

  const openReport = (reportId) => {
    location.href = '/dashboard/diagnostics/' + encodeURIComponent(reportId);
  };

  const isAutomaticError = (item) =>
    item.trigger === 'mower_error_code' ||
    item.trigger === 'task_event_error' ||
    !!item.diagnostic_event;

  const itemHtml = (item) => {
    const error = item.diagnostic_event || null;
    const automaticError = isAutomaticError(item);
    const typeClass = automaticError
      ? 'report-error'
      : item.report_type === 'robot'
        ? 'report-robot'
        : item.report_type === 'integration'
          ? 'report-integration'
          : '';
    const badgeClass = automaticError
      ? 'report-type-error'
      : item.report_type === 'robot'
        ? 'report-type-robot'
        : item.report_type === 'integration'
          ? 'report-type-integration'
          : '';
    const identity = [];
    if (item.robot_model) identity.push(`<span class="report-mini">${esc(item.robot_model)}</span>`);
    if (item.robot_id) identity.push(`<span class="report-mini mono">${esc(item.robot_id)}</span>`);
    if (!item.robot_model && !item.robot_id) {
      identity.push(`<span class="report-mini">telepítés: ${esc(shortId(item.installation_id))}</span>`);
    }

    const humanMessage = error?.task_event_message || null;
    const rawDescription = error?.err_description || null;
    const mainError = humanMessage || rawDescription || (error?.task_event_code ? `Task event ${esc(error.task_event_code)}` : null);
    const showRawDescription = !!(rawDescription && humanMessage && String(rawDescription).trim() !== String(humanMessage).trim());

    const eventParts = [];
    if (error && error.event_code !== null && error.event_code !== undefined && error.event_code !== '') eventParts.push(`event ${esc(error.event_code)}`);
    if (error && error.cloud_task_event_code !== null && error.cloud_task_event_code !== undefined && error.cloud_task_event_code !== '') eventParts.push(`cloud ${esc(error.cloud_task_event_code)}`);
    if (error?.mode) eventParts.push(`mód ${esc(error.mode)}`);
    if (error?.robot_sta) eventParts.push(`állapot ${esc(error.robot_sta)}`);

    const timeParts = [];
    if (error?.task_event_time) timeParts.push(`Robot esemény: ${esc(fmt(error.task_event_time))}`);
    if (item.received_at) timeParts.push(`Riport érkezett: ${esc(fmt(item.received_at))}`);

    const badgeLabel = automaticError
      ? 'Robot hibariport'
      : (item.report_type_label || 'Egyéb');

    return `
      <div class="diag-item ${typeClass}" data-report-id="${esc(item.report_id)}" tabindex="0" role="link">
        <div class="diag-main">
          <div class="diag-title">${esc(item.trigger || 'diagnosztika')}</div>
          <div class="diag-meta">${esc(item.report_id)}</div>
          <div class="report-identity">${identity.join('')}</div>
          ${mainError ? `<div class="report-error-line">Hiba: ${mainError}</div>` : ''}
          ${error && error.err_code !== null && error.err_code !== undefined && error.err_code !== '' ? `<div class="report-error-code">Hibakód: ${esc(error.err_code)}</div>` : ''}
          ${eventParts.length ? `<div class="report-event-line">Esemény: ${eventParts.join(' · ')}</div>` : ''}
          ${showRawDescription ? `<div class="report-raw-line">Gyári leírás: ${esc(rawDescription)}</div>` : ''}
          ${timeParts.length ? `<div class="report-time-line">${timeParts.join(' · ')}</div>` : ''}
        </div>
        <span class="pill ${badgeClass}">${esc(badgeLabel)}</span>
      </div>`;
  };

  const sectionHtml = (title, items) => {
    if (!items.length) return '';
    return `
      <div class="report-group">
        <div class="report-group-head">
          <span>${esc(title)}</span>
          <span class="report-group-count">${items.length} riport</span>
        </div>
        ${items.map(itemHtml).join('')}
      </div>`;
  };

  const robotKey = (item) => {
    if (item.robot_serial_hash) return 'hash:' + String(item.robot_serial_hash);
    if (item.robot_serial_number) return 'serial:' + String(item.robot_serial_number);
    if (item.robot_id) return 'id:' + String(item.robot_id);
    return 'installation:' + String(item.installation_id || 'unknown');
  };

  const groupByRobot = (items) => {
    const groups = new Map();

    items.forEach((item) => {
      const key = robotKey(item);
      let group = groups.get(key);
      if (!group) {
        group = {
          key,
          robot_model: item.robot_model || null,
          robot_id: item.robot_id || null,
          robot_serial_number: item.robot_serial_number || null,
          robot_serial_hash: item.robot_serial_hash || null,
          installation_id: item.installation_id || null,
          latest_received_at: item.received_at || '',
          items: [],
        };
        groups.set(key, group);
      }

      group.items.push(item);
      if (!group.robot_model && item.robot_model) group.robot_model = item.robot_model;
      if (!group.robot_id && item.robot_id) group.robot_id = item.robot_id;
      if (!group.robot_serial_number && item.robot_serial_number) group.robot_serial_number = item.robot_serial_number;
      if (!group.robot_serial_hash && item.robot_serial_hash) group.robot_serial_hash = item.robot_serial_hash;
      if (!group.installation_id && item.installation_id) group.installation_id = item.installation_id;
      if ((item.received_at || '') > (group.latest_received_at || '')) group.latest_received_at = item.received_at || '';
    });

    return [...groups.values()].sort((a, b) =>
      String(b.latest_received_at || '').localeCompare(String(a.latest_received_at || ''))
    );
  };

  const robotGroupHtml = (group, index) => {
    const errors = group.items.filter(isAutomaticError);
    const robot = group.items.filter(item => item.report_type === 'robot' && !isAutomaticError(item));
    const integration = group.items.filter(item => item.report_type === 'integration' && !isAutomaticError(item));
    const other = group.items.filter(item => !['robot','integration'].includes(item.report_type) && !isAutomaticError(item));

    const model = group.robot_model || (group.robot_id ? 'Ismeretlen modell' : 'Telepítés');
    const identifier = group.robot_id || (group.installation_id ? shortId(group.installation_id) : 'Ismeretlen azonosító');
    const diagnosticCount = robot.length + integration.length + other.length;

    return `
      <details class="report-robot-group" ${index === 0 ? 'open' : ''}>
        <summary class="report-robot-summary">
          <div class="report-robot-title">
            <span class="report-robot-name">${esc(model)}</span>
            <span class="report-robot-id">${esc(identifier)}</span>
          </div>
          <div class="report-robot-stats">
            ${errors.length ? `<span class="report-stat error">${errors.length} hiba</span>` : ''}
            ${diagnosticCount ? `<span class="report-stat diag">${diagnosticCount} diagnosztika</span>` : ''}
            ${integration.length ? `<span class="report-stat integration">${integration.length} integráció</span>` : ''}
            <span class="report-stat">${group.items.length} összesen</span>
          </div>
        </summary>
        <div class="report-robot-body">
          ${sectionHtml('Automatikus hibariportok', errors)}
          ${sectionHtml('Robot diagnosztikák', robot)}
          ${sectionHtml('Integrációs diagnosztikák', integration)}
          ${sectionHtml('Egyéb riportok', other)}
        </div>
      </details>`;
  };

  async function renderGroupedDiagnostics(box) {
    if (!box || processed.has(box)) return;
    processed.add(box);
    ensureStyle();

    const section = box.closest('.section');
    const sectionTitle = section?.querySelector('.section-title');
    const sectionMeta = section?.querySelector('.section-head .small');
    if (sectionTitle) sectionTitle.textContent = 'Riportok robotonként';
    if (sectionMeta) sectionMeta.textContent = 'betöltés…';

    try {
      const res = await fetch('/api/anthbot/admin/diagnostics/summary?limit=500', {credentials: 'same-origin'});
      if (res.status === 401) { location.href = '/dashboard'; return; }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);

      const data = await res.json();
      const items = Array.isArray(data.items) ? data.items : [];
      const robotGroups = groupByRobot(items);
      if (sectionMeta) {
        sectionMeta.textContent = `${robotGroups.length} robot/csoport · ${items.length} riport`;
      }

      box.innerHTML = robotGroups.length
        ? `<div class="report-groups">${robotGroups.map(robotGroupHtml).join('')}</div>`
        : '<div class="report-empty">Még nem érkezett diagnosztikai jelentés.</div>';

      box.querySelectorAll('.diag-item[data-report-id]').forEach((item) => {
        const reportId = item.dataset.reportId;
        if (!reportId) return;
        item.style.cursor = 'pointer';
        item.setAttribute('aria-label', 'Diagnosztika megnyitása: ' + reportId);
        item.addEventListener('click', () => openReport(reportId));
        item.addEventListener('keydown', (event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            openReport(reportId);
          }
        });
      });
    } catch (err) {
      if (sectionMeta) sectionMeta.textContent = 'hiba';
      box.innerHTML = `<div class="empty">A riportok csoportosítása nem sikerült: ${esc(err.message)}</div>`;
    }
  }

  const wire = () => {
    const box = document.getElementById('diag-list');
    if (box) renderGroupedDiagnostics(box);
  };

  new MutationObserver(wire).observe(document.documentElement, {childList: true, subtree: true});
  wire();
})();
</script>
""".encode("utf-8")


class DeveloperAgentStorageMiddleware:
    def __init__(self, app):
        self.app = app
        self._initialized = False

    async def __call__(self, scope, receive, send):
        path = str(scope.get("path", ""))
        if scope.get("type") == "http" and "developer-agent" in path and not self._initialized:
            init_developer_agent_tables()
            self._initialized = True
        await self.app(scope, receive, send)


class DashboardEmbeddingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = str(scope.get("path", ""))
        if scope.get("type") != "http" or not path.startswith("/dashboard"):
            await self.app(scope, receive, send)
            return

        inject_diagnostics = path.rstrip("/") == "/dashboard"

        async def send_wrapped(message):
            if message.get("type") == "http.response.start":
                headers = []
                for raw_name, raw_value in message.get("headers", []):
                    name = raw_name.decode("latin-1").lower()
                    if name in {"x-frame-options", "content-security-policy"}:
                        continue
                    if inject_diagnostics and name == "content-length":
                        continue
                    if name == "set-cookie":
                        value = raw_value.decode("latin-1")
                        value = re.sub(r"(?i)samesite=strict", "SameSite=None", value)
                        raw_value = value.encode("latin-1")
                    headers.append((raw_name, raw_value))

                headers.append((b"content-security-policy", f"frame-ancestors {_FRAME_ANCESTORS}".encode("latin-1")))
                message = {**message, "headers": headers}

            elif message.get("type") == "http.response.body" and inject_diagnostics:
                body = message.get("body", b"")
                if b"</body>" in body:
                    body = body.replace(b"</body>", _DASHBOARD_DIAGNOSTICS_SCRIPT + b"</body>", 1)
                    message = {**message, "body": body}

            await send(message)

        await self.app(scope, receive, send_wrapped)


app = DashboardEmbeddingMiddleware(DeveloperAgentStorageMiddleware(fastapi_app))
