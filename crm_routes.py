"""
crm_routes.py — JAMIE CRM dashboard + kanban board.
Mounts onto the existing ARIA™ FastAPI app (main.py) as a router.

Routes:
  GET  /crm                     -> HTML dashboard (kanban board + rig panel)
  GET  /crm/summary             -> JSON summary tile (matches dashboard.html's fetch shape)
  GET  /crm/leads               -> JSON list of jamie_leads, for the kanban board
  POST /crm/leads/{lead_id}/status -> move a lead between kanban columns
  GET  /crm/rig                 -> saved rig config (ngrok URL + masked token)
  POST /crm/rig                 -> save rig config to jamie_config table
  GET  /crm/status              -> system_status table (rig heartbeat, from startup_check.py)

Requires the tables created in Supabase: jamie_leads, jamie_config,
system_status (SQL already run).
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from core.memory import supabase_select, supabase_update, supabase_upsert

router = APIRouter()

VALID_STATUSES = {"new", "contacted", "replied", "won", "lost"}


class LeadStatusUpdate(BaseModel):
    status: str


class RigConfig(BaseModel):
    ngrok_url: str
    token: str


@router.get("/crm/summary")
async def crm_summary():
    leads = supabase_select("jamie_leads", limit=1000)
    total = len(leads)
    hot_today = len([l for l in leads if (l.get("total_score") or 0) >= 70])
    replied = len([l for l in leads if l.get("status") == "replied"])
    contacted = len([l for l in leads if l.get("status") in ("contacted", "replied", "won")])
    reply_rate = round((replied / contacted) * 100, 1) if contacted else 0
    return {
        "total_leads": total,
        "hot_leads_today": hot_today,
        "emails_sent": contacted,
        "reply_rate": reply_rate,
    }


@router.get("/crm/leads")
async def crm_leads():
    leads = supabase_select("jamie_leads", order_by="total_score", limit=500)
    return {"leads": leads}


@router.post("/crm/leads/{lead_id}/status")
async def update_lead_status(lead_id: int, payload: LeadStatusUpdate):
    if payload.status not in VALID_STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(VALID_STATUSES)}")
    result = supabase_update("jamie_leads", row_id=lead_id, data={"status": payload.status})
    if not result:
        raise HTTPException(404, "lead not found")
    return {"status": "updated", "lead": result}


@router.get("/crm/rig")
async def get_rig_config():
    rows = supabase_select("jamie_config", limit=10)
    cfg = {r["key"]: r["value"] for r in rows}
    token = cfg.get("token", "")
    return {
        "ngrok_url": cfg.get("ngrok_url", ""),
        "token_set": bool(token),
        "token_preview": (token[:4] + "…") if token else "",
    }


@router.post("/crm/rig")
async def save_rig_config(payload: RigConfig):
    supabase_upsert("jamie_config", {"key": "ngrok_url", "value": payload.ngrok_url}, on_conflict="key")
    supabase_upsert("jamie_config", {"key": "token", "value": payload.token}, on_conflict="key")
    return {"status": "saved"}


@router.get("/crm/status")
async def crm_status():
    rows = supabase_select("system_status", limit=20)
    return {"tools": rows}


CRM_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>JAMIE CRM — ARIAtrust</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --bg: #0b0d10; --panel: #14171c; --border: #262b33;
    --text: #e7e9ec; --dim: #8b93a1; --accent: #5b8cff;
    --new: #5b8cff; --contacted: #f0a83e; --replied: #4ec97f; --won: #b76ef0; --lost: #ef5b6b;
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font-family: -apple-system, Segoe UI, Roboto, sans-serif; }
  header { padding:20px 28px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; align-items:center; }
  header h1 { font-size:18px; margin:0; font-weight:600; }
  header .status-dot { width:8px; height:8px; border-radius:50%; display:inline-block; margin-right:6px; }
  .summary { display:flex; gap:16px; padding:20px 28px; flex-wrap:wrap; }
  .card { background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:16px 20px; min-width:140px; }
  .card .num { font-size:26px; font-weight:700; }
  .card .label { color:var(--dim); font-size:12px; margin-top:4px; }
  .rig-panel { margin:0 28px 20px; background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:18px 20px; }
  .rig-panel h2 { font-size:14px; margin:0 0 12px; color:var(--dim); text-transform:uppercase; letter-spacing:.05em; }
  .rig-row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
  .rig-row input { background:#0e1013; border:1px solid var(--border); color:var(--text); padding:9px 12px; border-radius:6px; font-size:13px; flex:1; min-width:220px; }
  button { background:var(--accent); color:#fff; border:none; padding:9px 16px; border-radius:6px; font-size:13px; cursor:pointer; font-weight:600; }
  button.secondary { background:#1e222a; color:var(--text); border:1px solid var(--border); }
  button:hover { opacity:0.9; }
  .board { display:grid; grid-template-columns: repeat(5, 1fr); gap:14px; padding:0 28px 28px; }
  .col { background:var(--panel); border:1px solid var(--border); border-radius:10px; min-height:400px; padding:12px; }
  .col h3 { font-size:12px; text-transform:uppercase; letter-spacing:.05em; color:var(--dim); margin:4px 6px 10px; display:flex; justify-content:space-between; }
  .lead-card { background:#1a1e25; border:1px solid var(--border); border-radius:8px; padding:10px 12px; margin-bottom:8px; cursor:grab; }
  .lead-card:active { cursor:grabbing; }
  .lead-card .name { font-weight:600; font-size:13px; }
  .lead-card .meta { color:var(--dim); font-size:11px; margin-top:4px; display:flex; justify-content:space-between; }
  .badge { padding:1px 7px; border-radius:20px; font-size:10px; font-weight:700; }
  .badge.confirmed { background:#173b28; color:var(--replied); }
  .badge.single_source { background:#3b2f17; color:var(--contacted); }
  .col[data-status="new"] h3 { color:var(--new); }
  .col[data-status="contacted"] h3 { color:var(--contacted); }
  .col[data-status="replied"] h3 { color:var(--replied); }
  .col[data-status="won"] h3 { color:var(--won); }
  .col[data-status="lost"] h3 { color:var(--lost); }
  .col.dragover { outline: 2px dashed var(--accent); outline-offset:-4px; }
  .empty { color:var(--dim); font-size:12px; padding:8px; text-align:center; }
  #log { margin:0 28px 28px; background:#08090b; border:1px solid var(--border); border-radius:10px; padding:12px 16px; font-family:monospace; font-size:12px; color:#7ee787; height:140px; overflow-y:auto; display:none; }
</style>
</head>
<body>

<header>
  <h1>JAMIE CRM <span style="color:var(--dim); font-weight:400;">— ARIAtrust</span></h1>
  <div id="rig-status" style="font-size:12px; color:var(--dim);"><span class="status-dot" style="background:var(--dim);"></span>checking rig…</div>
</header>

<div class="summary" id="summary">
  <div class="card"><div class="num" id="s-total">–</div><div class="label">Total Leads</div></div>
  <div class="card"><div class="num" id="s-hot">–</div><div class="label">Hot Leads (70+)</div></div>
  <div class="card"><div class="num" id="s-contacted">–</div><div class="label">Contacted</div></div>
  <div class="card"><div class="num" id="s-rate">–</div><div class="label">Reply Rate</div></div>
</div>

<div class="rig-panel">
  <h2>Rig Connection</h2>
  <div class="rig-row">
    <input id="ngrok-url" placeholder="ngrok URL, e.g. https://abc123.ngrok-free.app">
    <input id="rig-token" type="password" placeholder="JAMIE_CONTROL_TOKEN">
    <button class="secondary" onclick="saveRig()">Save</button>
    <button onclick="startHunt()">Start Hunt</button>
  </div>
  <div class="rig-row" style="margin-top:10px;">
    <input id="mission-text" placeholder="Mission, e.g. Find seed-stage fintech startups in London">
  </div>
</div>

<pre id="log"></pre>

<div class="board" id="board">
  <div class="col" data-status="new"><h3>New <span id="count-new">0</span></h3><div class="cards"></div></div>
  <div class="col" data-status="contacted"><h3>Contacted <span id="count-contacted">0</span></h3><div class="cards"></div></div>
  <div class="col" data-status="replied"><h3>Replied <span id="count-replied">0</span></h3><div class="cards"></div></div>
  <div class="col" data-status="won"><h3>Won <span id="count-won">0</span></h3><div class="cards"></div></div>
  <div class="col" data-status="lost"><h3>Lost <span id="count-lost">0</span></h3><div class="cards"></div></div>
</div>

<script>
let draggedId = null;

async function loadSummary() {
  const r = await fetch('/crm/summary');
  const d = await r.json();
  document.getElementById('s-total').textContent = d.total_leads;
  document.getElementById('s-hot').textContent = d.hot_leads_today;
  document.getElementById('s-contacted').textContent = d.emails_sent;
  document.getElementById('s-rate').textContent = d.reply_rate + '%';
}

async function loadLeads() {
  const r = await fetch('/crm/leads');
  const d = await r.json();
  const cols = {new: [], contacted: [], replied: [], won: [], lost: []};
  for (const lead of d.leads) {
    const status = cols[lead.status] ? lead.status : 'new';
    cols[status].push(lead);
  }
  for (const status in cols) {
    const col = document.querySelector(`.col[data-status="${status}"] .cards`);
    document.getElementById('count-' + status).textContent = cols[status].length;
    col.innerHTML = cols[status].length ? '' : '<div class="empty">No leads</div>';
    for (const lead of cols[status]) {
      const card = document.createElement('div');
      card.className = 'lead-card';
      card.draggable = true;
      card.dataset.id = lead.id;
      card.innerHTML = `
        <div class="name">${lead.name}</div>
        <div class="meta">
          <span class="badge ${lead.confidence}">${lead.confidence === 'confirmed' ? 'Confirmed' : 'Single Source'}</span>
          <span>${lead.total_score}/100</span>
        </div>`;
      card.addEventListener('dragstart', () => { draggedId = lead.id; card.style.opacity = '0.4'; });
      card.addEventListener('dragend', () => { card.style.opacity = '1'; });
      col.appendChild(card);
    }
  }
}

document.querySelectorAll('.col').forEach(col => {
  col.addEventListener('dragover', e => { e.preventDefault(); col.classList.add('dragover'); });
  col.addEventListener('dragleave', () => col.classList.remove('dragover'));
  col.addEventListener('drop', async e => {
    e.preventDefault();
    col.classList.remove('dragover');
    if (draggedId === null) return;
    const newStatus = col.dataset.status;
    await fetch(`/crm/leads/${draggedId}/status`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({status: newStatus})
    });
    draggedId = null;
    loadLeads();
    loadSummary();
  });
});

async function loadRig() {
  const r = await fetch('/crm/rig');
  const d = await r.json();
  if (d.ngrok_url) document.getElementById('ngrok-url').value = d.ngrok_url;
  if (d.token_set) document.getElementById('rig-token').placeholder = 'saved (' + d.token_preview + '…)';
  checkRigLive(d.ngrok_url);
}

async function saveRig() {
  const ngrok_url = document.getElementById('ngrok-url').value.trim();
  const token = document.getElementById('rig-token').value.trim();
  await fetch('/crm/rig', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ngrok_url, token})
  });
  checkRigLive(ngrok_url);
}

async function checkRigLive(url) {
  const dot = document.querySelector('#rig-status .status-dot');
  const label = document.getElementById('rig-status');
  if (!url) { label.innerHTML = '<span class="status-dot" style="background:var(--dim);"></span>no rig URL saved'; return; }
  try {
    const r = await fetch(url.replace(/\\/$/, '') + '/health', {mode: 'cors'});
    if (r.ok) {
      label.innerHTML = '<span class="status-dot" style="background:var(--replied);"></span>rig online';
    } else {
      label.innerHTML = '<span class="status-dot" style="background:var(--lost);"></span>rig unreachable';
    }
  } catch (e) {
    label.innerHTML = '<span class="status-dot" style="background:var(--lost);"></span>rig unreachable';
  }
}

async function startHunt() {
  const ngrokUrl = document.getElementById('ngrok-url').value.trim();
  const token = document.getElementById('rig-token').value.trim();
  const mission = document.getElementById('mission-text').value.trim();
  if (!ngrokUrl || !mission) { alert('Enter the rig URL and a mission first.'); return; }

  const log = document.getElementById('log');
  log.style.display = 'block';
  log.textContent = 'Starting mission…\\n';

  try {
    await fetch(ngrokUrl.replace(/\\/$/, '') + '/start', {
      method: 'POST',
      mode: 'cors',
      headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token},
      body: JSON.stringify({mission})
    });
    const es = new EventSource(ngrokUrl.replace(/\\/$/, '') + '/stream?token=' + encodeURIComponent(token));
    es.onmessage = (e) => { log.textContent += e.data + '\\n'; log.scrollTop = log.scrollHeight; };
    es.onerror = () => { log.textContent += '[stream closed]\\n'; es.close(); loadLeads(); loadSummary(); };
  } catch (e) {
    log.textContent += 'Could not reach rig: ' + e + '\\n';
  }
}

loadSummary();
loadLeads();
loadRig();
setInterval(() => { loadSummary(); loadLeads(); }, 15000);
</script>
</body>
</html>"""


@router.get("/crm", response_class=HTMLResponse)
async def crm_dashboard():
    return CRM_HTML
