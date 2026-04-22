"""
main.py — FastAPI 入口 + 后台轮询调度
"""

import asyncio
import logging
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from app.config import config
from app.database import init_db, list_all_threads
from app.mail_service import fetch_unread_emails
from app.agent import run_check_cycle

# ─── 日志配置 ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

_log_buffer: deque = deque(maxlen=300)


class _MemLogHandler(logging.Handler):
    def emit(self, record):
        _log_buffer.append({
            "timestamp": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
            "level":     record.levelname,
            "message":   self.format(record),
        })


_mem_handler = _MemLogHandler()
_mem_handler.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger().addHandler(_mem_handler)

# ─── 全局状态 ──────────────────────────────────────────────────────────────────

_bg_task    = None
_is_running = False


# ─── 应用生命周期 ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("🚀 KOL Outreach Agent 启动")
    logger.info(f"   品牌: {config.BRAND_NAME}")
    logger.info(f"   邮箱: {config.EMAIL_ADDRESS}")
    logger.info(f"   LLM:  {config.LLM_MODEL} @ {config.LLM_BASE_URL}")
    logger.info(f"   轮询间隔: {config.POLL_INTERVAL} 秒")
    logger.info("   Dashboard: http://localhost:8000/dashboard")
    yield
    logger.info("👋 服务已关闭")


app = FastAPI(
    title="KOL Outreach Agent",
    description="电商达人合作谈判智能体 — 阿里企业邮箱 + AI 驱动，防 Spam 回复",
    version="2.1.0",
    lifespan=lifespan,
)


# ─── 后台轮询 ──────────────────────────────────────────────────────────────────

async def _polling_loop():
    global _is_running
    while _is_running:
        try:
            run_check_cycle()
        except Exception as e:
            logger.error(f"❌ 轮询异常: {e}", exc_info=True)
        logger.info(f"⏰ 下次检查: {config.POLL_INTERVAL} 秒后")
        await asyncio.sleep(config.POLL_INTERVAL)


# ─── REST API ──────────────────────────────────────────────────────────────────

@app.get("/", summary="健康检查")
async def root():
    return {"status": "ok", "agent": "KOL Outreach Agent", "version": "2.1.0"}


@app.post("/start-auto", summary="启动后台自动轮询")
async def start_auto():
    global _bg_task, _is_running
    if _is_running:
        return {"status": "already_running"}
    _is_running = True
    _bg_task = asyncio.create_task(_polling_loop())
    logger.info(f"🤖 已启动后台轮询，间隔 {config.POLL_INTERVAL} 秒")
    return {"status": "started", "poll_interval_seconds": config.POLL_INTERVAL}


@app.post("/stop-auto", summary="停止后台自动轮询")
async def stop_auto():
    global _bg_task, _is_running
    if not _is_running:
        return {"status": "not_running"}
    _is_running = False
    if _bg_task:
        _bg_task.cancel()
        _bg_task = None
    logger.info("⏹️ 已停止后台轮询")
    return {"status": "stopped"}


