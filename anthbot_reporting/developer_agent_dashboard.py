from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse

from developer_agent_api import _db, require_admin

router = APIRouter()


_JOBS_FIX_SCRIPT = r"""
<script id="anthbot-developer-agent-jobs-fix">
(() => {
  const renderJobs = (rows) => {
    const host = document.querySelector('#jobs');
    if (!host) return;
    host.innerHTML = rows.length ? `<table><thead><tr><th>ID</th><th>Telepítés</th><th>Probe</th><th>Állapot</th><th>Idő</th><th>Eredmény</th></tr></thead><tbody>${rows.map(x=>`<tr><td>${x.job_id}</td><td class="id">${esc(x.installation_id)}</td><td>${esc(PROBE_LABELS[x.action]||x.action)}${x.target_model?`<br><span class="muted">${esc(x.target_model)}</span>`:''}${x.params&&Object.keys(x.params).length?`<details><summary class="muted">paraméterek</summary><div class="jobparams">${esc(pretty(x.params))}</div></details>`:''}</td><td>${esc(x.status)}${x.error?`<br><span class="off">${esc(x.error)}</span>`:''}</td><td>${esc(x.created_at)}<br><span class="muted">${esc(x.completed_at||'')}</span></td><td><div class="result-actions"><button type="button" onclick="loadJobResult(${Number(x.job_id)},this)">Megnyitás</button><button type="button" onclick="downloadJobResult(${Number(x.job_id)},this)">💾 Mentés</button></div><div id="job-result-${Number(x.job_id)}" class="result" hidden></div></td></tr>`).join('')}</tbody></table>`:'<p class="muted">Még nincs teszt.</p>';
  };

  const mergeJob = (item) => {
    const index = jobs.findIndex(x => String(x.job_id) === String(item.job_id));
    if (index >= 0) jobs[index] = {...jobs[index], ...item};
    else jobs.push(item);
    return index >= 0 ? jobs[index] : item;
  };

  const fetchJob = async (jobId) => {
    const cached = jobs.find(x => String(x.job_id) === String(jobId) && (Object.prototype.hasOwnProperty.call(x, 'result') || x.result_error));
    if (cached) return cached;
    const item = await api(`/api/anthbot/admin/developer-agent/jobs/${encodeURIComponent(jobId)}`);
    return mergeJob(item);
  };

  loadJobs = async function() {
    const host = document.querySelector('#jobs');
    if (!host) return;
    try {
      const data = await api('/api/anthbot/admin/developer-agent/jobs?limit=100');
      jobs = data.items || [];
      renderJobs(jobs);
    } catch (e) {
      jobs = [];
      host.innerHTML = `<p class="off">Hiba a tesztek betöltésekor: ${esc(e.message)}</p>`;
    }
  };

  window.loadJobResult = async function(jobId, button) {
    const box = document.getElementById(`job-result-${jobId}`);
    if (!box) return;
    if (!box.hidden) {
      box.hidden = true;
      if (button) button.textContent = 'Megnyitás';
      return;
    }
    if (button) {
      button.disabled = true;
      button.textContent = 'Betöltés…';
    }
    try {
      const item = await fetchJob(jobId);
      if (Object.prototype.hasOwnProperty.call(item, 'result')) box.textContent = pretty(item.result);
      else if (item.result_error) box.textContent = `Hiba: ${item.result_error}`;
      else box.textContent = 'Ehhez a teszthez nincs tárolt eredmény.';
      box.hidden = false;
      if (button) button.textContent = 'Bezárás';
    } catch (e) {
      box.textContent = `Hiba az eredmény betöltésekor: ${e.message}`;
      box.hidden = false;
      if (button) button.textContent = 'Újra';
    } finally {
      if (button) button.disabled = false;
    }
  };

  downloadJobResult = async function(jobId, button) {
    const original = button ? button.textContent : '';
    if (button) {
      button.disabled = true;
      button.textContent = 'Betöltés…';
    }
    try {
      const x = await fetchJob(jobId);
      if (!Object.prototype.hasOwnProperty.call(x, 'result')) {
        alert(x.result_error ? `Az eredmény nem olvasható: ${x.result_error}` : 'Ehhez a teszthez nincs tárolt eredmény.');
        return;
      }
      const payload = {job_id:x.job_id,installation_id:x.installation_id,action:x.action,target_model:x.target_model??null,params:x.params??{},status:x.status,created_at:x.created_at,completed_at:x.completed_at??null,error:x.error??null,result:x.result};
      const blob = new Blob([JSON.stringify(payload,null,2)+'\n'], {type:'application/json;charset=utf-8'});
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `ANTHBOT_${safeFilePart(x.target_model)}_${safeFilePart(x.action)}_${x.job_id}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (e) {
      alert(`Hiba az eredmény letöltésekor: ${e.message}`);
    } finally {
      if (button) {
        button.disabled = false;
        button.textContent = original || '💾 Mentés';
      }
    }
  };
  window.downloadJobResult = downloadJobResult;

  load();
})();
</script>
"""


@router.get(
    "/api/anthbot/admin/developer-agent/jobs/{job_id}",
    dependencies=[Depends(require_admin)],
)
def developer_agent_job(job_id: int) -> dict[str, object]:
    """Return one job and its result without decoding every historical result."""
    with _db() as conn:
        row = conn.execute(
            """
            SELECT job_id, installation_id, action, target_model, params_json,
                   status, created_at, claimed_at, completed_at,
                   result_json, error
            FROM developer_agent_jobs
            WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="developer agent job not found")

    try:
        params = json.loads(row["params_json"]) if row["params_json"] else {}
    except (TypeError, ValueError):
        params = {}

    item: dict[str, object] = {
        "job_id": row["job_id"],
        "installation_id": row["installation_id"],
        "action": row["action"],
        "target_model": row["target_model"],
        "params": params,
        "status": row["status"],
        "created_at": row["created_at"],
        "claimed_at": row["claimed_at"],
        "completed_at": row["completed_at"],
        "error": row["error"],
    }
    if row["result_json"]:
        try:
            item["result"] = json.loads(row["result_json"])
        except (TypeError, ValueError) as err:
            item["result_error"] = f"stored result is invalid JSON: {err}"
    return item


@router.get(
    "/dashboard/developer-agent",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin)],
)
def developer_agent_dashboard() -> HTMLResponse:
    try:
        html = Path(__file__).with_name("developer_agent_dashboard.html").read_text(
            encoding="utf-8"
        )
    except OSError as err:
        raise HTTPException(status_code=503, detail="developer-agent dashboard unavailable") from err

    # The bundled page used to fetch the full result payload of the latest 100
    # jobs every 30 seconds. Large full-state diagnostics eventually made that
    # request slow or fragile enough that the jobs section stayed blank. Skip
    # that initial eager load, override it with the metadata-only loader below,
    # and fetch one result only when the user opens or saves it.
    html = html.replace(
        "load();setInterval(load,30000);",
        "setInterval(load,30000);",
        1,
    )
    html = html.replace("</body>", f"{_JOBS_FIX_SCRIPT}</body>", 1)
    return HTMLResponse(html)
