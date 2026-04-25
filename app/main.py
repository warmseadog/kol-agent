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
from app.database import (
    init_db,
    list_all_threads,
    list_processed_messages,
    get_thread_messages,
    delete_thread,
    delete_all_data,
)
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
    version="3.0.0",
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
    return {"status": "ok", "agent": "KOL Outreach Agent", "version": "3.0.0"}


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
        "auto_polling":          _is_running,
        "poll_interval_seconds": config.POLL_INTERVAL,
        "email_account":         config.EMAIL_ADDRESS,
        "brand":                 config.BRAND_NAME,
        "llm_model":             config.LLM_MODEL,
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


@app.get("/processed", summary="查看最近已处理的邮件记录")
async def list_processed(limit: int = 50):
    records = list_processed_messages(limit=limit)
    return {"count": len(records), "records": records}


@app.get("/thread/{thread_id}", summary="查看某个线程的完整对话历史")
async def get_thread(thread_id: str, limit: int = 20):
    msgs = get_thread_messages(thread_id, limit=limit)
    return {"thread_id": thread_id, "count": len(msgs), "messages": msgs}


@app.delete("/thread/{thread_id}", summary="删除指定 Thread 的全部数据")
async def delete_thread_api(thread_id: str):
    deleted = delete_thread(thread_id)
    logger.info(f"🗑️ 已删除 Thread: {thread_id[:60]} | 共 {deleted} 条记录")
    return {"status": "deleted", "thread_id": thread_id, "deleted_rows": deleted}