@app.post("/check", summary="立即执行一次邮件检查")
async def check_now():
    try:
        result = run_check_cycle()
        return {"status": "success", **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/status", summary="查看服务状态")
async def get_status():
    return {
        "auto_polling":         _is_running,
        "poll_interval_seconds": config.POLL_INTERVAL,
        "email_account":        config.EMAIL_ADDRESS,
        "brand":                config.BRAND_NAME,
        "llm_model":            config.LLM_MODEL,
    }


@app.get("/emails", summary="查看当前未读邮件列表（不触发回复）")
async def list_emails(limit: int = 10):
    try:
        emails = fetch_unread_emails(limit=limit)
        return {"count": len(emails), "emails": [
            {"uid": m["uid"], "from": m["from_raw"], "subject": m["subject"],
             "date": m["date"], "has_message_id": bool(m["message_id"])}
            for m in emails
        ]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/kols", summary="查看所有 KOL 会话状态")
async def list_kols():
    threads = list_all_threads()
    return {"count": len(threads), "kols": threads}


@app.get("/logs", summary="获取最近运行日志")
async def get_logs(tail: int = 60):
    logs = list(_log_buffer)[-tail:]
    return {"count": len(logs), "logs": logs}


# ─── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse, summary="监控面板")
async def dashboard():
    return HTMLResponse(content=_DASHBOARD_HTML)


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>KOL Outreach Agent</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',sans-serif;background:linear-gradient(135deg,#0f0c29,#302b63,#24243e);min-height:100vh;padding:20px;color:#fff}
.wrap{max-width:1280px;margin:0 auto}
h1{text-align:center;margin-bottom:24px;font-size:24px;color:#a78bfa}
.controls{display:flex;gap:10px;margin-bottom:20px;flex-wrap:wrap}
.btn{padding:10px 20px;border-radius:8px;border:none;cursor:pointer;font-weight:700;font-size:14px;transition:.2s}
.btn-start{background:#34d399;color:#064e3b}.btn-start:hover{background:#10b981}
.btn-stop{background:#f87171;color:#fff}.btn-stop:hover{background:#ef4444}
.btn-check{background:#fbbf24;color:#1c1917}.btn-check:hover{background:#f59e0b}
.btn-rf{background:#818cf8;color:#fff}.btn-rf:hover{background:#6366f1}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:20px}
.card{background:rgba(255,255,255,.08);border-radius:12px;padding:16px;border:1px solid rgba(255,255,255,.12)}
.card h4{color:#94a3b8;font-size:11px;margin-bottom:8px;text-transform:uppercase}
.card .val{font-size:20px;font-weight:700;color:#a78bfa;word-break:break-all}
.val.on{color:#34d399}.val.off{color:#f87171}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:760px){.grid2{grid-template-columns:1fr}}
.panel{background:rgba(255,255,255,.05);border-radius:12px;padding:16px;border:1px solid rgba(255,255,255,.1)}
.panel h3{color:#a78bfa;margin-bottom:12px;font-size:14px}
.log-item{padding:6px 8px;border-bottom:1px solid rgba(255,255,255,.06);font-family:Consolas,monospace;font-size:12px;display:flex;gap:8px}
.log-ts{color:#475569;min-width:150px;flex-shrink:0}
.log-lv{padding:1px 6px;border-radius:3px;font-size:10px;min-width:52px;text-align:center;flex-shrink:0}
.lv-INFO{background:#1e3a5f;color:#7dd3fc}.lv-WARNING{background:#422006;color:#fbbf24}.lv-ERROR{background:#450a0a;color:#fca5a5}
.log-msg{color:#cbd5e1;flex:1;word-break:break-all}
.kol-item{padding:10px 12px;border-left:3px solid #a78bfa;background:rgba(167,139,250,.05);margin-bottom:8px;border-radius:0 8px 8px 0}
.kol-item.s1{border-color:#60a5fa}.kol-item.s2{border-color:#fbbf24}.kol-item.s3{border-color:#34d399}.kol-item.s4{border-color:#f472b6}
.kol-name{font-weight:700;font-size:14px;margin-bottom:3px}
.kol-meta{font-size:11px;color:#94a3b8}
.sb{display:inline-block;padding:1px 8px;border-radius:10px;font-size:10px;font-weight:700;margin-left:6px}
.sb1{background:#1e3a5f;color:#60a5fa}.sb2{background:#422006;color:#fbbf24}.sb3{background:#064e3b;color:#34d399}.sb4{background:#4a044e;color:#f472b6}
.empty{text-align:center;color:#475569;padding:24px;font-size:13px}
</style>
</head>
<body>
<div class="wrap">
<h1>🤖 KOL Outreach Agent 监控面板</h1>
<div class="controls">
  <button class="btn btn-start" onclick="api('POST','/start-auto','已启动')">▶ 启动轮询</button>
  <button class="btn btn-stop"  onclick="api('POST','/stop-auto','已停止')">⏹ 停止轮询</button>
  <button class="btn btn-check" onclick="api('POST','/check','检查完成').then(loadKols)">📬 立即检查</button>
  <button class="btn btn-rf"    onclick="loadAll()">🔄 刷新</button>
</div>
<div class="cards">
  <div class="card"><h4>轮询状态</h4><div class="val" id="c-status">-</div></div>
  <div class="card"><h4>轮询间隔</h4><div class="val" id="c-interval">-</div></div>
  <div class="card"><h4>邮箱账户</h4><div class="val" id="c-email" style="font-size:12px">-</div></div>
  <div class="card"><h4>LLM 模型</h4><div class="val" id="c-model" style="font-size:13px">-</div></div>
  <div class="card"><h4>KOL 总数</h4><div class="val" id="c-kols">-</div></div>
</div>
<div class="grid2">
  <div class="panel"><h3>📊 KOL 会话进度</h3><div id="kol-list"><div class="empty">加载中...</div></div></div>
  <div class="panel"><h3>📝 实时日志</h3><div id="log-list" style="max-height:460px;overflow-y:auto"><div class="empty">加载中...</div></div></div>
</div>
</div>
<script>
const SL={1:'破冰邀请',2:'规则确认',3:'跟进评价',4:'返款确认'};
function esc(s){const d=document.createElement('div');d.textContent=s||'';return d.innerHTML;}
async function api(m,u,msg){
  try{const r=await fetch(u,{method:m});const d=await r.json();
  if(msg)alert(msg+(d.processed!=null?`\\n处理 ${d.processed} 封，成功 ${d.success} 封`:''));
  loadStatus();return d;}catch(e){alert('失败:'+e.message);}
}
async function loadStatus(){
  const d=await(await fetch('/status')).json();
  document.getElementById('c-status').textContent=d.auto_polling?'运行中':'已停止';
  document.getElementById('c-status').className='val '+(d.auto_polling?'on':'off');
  document.getElementById('c-interval').textContent=d.poll_interval_seconds+'秒';
  document.getElementById('c-email').textContent=d.email_account||'-';
  document.getElementById('c-model').textContent=d.llm_model||'-';
}
async function loadKols(){
  const d=await(await fetch('/kols')).json();
  document.getElementById('c-kols').textContent=d.count;
  const el=document.getElementById('kol-list');
  if(!d.kols.length){el.innerHTML='<div class="empty">暂无 KOL 记录</div>';return;}
  el.innerHTML=d.kols.map(k=>`<div class="kol-item s${k.current_stage}">
    <div class="kol-name">${esc(k.kol_name||k.kol_email)}<span class="sb sb${k.current_stage}">阶段${k.current_stage} ${SL[k.current_stage]||''}</span></div>
    <div class="kol-meta">${esc(k.kol_email)} · ${k.updated_at.slice(0,16)}</div>
    ${k.notes?`<div class="kol-meta" style="margin-top:3px;color:#6b7280">${esc(k.notes.slice(0,90))}</div>`:''}
  </div>`).join('');
}
async function loadLogs(){
  const d=await(await fetch('/logs?tail=60')).json();
  const el=document.getElementById('log-list');
  if(!d.logs.length){el.innerHTML='<div class="empty">暂无日志</div>';return;}
  el.innerHTML=[...d.logs].reverse().map(l=>`<div class="log-item">
    <span class="log-ts">${l.timestamp}</span>
    <span class="log-lv lv-${l.level}">${l.level}</span>
    <span class="log-msg">${esc(l.message)}</span>
  </div>`).join('');
}
function loadAll(){loadStatus();loadKols();loadLogs();}
loadAll();
setInterval(loadStatus,5000);setInterval(loadLogs,4000);setInterval(loadKols,10000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, reload=False)