@app.delete("/all-data", summary="清空全部数据（测试重置）")
async def delete_all_api():
    result = delete_all_data()
    logger.info(f"🗑️ 已清空全部数据: {result}")
    return {"status": "cleared", **result}


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
.wrap{max-width:1400px;margin:0 auto}
h1{text-align:center;margin-bottom:24px;font-size:24px;color:#a78bfa}
.controls{display:flex;gap:10px;margin-bottom:20px;flex-wrap:wrap}
.btn{padding:10px 20px;border-radius:8px;border:none;cursor:pointer;font-weight:700;font-size:14px;transition:.2s}
.btn-start{background:#34d399;color:#064e3b}.btn-start:hover{background:#10b981}
.btn-stop{background:#f87171;color:#fff}.btn-stop:hover{background:#ef4444}
.btn-check{background:#fbbf24;color:#1c1917}.btn-check:hover{background:#f59e0b}
.btn-rf{background:#818cf8;color:#fff}.btn-rf:hover{background:#6366f1}
.btn-del{background:#be123c;color:#fff}.btn-del:hover{background:#9f1239}
.btn-del-sm{background:transparent;border:1px solid #be123c;color:#fb7185;border-radius:6px;padding:2px 8px;font-size:11px;cursor:pointer;font-weight:600;flex-shrink:0;transition:.15s}.btn-del-sm:hover{background:#be123c;color:#fff}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}
.card{background:rgba(255,255,255,.08);border-radius:12px;padding:16px;border:1px solid rgba(255,255,255,.12)}
.card h4{color:#94a3b8;font-size:11px;margin-bottom:8px;text-transform:uppercase}
.card .val{font-size:20px;font-weight:700;color:#a78bfa;word-break:break-all}
.val.on{color:#34d399}.val.off{color:#f87171}
/* tab navigation */
.tabs{display:flex;gap:4px;margin-bottom:16px;border-bottom:1px solid rgba(255,255,255,.12);padding-bottom:0}
.tab{padding:10px 20px;cursor:pointer;border-radius:8px 8px 0 0;font-size:13px;font-weight:600;color:#94a3b8;background:rgba(255,255,255,.04);border:1px solid transparent;border-bottom:none;transition:.2s}
.tab.active{color:#a78bfa;background:rgba(167,139,250,.12);border-color:rgba(167,139,250,.3)}
.tab-content{display:none}.tab-content.active{display:block}
.panel{background:rgba(255,255,255,.05);border-radius:0 12px 12px 12px;padding:16px;border:1px solid rgba(255,255,255,.1)}
/* KOL list */
.kol-item{padding:10px 12px;border-left:3px solid #a78bfa;background:rgba(167,139,250,.05);margin-bottom:8px;border-radius:0 8px 8px 0;cursor:pointer;transition:.15s}
.kol-item:hover{background:rgba(167,139,250,.1)}
.kol-item.s1{border-color:#60a5fa}.kol-item.s2{border-color:#fbbf24}.kol-item.s3{border-color:#34d399}.kol-item.s4{border-color:#f472b6}
.kol-name{font-weight:700;font-size:14px;margin-bottom:3px}
.kol-meta{font-size:11px;color:#94a3b8}
.sb{display:inline-block;padding:1px 8px;border-radius:10px;font-size:10px;font-weight:700;margin-left:6px}
.sb1{background:#1e3a5f;color:#60a5fa}.sb2{background:#422006;color:#fbbf24}.sb3{background:#064e3b;color:#34d399}.sb4{background:#4a044e;color:#f472b6}
.summary-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:16px}
.summary-card{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:12px}
.summary-card .k{font-size:11px;color:#94a3b8;text-transform:uppercase;margin-bottom:6px}
.summary-card .v{font-size:22px;font-weight:700;color:#e9d5ff}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0}
.chip{display:inline-flex;align-items:center;padding:2px 8px;border-radius:999px;font-size:10px;font-weight:700;line-height:1.4}
.chip-status{background:rgba(129,140,248,.18);color:#c7d2fe}
.chip-stage{background:rgba(167,139,250,.16);color:#ddd6fe}
.chip-positive{background:rgba(16,185,129,.16);color:#86efac}
.chip-mild{background:rgba(251,191,36,.16);color:#fde68a}
.chip-severe{background:rgba(248,113,113,.16);color:#fecaca}
.info-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:6px;margin-top:8px}
.info-box{background:rgba(15,23,42,.35);border:1px solid rgba(255,255,255,.06);border-radius:8px;padding:8px 10px}
.info-box .label{font-size:10px;color:#94a3b8;text-transform:uppercase;margin-bottom:4px}
.info-box .text{font-size:12px;color:#e2e8f0;line-height:1.45;word-break:break-word}
/* processed table */
.tbl{width:100%;border-collapse:collapse;font-size:12px}
.tbl th{text-align:left;padding:8px 10px;background:rgba(255,255,255,.06);color:#94a3b8;font-size:11px;text-transform:uppercase;border-bottom:1px solid rgba(255,255,255,.1)}
.tbl td{padding:8px 10px;border-bottom:1px solid rgba(255,255,255,.05);color:#cbd5e1;vertical-align:top}
.tbl tr:hover td{background:rgba(255,255,255,.03)}
.tbl .tid{font-family:Consolas,monospace;font-size:10px;color:#6366f1;max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tbl .excerpt{color:#64748b;font-size:11px;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* thread detail modal */
.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:100;align-items:center;justify-content:center}
.modal-bg.open{display:flex}
.modal{background:#1e1b4b;border-radius:14px;padding:24px;max-width:700px;width:95%;max-height:80vh;overflow-y:auto;border:1px solid rgba(167,139,250,.3)}
.modal h3{color:#a78bfa;margin-bottom:16px;font-size:16px}
.msg-bubble{padding:10px 14px;border-radius:10px;margin-bottom:10px;font-size:13px;line-height:1.6}
.msg-kol{background:rgba(99,102,241,.15);border-left:3px solid #6366f1}
.msg-our{background:rgba(52,211,153,.1);border-left:3px solid #34d399;margin-left:20px}
.msg-role{font-size:10px;font-weight:700;margin-bottom:4px;text-transform:uppercase;letter-spacing:.5px}
.msg-kol .msg-role{color:#818cf8}.msg-our .msg-role{color:#34d399}
.msg-ts{font-size:10px;color:#475569;margin-top:4px}
.msg-subj{font-size:11px;color:#64748b;margin-bottom:3px}
.close-btn{position:sticky;top:0;float:right;background:#f87171;border:none;color:#fff;border-radius:6px;padding:4px 10px;cursor:pointer;font-weight:700;margin-bottom:8px}
/* log */
.log-item{padding:6px 8px;border-bottom:1px solid rgba(255,255,255,.06);font-family:Consolas,monospace;font-size:12px;display:flex;gap:8px}
.log-ts{color:#475569;min-width:150px;flex-shrink:0}
.log-lv{padding:1px 6px;border-radius:3px;font-size:10px;min-width:52px;text-align:center;flex-shrink:0}
.lv-INFO{background:#1e3a5f;color:#7dd3fc}.lv-WARNING{background:#422006;color:#fbbf24}.lv-ERROR{background:#450a0a;color:#fca5a5}
.log-msg{color:#cbd5e1;flex:1;word-break:break-all}
.empty{text-align:center;color:#475569;padding:24px;font-size:13px}
#api-banner{display:none;margin:0 0 16px;padding:12px 16px;border-radius:8px;border:1px solid #f59e0b;background:rgba(180,83,9,.25);color:#fbbf24;font-size:13px;word-break:break-all}
#api-banner.show{display:block}
#api-banner.ok{border-color:#34d399;background:rgba(6,78,59,.3);color:#6ee7b7}
</style>
</head>
<body>
<div class="wrap">
<div id="api-banner" role="status"></div>
<h1>🤖 KOL Outreach Agent 监控面板</h1>
<div class="controls">
  <button class="btn btn-start" onclick="api('POST','/start-auto','已启动')">▶ 启动轮询</button>
  <button class="btn btn-stop"  onclick="api('POST','/stop-auto','已停止')">⏹ 停止轮询</button>
  <button class="btn btn-check" onclick="api('POST','/check','检查完成').then(()=>{loadKols();loadProcessed();})">📬 立即检查</button>
  <button class="btn btn-rf"    onclick="loadAll()">🔄 刷新</button>
  <button class="btn btn-del"   onclick="clearAll()">🗑️ 清空全部</button>
</div>
<div class="cards">
  <div class="card"><h4>轮询状态</h4><div class="val" id="c-status">-</div></div>
  <div class="card"><h4>轮询间隔</h4><div class="val" id="c-interval">-</div></div>
  <div class="card"><h4>邮箱账户</h4><div class="val" id="c-email" style="font-size:11px">-</div></div>
  <div class="card"><h4>LLM 模型</h4><div class="val" id="c-model" style="font-size:12px">-</div></div>
  <div class="card"><h4>KOL 总数</h4><div class="val" id="c-kols">-</div></div>
  <div class="card"><h4>已处理邮件</h4><div class="val" id="c-proc">-</div></div>
  <div class="card"><h4>待收货</h4><div class="val" id="c-waiting">-</div></div>
  <div class="card"><h4>价值转化中</h4><div class="val" id="c-value">-</div></div>
  <div class="card"><h4>危机处理中</h4><div class="val" id="c-crisis">-</div></div>
</div>

<div class="tabs">
  <div class="tab active" onclick="switchTab('kols')">📊 KOL 会话进度</div>
  <div class="tab" onclick="switchTab('processed')">📋 处理流水</div>
  <div class="tab" onclick="switchTab('logs')">📝 实时日志</div>
</div>

<div id="tab-kols" class="tab-content active">
  <div class="panel">
    <div id="kol-summary" class="summary-grid">
      <div class="summary-card"><div class="k">总线程</div><div class="v">-</div></div>
      <div class="summary-card"><div class="k">正向反馈</div><div class="v">-</div></div>
      <div class="summary-card"><div class="k">轻微不满</div><div class="v">-</div></div>
      <div class="summary-card"><div class="k">强烈不满</div><div class="v">-</div></div>
    </div>
    <div id="kol-list"><div class="empty">加载中...</div></div>
  </div>
</div>

<div id="tab-processed" class="tab-content">
  <div class="panel">
    <div style="overflow-x:auto">
      <table class="tbl">
        <thead><tr>
          <th>处理时间</th>
          <th>KOL</th>
          <th>Thread ID</th>
          <th>邮件 ID</th>
          <th>主题</th>
          <th>内容摘要</th>
        </tr></thead>
        <tbody id="proc-body"><tr><td colspan="6" class="empty">加载中...</td></tr></tbody>
      </table>
    </div>
  </div>
</div>

<div id="tab-logs" class="tab-content">
  <div class="panel"><div id="log-list" style="max-height:520px;overflow-y:auto"><div class="empty">加载中...</div></div></div>
</div>
</div>

<!-- Thread detail modal -->
<div class="modal-bg" id="modal-bg" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <button class="close-btn" onclick="closeModal()">✕ 关闭</button>
    <h3 id="modal-title">对话详情</h3>
    <div id="modal-body"></div>
  </div>
</div>

<script>
const SL={1:'破冰邀请',2:'规则确认',3:'跟进评价',4:'返款确认'};
const SENT_LABEL={positive:'正向反馈',mild_negative:'轻微不满',severe_negative:'强烈不满'};
function esc(s){const d=document.createElement('div');d.textContent=s||'';return d.innerHTML;}
function shortText(v,n){return (v||'').toString().trim().slice(0,n||120);}
function chipCls(sent){
  if(sent==='positive') return 'chip chip-positive';
  if(sent==='mild_negative') return 'chip chip-mild';
  if(sent==='severe_negative') return 'chip chip-severe';
  return 'chip chip-stage';
}
function summarizeExtracted(k){
  const info=k&&typeof k.extracted_info==='object'&&k.extracted_info?k.extracted_info:{};
  const blocks=[];
  const order=info.order_info||{};
  const value=info.value_info||{};
  const refund=info.refund_info||{};
  const recs=Array.isArray(info.recommended_products)?info.recommended_products:[];
  if(recs.length){
    blocks.push({label:'推荐产品',text:recs.map(x=>x.name||x.asin||'').filter(Boolean).join(' / ')});
  }
  if(order.recipient_name||order.product_name||order.address){
    const lines=[order.recipient_name||'',order.product_name||'',shortText(order.address,60)].filter(Boolean);
    blocks.push({label:'订单信息',text:lines.join(' · ')});
  }
  if(value.payment_account||value.review_link||value.review_screenshot_verified){
    const lines=[
      value.payment_method?`${value.payment_method}: ${value.payment_account||'待补充'}`:(value.payment_account||''),
      value.review_link||'',
      value.review_screenshot_verified?'已核实评价截图':''
    ].filter(Boolean);
    blocks.push({label:'价值信息',text:lines.join(' · ')});
  }
  if(refund.refund_account||refund.order_number||refund.issue_summary){
    const lines=[refund.order_number?`订单号 ${refund.order_number}`:'',refund.refund_account||'',refund.issue_summary||''].filter(Boolean);
    blocks.push({label:'退款信息',text:lines.join(' · ')});
  }
  if(info.intent_reasoning&&!blocks.length){
    blocks.push({label:'识别备注',text:shortText(info.intent_reasoning,100)});
  }
  return blocks;
}
function renderSummaryStats(kols){
  const sOf=k=>k.sentiment||((k.extracted_info&&k.extracted_info.sentiment)||'');
  const total=kols.length;
  const pos=kols.filter(k=>k.status==='满意待收款信息'||k.status==='已生成标准工单'||sOf(k)==='positive').length;
  const mild=kols.filter(k=>k.status==='返款安抚中'||sOf(k)==='mild_negative').length;
  const severe=kols.filter(k=>k.status==='危机退款处理中'||k.status==='已生成危机工单'||sOf(k)==='severe_negative').length;
  document.getElementById('kol-summary').innerHTML=[
    {k:'总线程',v:total},
    {k:'正向反馈',v:pos},
    {k:'轻微不满',v:mild},
    {k:'强烈不满',v:severe},
  ].map(x=>`<div class="summary-card"><div class="k">${esc(x.k)}</div><div class="v">${esc(String(x.v))}</div></div>`).join('');
  document.getElementById('c-waiting').textContent=String(kols.filter(k=>k.status==='待收货').length);
  document.getElementById('c-value').textContent=String(kols.filter(k=>k.status==='满意待收款信息'||k.status==='已生成标准工单').length);
  document.getElementById('c-crisis').textContent=String(kols.filter(k=>k.status==='危机退款处理中'||k.status==='已生成危机工单').length);
}
function showBanner(text,isOk){
  const b=document.getElementById('api-banner');
  b.textContent=text;
  b.className='show'+(isOk?' ok':'');
}
function clearBannerIfOk(){
  const b=document.getElementById('api-banner');
  b.className='';
  b.textContent='';
}
/** 带状态检查与 body 采样的安全 JSON 解析，避免 500 返回 HTML 时整页卡死 */
async function fetchJson(url,opts){
  const r=await fetch(url,opts);
  const ct=(r.headers.get('content-type')||'');
  if(!r.ok){
    const t=await r.text();
    const hint=t.slice(0,120).replace(/\\s+/g,' ');
    throw new Error('HTTP '+r.status+': '+(hint||'无内容'));
  }
  if(!ct.includes('json')){
    const t=await r.text();
    throw new Error('非 JSON 响应: '+(t.slice(0,80)));
  }
  return r.json();
}
function switchTab(t){
  document.querySelectorAll('.tab').forEach((el,i)=>{
    const names=['kols','processed','logs'];
    el.classList.toggle('active',names[i]===t);
  });
  document.querySelectorAll('.tab-content').forEach(el=>el.classList.remove('active'));
  document.getElementById('tab-'+t).classList.add('active');
}
async function api(m,u,msg){
  try{
    const d=await fetchJson(u,{method:m});
    if(msg)alert(msg+(d&&d.processed!=null?`\\n处理 ${d.processed} 封，成功 ${d.success} 封`:''));
    await loadStatus();
    return d;
  }catch(e){
    showBanner('API 错误 ('+u+'): '+e.message,false);
    alert('操作失败: '+e.message);
  }
}
async function loadStatus(){
  try{
    const d=await fetchJson('/status');
    document.getElementById('c-status').textContent=d.auto_polling?'运行中':'已停止';
    document.getElementById('c-status').className='val '+(d.auto_polling?'on':'off');
    document.getElementById('c-interval').textContent=(d.poll_interval_seconds!=null?d.poll_interval_seconds:'-')+'秒';
    document.getElementById('c-email').textContent=d.email_account||'-';
    document.getElementById('c-model').textContent=d.llm_model||'-';
  }catch(e){
    showBanner('无法连接 /status: '+e.message+' — 请确认本机已用 uvicorn 启动服务 (例: 访问同一地址的 /status) ',false);
    document.getElementById('c-status').textContent='错误';
    return false;
  }
  return true;
}
async function loadKols(){
  const el=document.getElementById('kol-list');
  try{
    const d=await fetchJson('/kols');
    if(!d||!Array.isArray(d.kols)){el.innerHTML='<div class="empty">数据格式异常</div>';return;}
    document.getElementById('c-kols').textContent=String(d.count??d.kols.length);
    renderSummaryStats(d.kols);
    if(!d.kols.length){el.innerHTML='<div class="empty">暂无 KOL 记录</div>';return;}
    el.innerHTML=d.kols.map(k=>{
      const st=Number(k.current_stage||1);
      const uat=(k.updated_at||'').toString();
      const status=k.status||'未知状态';
      const infoBlocks=summarizeExtracted(k);
      const sent=k.sentiment||((k.extracted_info&&k.extracted_info.sentiment)||'');
      const sentimentChip=sent?`<span class="${chipCls(sent)}">${esc(SENT_LABEL[sent]||sent)}</span>`:'';
      const infoHtml=infoBlocks.length?`<div class="info-list">${infoBlocks.map(b=>`<div class="info-box"><div class="label">${esc(b.label)}</div><div class="text">${esc(b.text)}</div></div>`).join('')}</div>`:'';
      return `<div class="kol-item s${st}" style="display:flex;align-items:flex-start;gap:10px">
    <div style="flex:1;cursor:pointer" onclick="openThread(${JSON.stringify(k.thread_id)},${JSON.stringify(k.kol_name||k.kol_email)})">
      <div class="kol-name">${esc(k.kol_name||k.kol_email)}<span class="sb sb${st}">阶段${st} ${SL[st]||''}</span></div>
      <div class="kol-meta">${esc(k.kol_email)} · 更新: ${esc(uat.slice(0,16))}</div>
      <div class="chips">
        <span class="chip chip-status">${esc(status)}</span>
        <span class="chip chip-stage">${esc('DB阶段 ' + st)}</span>
        ${sentimentChip}
      </div>
      ${k.notes?`<div class="kol-meta" style="margin-top:3px;color:#6b7280">${esc(String(k.notes).slice(0,100))}</div>`:''}
      ${infoHtml}
    </div>
    <button class="btn-del-sm" type="button" onclick="event.stopPropagation();deleteThread(${JSON.stringify(k.thread_id)},${JSON.stringify(k.kol_name||k.kol_email)})">🗑️ 删除</button>
  </div>`;
    }).join('');
    return true;
  }catch(e){
    el.innerHTML='<div class="empty">加载失败: '+esc(e.message)+'</div>';
    showBanner('GET /kols 失败: '+e.message,false);
    return false;
  }
}
async function loadProcessed(){
  const tb=document.getElementById('proc-body');
  try{
    const d=await fetchJson('/processed?limit=100');
    if(!d||!Array.isArray(d.records)){tb.innerHTML='<tr><td colspan="6" class="empty">数据格式异常</td></tr>';return;}
    document.getElementById('c-proc').textContent=String(d.count??d.records.length);
    if(!d.records.length){tb.innerHTML='<tr><td colspan="6" class="empty">暂无处理记录</td></tr>';return;}
    tb.innerHTML=d.records.map(r=>`<tr>
    <td style="white-space:nowrap;font-size:11px">${(r.processed_at||'').toString().slice(0,16)}</td>
    <td><div style="font-weight:600;font-size:13px">${esc(r.kol_name||r.kol_email||'-')}</div><div style="font-size:10px;color:#64748b">${esc(r.kol_email||'')}</div></td>
    <td class="tid" title="${esc(r.thread_id)}">${esc(r.thread_id||'')}</td>
    <td class="tid" title="${esc(r.message_id)}">${esc((r.message_id||'').toString().slice(0,24))}</td>
    <td style="font-size:12px;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.subject||'-')}</td>
    <td class="excerpt" title="${esc(r.body_excerpt||'')}">${esc((r.body_excerpt||'').toString().slice(0,80))}</td>
  </tr>`).join('');
    return true;
  }catch(e){
    tb.innerHTML='<tr><td colspan="6" class="empty">加载失败: '+esc(e.message)+'</td></tr>';
    showBanner('GET /processed 失败: '+e.message,false);
    return false;
  }
}
async function loadLogs(){
  const el=document.getElementById('log-list');
  try{
    const d=await fetchJson('/logs?tail=80');
    if(!d||!Array.isArray(d.logs)){el.innerHTML='<div class="empty">数据格式异常</div>';return true;}
    if(!d.logs.length){el.innerHTML='<div class="empty">暂无日志</div>';return true;}
    el.innerHTML=[...d.logs].reverse().map(l=>`<div class="log-item">
    <span class="log-ts">${esc(l.timestamp||'')}</span>
    <span class="log-lv lv-${esc(l.level||'INFO')}">${esc(l.level||'')}</span>
    <span class="log-msg">${esc(l.message||'')}</span>
  </div>`).join('');
    return true;
  }catch(e){
    el.innerHTML='<div class="empty">加载失败: '+esc(e.message)+'</div>';
    return false;
  }
}
async function openThread(threadId, name){
  document.getElementById('modal-title').textContent='对话详情 — '+(name||'');
  document.getElementById('modal-body').innerHTML='<div class="empty">加载中...</div>';
  document.getElementById('modal-bg').classList.add('open');
  try{
    const d=await fetchJson('/thread/'+encodeURIComponent(threadId)+'?limit=20');
    if(!d||!Array.isArray(d.messages)||!d.messages.length){
      document.getElementById('modal-body').innerHTML='<div class="empty">暂无对话记录</div>';return;
    }
    document.getElementById('modal-body').innerHTML=d.messages.map(m=>`
      <div class="msg-bubble msg-${m.role==='our'?'our':'kol'}">
        <div class="msg-role">${m.role==='our'?'我方回复':'KOL 来信'}</div>
        ${m.subject?`<div class="msg-subj">主题: ${esc(m.subject)}</div>`:''}
        <div>${esc(m.body||'').replace(/\\n/g,'<br>')}</div>
        <div class="msg-ts">${(m.created_at||'').toString().slice(0,16)}</div>
      </div>`).join('');
  }catch(e){
    document.getElementById('modal-body').innerHTML='<div class="empty">加载失败: '+esc(e.message)+'</div>';
  }
}
function closeModal(){document.getElementById('modal-bg').classList.remove('open');}
async function deleteThread(threadId, name){
  if(!confirm('确认删除「' + name + '」的全部对话历史？\\n业务表与 LangGraph checkpoints 都会一并删除。')) return;
  try{
    const d=await fetchJson('/thread/'+encodeURIComponent(threadId),{method:'DELETE'});
    alert('已删除，共清除 '+(d.deleted_rows??0)+' 条记录');
    await loadKols(); await loadProcessed();
  }catch(e){
    showBanner('删除失败: '+e.message,false);
    alert('删除失败: '+e.message);
  }
}
async function clearAll(){
  if(!confirm('⚠️ 确认清空全部数据？\\n所有 KOL 会话、处理记录、对话历史以及 LangGraph checkpoints 都会被彻底删除，此操作不可撤销！')) return;
  try{
    const d=await fetchJson('/all-data',{method:'DELETE'});
    alert('已清空全部数据\\n thread_messages: '+(d.thread_messages??0)+' 条\\n processed_messages: '+(d.processed_messages??0)+' 条\\n kol_threads: '+(d.kol_threads??0)+' 条\\n checkpoints: '+(d.checkpoints??0)+' 条\\n checkpoint_writes: '+(d.checkpoint_writes??0)+' 条');
    await loadKols(); await loadProcessed();
  }catch(e){
    showBanner('清空失败: '+e.message,false);
    alert('清空失败: '+e.message);
  }
}
async function loadAll(){
  const a=await loadStatus();
  const b=await loadKols();
  const c=await loadProcessed();
  const d=await loadLogs();
  if(a&&b&&c&&d) clearBannerIfOk();
}
loadAll();
setInterval(()=>{loadStatus().catch(()=>{});},5000);
setInterval(()=>{loadLogs().catch(()=>{});},4000);
setInterval(()=>{loadKols().catch(()=>{});loadProcessed().catch(()=>{});},15000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, reload=False)
