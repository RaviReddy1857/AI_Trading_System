PAGE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trading Research System</title>
<style>
 :root{--bg:#f6f7f9;--card:#fff;--ink:#16202a;--mute:#5b6875;--line:#dde2e8;--ok:#0a7a45;--bad:#b3261e;--warn:#9a6200}
 @media (prefers-color-scheme:dark){:root{--bg:#11161c;--card:#1a212a;--ink:#e8edf2;--mute:#9aa7b4;--line:#2c3743;--ok:#4cc38a;--bad:#ff8a80;--warn:#f0b849}}
 body{margin:0;font:15px/1.45 system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
 header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:8px 20px;align-items:baseline}
 h1{font-size:18px;margin:0} h2{font-size:14px;margin:0 0 8px;color:var(--mute);font-weight:600}
 .tag{font-size:12px;border:1px solid var(--line);border-radius:4px;padding:2px 8px;color:var(--mute)}
 .warn{color:var(--warn);border-color:var(--warn)}
 main{padding:16px 20px;display:grid;gap:16px;max-width:1000px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px;overflow-x:auto}
 table{border-collapse:collapse;width:100%} th,td{padding:6px 10px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
 th{font-size:12px;color:var(--mute);font-weight:600} th:first-child,td:first-child{text-align:left}
 .LIVE{color:var(--ok)} .STALE,.NO_DATA{color:var(--bad)} .CLOSED{color:var(--mute)}
 .up{color:var(--ok)} .down{color:var(--bad)} .kv{display:flex;flex-wrap:wrap;gap:6px 24px} .kv b{font-weight:600}
 .badges{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:8px}
 .badge{font-weight:700;font-size:16px;border-radius:6px;padding:6px 12px;border:2px solid currentColor}
 .TRENDING_UP,.BULLISH{color:var(--ok)} .TRENDING_DOWN,.BEARISH{color:var(--bad)} .VOLATILE,.EVENT{color:var(--warn)}
 .RANGE,.NEUTRAL{color:var(--ink)} .UNKNOWN{color:var(--mute)}
 ul{margin:4px 0 0;padding-left:20px} li{margin:2px 0} .mute{color:var(--mute);font-size:13px}
 textarea{width:100%;box-sizing:border-box;min-height:90px;font:13px ui-monospace,Consolas,monospace;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:6px}
 select,button{font:inherit;padding:4px 10px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--ink)}
 details summary{cursor:pointer;color:var(--mute);font-size:13px;font-weight:600}
</style></head><body>
<header><h1>Trading Research System</h1>
 <span class="tag">RESEARCH ONLY: this app cannot place orders</span>
 <a class="tag" href="/backtests">Backtest reports</a>
 <a class="tag" href="/options">Options view</a>
 <a class="tag" href="/stock-option-selling">Stock option selling</a>
 <span class="tag" id="feed"></span><span class="tag" id="asof"></span></header>
<a href="/data-status" id="dstrip" style="display:block;padding:8px 20px;border-bottom:1px solid var(--line);color:var(--ink);text-decoration:none;font-size:14px">Data status: loading...</a>
<main>
 <div class="card" id="regime-card"><h2>Today's market type</h2>
  <div class="badges"><span class="badge UNKNOWN" id="regime">UNKNOWN</span>
   <span class="badge UNKNOWN" id="bias">FII bias: UNKNOWN</span>
   <span class="mute" id="rmeta"></span></div>
  <ul id="rreasons"></ul><ul id="breasons"></ul><div class="mute" id="rwarn"></div>
  <details style="margin-top:8px"><summary>Regime history</summary>
   <table style="margin-top:6px"><thead><tr><th>Time (IST)</th><th>Session</th><th>Regime</th><th>FII bias</th><th>Basis</th><th>Rules</th></tr></thead>
   <tbody id="rhist"></tbody></table></details>
  <details style="margin-top:8px"><summary>Paste FII/DII figures, events or holidays (CSV)</summary>
   <p class="mute">FII/DII: <code>date,fii_net_cr,dii_net_cr</code> or the NSE table (Category, Date, Buy, Sell, Net). New days are added.<br>
   Events: <code>date,type,note,symbol</code>, type one of EXPIRY, RBI_POLICY, BUDGET, RESULTS, MARKET_EVENT. Replaces the list.<br>
   Holidays: <code>date,note</code>. Replaces the list.</p>
   <select id="ikind"><option value="fii_dii">FII/DII</option><option value="events">Events</option><option value="holidays">Holidays</option></select>
   <textarea id="itext" placeholder="paste CSV here, including the header row"></textarea>
   <button id="ibtn" type="button">Import</button> <span class="mute" id="iresult"></span></details>
 </div>
 <div class="card"><h2>Live prices</h2>
  <table><thead><tr><th>Symbol</th><th>Last</th><th>Change %</th><th>Bid</th><th>Ask</th><th>Age (s)</th><th>Status</th></tr></thead>
  <tbody id="rows"></tbody></table></div>
 <div class="card"><h2>Account settings (from config.yaml)</h2><div class="kv" id="cfg"></div></div>
 <div class="card"><h2>Data quality</h2><div class="kv" id="qstats"></div>
  <table style="margin-top:8px"><thead><tr><th>Time (UTC)</th><th>Symbol</th><th>Reason</th><th>Last</th></tr></thead>
  <tbody id="qrows"></tbody></table></div>
</main>
<script>
const f=(v,d=2)=>v==null?"n/a":Number(v).toLocaleString("en-IN",{minimumFractionDigits:d,maximumFractionDigits:d});
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
async function j(u){const r=await fetch(u);return r.json()}
async function tick(){
 try{
  const p=await j("/api/prices");
  const feed=document.getElementById("feed");
  feed.textContent=p.feed==="sim"?"SIMULATED PRICES, not live data":"Feed: ICICI Breeze";
  feed.className="tag"+(p.feed==="sim"?" warn":"");
  document.getElementById("asof").textContent="As of "+new Date(p.as_of_utc).toLocaleTimeString();
  document.getElementById("rows").innerHTML=p.rows.map(r=>`<tr><td>${esc(r.symbol)}</td><td>${f(r.last)}</td>
   <td class="${r.change_pct>0?"up":r.change_pct<0?"down":""}">${f(r.change_pct)}</td>
   <td>${f(r.bid)}</td><td>${f(r.ask)}</td><td>${f(r.age_seconds,1)}</td><td class="${r.status}">${r.status}</td></tr>`).join("");
  const q=await j("/api/quality");
  document.getElementById("qstats").innerHTML=`<span>Good ticks: <b>${q.good_ticks}</b></span><span>Quarantined: <b>${q.quarantined_ticks}</b></span>`;
  document.getElementById("qrows").innerHTML=q.recent_events.slice(0,8).map(e=>`<tr><td>${esc(e.at_utc.slice(11,19))}</td><td>${esc(e.symbol)}</td><td>${esc(e.reason)}</td><td>${f(e.last)}</td></tr>`).join("")||"<tr><td colspan=4>No quarantined ticks</td></tr>";
 }catch(e){document.getElementById("asof").textContent="Server not reachable"}
}
async function cfg(){const c=await j("/api/config");const l=c.limits_inr;
 document.getElementById("cfg").innerHTML=`<span>Capital: <b>&#8377;${f(c.capital_inr,0)}</b></span><span>Mode: <b>${esc(c.account_mode)}</b></span>
 <span>Max loss per trade: <b>&#8377;${f(l.max_loss_per_trade,0)}</b></span><span>Per option position: <b>&#8377;${f(l.max_loss_per_option,0)}</b></span>
 <span>Daily stop: <b>&#8377;${f(l.daily_loss_stop,0)}</b></span><span>Monthly pause: <b>&#8377;${f(l.monthly_loss_pause,0)}</b></span>
 <span>Cash kept free: <b>&#8377;${f(l.uncommitted_cash,0)}</b></span>`}
const ist=s=>new Date(s).toLocaleString("en-IN",{timeZone:"Asia/Kolkata",hour12:false,day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit"});
async function regime(){
 try{
  const r=await j("/api/regime");
  const rg=document.getElementById("regime");rg.textContent=r.regime.replace("_"," ");rg.className="badge "+r.regime;
  const b=document.getElementById("bias");b.textContent="FII bias: "+r.bias;b.className="badge "+r.bias;
  document.getElementById("rmeta").textContent=`Session ${r.session_date} | basis ${r.basis}${r.basis_date&&r.basis!=="LIVE"?" "+r.basis_date:""} | ${r.rules_version} | updated ${ist(r.as_of_utc)}${r.holiday?" | Holiday: "+r.holiday:""}`;
  document.getElementById("rreasons").innerHTML=r.reasons.map(x=>`<li>${esc(x)}</li>`).join("");
  document.getElementById("breasons").innerHTML=r.bias_reasons.map(x=>`<li class="mute">${esc(x)}</li>`).join("");
  const w=Object.entries(r.data_warnings||{});
  document.getElementById("rwarn").innerHTML=w.map(([k,v])=>`<div class="down">${esc(k)}.csv not loaded: ${esc(v.join("; "))}</div>`).join("");
  document.getElementById("rhist").innerHTML=r.history.map(h=>`<tr><td>${ist(h.at_utc)}</td><td>${esc(h.session_date)}</td><td class="${h.regime}">${h.regime}</td><td class="${h.bias}">${h.bias}</td><td>${esc(h.basis)}</td><td>${esc(h.rules_version)}</td></tr>`).join("");
 }catch(e){document.getElementById("rmeta").textContent="Regime not available"}
}
document.getElementById("ibtn").onclick=async()=>{
 const kind=document.getElementById("ikind").value,out=document.getElementById("iresult");
 try{const r=await fetch("/data/import/"+kind,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text:document.getElementById("itext").value})});
  const x=await r.json();out.textContent=x.ok?`Saved ${x.rows_saved} row(s)`:("Not saved: "+(x.errors||[x.detail]).join("; "));
  out.className=x.ok?"up":"down";if(x.ok)regime();
 }catch(e){out.textContent="Import failed";out.className="down"}
};
async function dstatus(){
 const el=document.getElementById("dstrip");
 try{const s=await j("/api/data-status");const c=s.counts;
  const bad=s.rows.filter(r=>r.colour==="red"||r.colour==="amber").map(r=>r.source);
  el.innerHTML=`Data status: <b class="up">${c.green} OK</b> &middot; <b style="color:var(--warn)">${c.amber} need attention</b> &middot; <b class="down">${c.red} missing or blocking</b>`+(bad.length?` <span class="mute">(${esc(bad.join(", "))})</span>`:"")+` <span class="mute">&rarr; details</span>`;
 }catch(e){el.textContent="Data status: not available"}
}
cfg();tick();regime();dstatus();setInterval(tick,2000);setInterval(regime,15000);setInterval(dstatus,60000);
</script></body></html>
"""

DATA_STATUS_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Data status</title>
<style>
 :root{--bg:#f6f7f9;--card:#fff;--ink:#16202a;--mute:#5b6875;--line:#dde2e8;--ok:#0a7a45;--bad:#b3261e;--warn:#9a6200}
 @media (prefers-color-scheme:dark){:root{--bg:#11161c;--card:#1a212a;--ink:#e8edf2;--mute:#9aa7b4;--line:#2c3743;--ok:#4cc38a;--bad:#ff8a80;--warn:#f0b849}}
 body{margin:0;font:15px/1.45 system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
 header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:8px 20px;align-items:baseline}
 h1{font-size:18px;margin:0} h2{font-size:14px;margin:0 0 8px;color:var(--mute);font-weight:600}
 .tag{font-size:12px;border:1px solid var(--line);border-radius:4px;padding:2px 8px;color:var(--mute)}
 main{padding:16px 20px;display:grid;gap:16px;max-width:1200px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px;overflow-x:auto}
 table{border-collapse:collapse;width:100%} th,td{padding:6px 10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
 th{font-size:12px;color:var(--mute);font-weight:600}
 .green{color:var(--ok);font-weight:700} .amber{color:var(--warn);font-weight:700} .red{color:var(--bad);font-weight:700} .grey{color:var(--mute)}
 .mute{color:var(--mute);font-size:13px} ul{margin:4px 0 0;padding-left:20px} li{margin:3px 0}
 input,button{font:inherit;padding:4px 10px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--ink)}
</style></head><body>
<header><h1>Data status</h1><span class="tag">RESEARCH ONLY: this app cannot place orders</span>
 <a class="tag" href="/">Back to prices</a> <span class="tag" id="asof"></span></header>
<main>
 <div class="card"><h2 id="summary">Loading...</h2>
  <table><thead><tr><th>Source</th><th>Last updated</th><th>Coverage</th><th>Status</th><th>Blocks</th><th>What you need to do</th></tr></thead>
  <tbody id="rows"></tbody></table>
  <div class="mute" id="note"></div></div>
 <div class="card"><h2>Import</h2>
  <p class="mute">Put the files you downloaded in <code>data/inbox/</code> and press the button (or run <code>python -m app.inbox</code>).
  Processed files move to <code>data/inbox/done/</code>; rejected ones to <code>data/inbox/rejected/</code> with a reason file.</p>
  <div class="mute">Board meetings export (needed only for a board-meetings file; it is rejected without them):
  <label>from <input id="bfrom" size="11" placeholder="YYYY-MM-DD"></label>
  <label>through <input id="through" size="11" placeholder="YYYY-MM-DD"></label>
  <label><input id="ball" type="checkbox"> I confirm it is the unfiltered all-companies export (no symbol, index or purpose filter)</label></div>
  <button id="inbox" type="button">Import inbox files</button> <span class="mute" id="inboxres"></span>
  <h2 style="margin-top:14px">RBI decision</h2>
  <label class="mute">Date <input id="rdate" size="11" placeholder="YYYY-MM-DD"></label>
  <label class="mute">Decision <input id="rdec" size="40" placeholder="repo unchanged at 5.25%  (or: scheduled)"></label>
  <button id="rbtn" type="button">Save</button> <span class="mute" id="rres"></span></div>
 <div class="card"><h2>How to update</h2><div id="how"></div></div>
</main>
<script>
const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const label={green:"OK",amber:"needs attention",red:"missing / blocking",grey:"info"};
async function load(){
 const s=await (await fetch("/api/data-status")).json();
 document.getElementById("asof").textContent="As of "+s.as_of_ist+" IST";
 const c=s.counts;
 document.getElementById("summary").innerHTML=`<span class="green">${c.green} OK</span> &middot; <span class="amber">${c.amber} need attention</span> &middot; <span class="red">${c.red} missing or blocking</span> <span class="mute">(last completed session ${esc(s.last_session_ist)}; times in IST)</span>`;
 document.getElementById("rows").innerHTML=s.rows.map(r=>{
  const d=r.detail||{};const extra=[];
  if(d.stocks_without_coverage&&d.stocks_without_coverage.length)extra.push("No coverage through the next expiry: "+d.stocks_without_coverage.join(", "));
  if(d.coverage_older_than_limit&&d.coverage_older_than_limit.length)extra.push("Coverage older than the limit: "+d.coverage_older_than_limit.join(", "));
  if(d.behind&&d.behind.length)extra.push("Behind: "+d.behind.map(x=>x+" ("+(d.last_date_per_stock[x]||"none")+")").join(", "));
  if(d.commit)extra.push("Commit "+d.commit);
  return `<tr><td>${esc(r.source)}</td><td>${esc(r.last_updated||"never")}</td><td>${esc(r.coverage)}${extra.map(x=>`<div class="mute">${esc(x)}</div>`).join("")}</td>
  <td class="${r.colour}">${esc(r.status)}<div class="mute">${label[r.colour]}</div></td><td>${esc(r.blocks)}</td><td>${esc(r.what_to_do)}</td></tr>`}).join("");
 document.getElementById("note").textContent=s.note;
 document.getElementById("how").innerHTML=Object.entries(s.how_to_update).map(([k,v])=>`<b>${esc(k[0].toUpperCase()+k.slice(1))}</b><ul>${v.map(x=>`<li>${esc(x)}</li>`).join("")}</ul>`).join("");
}
async function post(u,b){const r=await fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)});return r.json()}
document.getElementById("inbox").onclick=async()=>{const o=document.getElementById("inboxres");o.textContent="importing...";
 try{const x=await post("/data/import/inbox",{board_from:document.getElementById("bfrom").value,board_through:document.getElementById("through").value,board_all_companies:document.getElementById("ball").checked});
  o.textContent=x.files&&x.files.length?x.files.map(f=>f.file+": "+(f.ok===false?"REJECTED - "+f.errors.join("; "):(f.kind+", "+(f.added||0)+" added, "+(f.duplicates||0)+" duplicates"))).join(" | "):"No files in data/inbox/";load()}
 catch(e){o.textContent="Import failed"}};
document.getElementById("rbtn").onclick=async()=>{const o=document.getElementById("rres");
 try{const x=await post("/data/import/rbi-decision",{date:document.getElementById("rdate").value,decision:document.getElementById("rdec").value});
  o.textContent=x.ok?(x.added?"Saved":"Already there"):"Not saved: "+(x.errors||[x.detail]).join("; ");load()}
 catch(e){o.textContent="Save failed"}};
load();setInterval(load,60000);
</script></body></html>
"""


BACKTEST_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backtest Reports</title>
<style>
 :root{--bg:#f6f7f9;--card:#fff;--ink:#16202a;--mute:#5b6875;--line:#dde2e8;--ok:#0a7a45;--bad:#b3261e;--warn:#9a6200}
 @media (prefers-color-scheme:dark){:root{--bg:#11161c;--card:#1a212a;--ink:#e8edf2;--mute:#9aa7b4;--line:#2c3743;--ok:#4cc38a;--bad:#ff8a80;--warn:#f0b849}}
 body{margin:0;font:15px/1.45 system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
 header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:8px 20px;align-items:baseline}
 h1{font-size:18px;margin:0} h2{font-size:14px;margin:0 0 8px;color:var(--mute);font-weight:600}
 .tag{font-size:12px;border:1px solid var(--line);border-radius:4px;padding:2px 8px;color:var(--mute)}
 .warn{color:var(--warn);border-color:var(--warn)}
 main{padding:16px 20px;display:grid;gap:16px;max-width:1000px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px;overflow-x:auto}
 table{border-collapse:collapse;width:100%} th,td{padding:6px 10px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
 th{font-size:12px;color:var(--mute);font-weight:600} th:first-child,td:first-child{text-align:left}
 .LIVE{color:var(--ok)} .STALE,.NO_DATA{color:var(--bad)} .CLOSED{color:var(--mute)}
 .up{color:var(--ok)} .down{color:var(--bad)} .kv{display:flex;flex-wrap:wrap;gap:6px 24px} .kv b{font-weight:600}
 .badges{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:8px}
 .badge{font-weight:700;font-size:16px;border-radius:6px;padding:6px 12px;border:2px solid currentColor}
 .TRENDING_UP,.BULLISH{color:var(--ok)} .TRENDING_DOWN,.BEARISH{color:var(--bad)} .VOLATILE,.EVENT{color:var(--warn)}
 .RANGE,.NEUTRAL{color:var(--ink)} .UNKNOWN{color:var(--mute)}
 ul{margin:4px 0 0;padding-left:20px} li{margin:2px 0} .mute{color:var(--mute);font-size:13px}
 textarea{width:100%;box-sizing:border-box;min-height:90px;font:13px ui-monospace,Consolas,monospace;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:6px}
 select,button{font:inherit;padding:4px 10px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--ink)}
 details summary{cursor:pointer;color:var(--mute);font-size:13px;font-weight:600}
</style></head><body>
<header><h1>Backtest reports</h1>
 <span class="tag">RESEARCH ONLY: this app cannot place orders</span>
 <a class="tag" href="/">Back to prices</a> <a class="tag" href="/options">Options view</a></header>
<main>
 <div class="card"><h2>Latest run per strategy</h2>
  <div class="mute">Net results are after brokerage, taxes, charges and slippage. Entries fill at the next bar's open.
  Results with fewer than 30 trades are marked LOW SAMPLE. Run <code>python -m app.backtest</code> to refresh.</div>
  <table style="margin-top:8px"><thead><tr><th>Strategy</th><th>Period</th><th>Trades</th><th>Win %</th><th>Avg win</th>
  <th>Avg loss</th><th>Net P&amp;L (Rs)</th><th>Costs (Rs)</th><th>Max DD (Rs)</th><th>PF</th><th>Sample</th></tr></thead>
  <tbody id="runs"><tr><td colspan="11" class="mute">loading...</td></tr></tbody></table></div>
 <div id="detail"></div>
</main>
<script>
const fmt=(x,d=2)=>x===null||x===undefined?'-':Number(x).toLocaleString('en-IN',{minimumFractionDigits:d,maximumFractionDigits:d});
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function row(name,o,period){return `<tr><td>${name}</td><td>${esc(period)}</td><td>${o.trades}</td><td>${fmt(o.win_rate_pct,1)}</td>
 <td>${fmt(o.avg_win_inr)}</td><td>${fmt(o.avg_loss_inr)}</td><td class="${o.net_pnl_inr>0?'up':(o.net_pnl_inr<0?'down':'')}">${fmt(o.net_pnl_inr)}</td>
 <td>${fmt(o.costs_inr)}</td><td>${fmt(o.max_drawdown_inr)}</td><td>${fmt(o.profit_factor)}</td>
 <td class="${o.low_sample?'STALE':''}">${o.low_sample?'LOW SAMPLE':'ok'}</td></tr>`}
async function show(id){
 const r=await (await fetch('/api/backtests/'+encodeURIComponent(id))).json(); const rep=r.report;
 const reg=Object.entries(rep.by_regime).map(([k,o])=>row(`<span class="${k}">${k}</span>`,o,'')).join('')||'<tr><td colspan="11" class="mute">no trades</td></tr>';
 const wf=rep.walk_forward.windows.map(w=>row(w.from+' .. '+w.to,w,w.sessions+' sessions')).join('');
 const h=rep.holdout; const hold=h.evaluated?row('holdout',h.result,h.from+' .. '+h.to):`<tr><td colspan="11" class="mute">${esc(h.from)} .. ${esc(h.to)}: ${esc(h.result)}</td></tr>`;
 document.getElementById('detail').innerHTML=`<div class="card"><h2>${esc(rep.strategy_id)} ${esc(rep.strategy_version)}</h2>
 <div class="badges">${stat(rep)}<span class="tag warn">${esc(rep.cost_label)}</span><span class="tag">${esc(rep.costs.plan)}</span>
 <span class="tag">source ${esc(rep.data_source)}</span>${rep.kind==='option'?`<span class="tag ${rep.payoff_simulation?'warn':''}">${rep.payoff_simulation?'PAYOFF SIMULATION':'real option prices'}</span>`:''}${rep.data_note?`<span class="tag warn">${esc(rep.data_note)}</span>`:''}</div>
 <div class="mute">${esc(rep.overall.sample_note)}.${rep.exposure?` Peak exposure: ${rep.exposure.max_open_trades} trades open, Rs ${fmt(rep.exposure.max_position_value_inr,0)} (${rep.exposure.max_position_value_pct_of_capital}% of capital).`:''} Regimes: ${esc(rep.allowed_regimes.join(', '))}. Skipped signals: ${esc(JSON.stringify(rep.skipped_signals))}${rep.data_exclusions&&rep.horizon==='INTRADAY'?` Data exclusions: ${rep.data_exclusions.sessions_incomplete} incomplete and ${rep.data_exclusions.sessions_without_bars} empty symbol-sessions not traded; trades dropped ${rep.data_exclusions.trades_dropped_missing_bar} (missing bar), ${rep.data_exclusions.trades_dropped_session_ended} (session ended early).`:''}${rep.data_exclusions&&rep.horizon!=='INTRADAY'&&rep.data_exclusions.trades_dropped_missing_daily_bar!==undefined?` Data exclusions: ${rep.data_exclusions.trades_dropped_missing_daily_bar} trade(s) dropped because the stock had no daily bar for a session while the trade was open.`:''}</div>
${optionBlock(rep)}
 <h2 style="margin-top:12px">By regime</h2><table><thead><tr><th>Regime</th><th></th><th>Trades</th><th>Win %</th><th>Avg win</th><th>Avg loss</th><th>Net</th><th>Costs</th><th>Max DD</th><th>PF</th><th>Sample</th></tr></thead><tbody>${reg}</tbody></table>
 <h2 style="margin-top:12px">Walk-forward windows (fixed rules)</h2><table><thead><tr><th>Window</th><th></th><th>Trades</th><th>Win %</th><th>Avg win</th><th>Avg loss</th><th>Net</th><th>Costs</th><th>Max DD</th><th>PF</th><th>Sample</th></tr></thead><tbody>${wf}</tbody></table>
 <h2 style="margin-top:12px">Holdout</h2><table><tbody>${hold}</tbody></table>
 <ul>${rep.notes.map(n=>`<li class="mute">${esc(n)}</li>`).join('')}</ul></div>`;
}
function optionBlock(rep){
 if(rep.kind!=='option') return '';
 const b=rep.baseline, c=rep.comparison;
 const variants=Object.entries(b.by_variant||{}).map(([k,o])=>row(esc(k),o,'')).join('');
 const unders=Object.entries(rep.by_underlying||{}).map(([k,o])=>row('strategy on '+esc(k),o,'')).join('');
 return `<div class="mute" style="margin-top:8px">Option prices: <b>${esc(rep.price_source_label)}</b>. Underlyings: ${esc((rep.underlyings||[]).join(', '))}. One lot per trade at today's lot size ${esc(JSON.stringify(rep.lot_sizes))}.${rep.eligibility_label?` <span class="tag warn">${esc(rep.eligibility_label)}</span>`:''}</div>
 <h2 style="margin-top:12px">Strategy against its no-filter baseline (same structure on a fixed schedule, no entry rules)</h2>
 <div class="badges"><span class="badge ${c.verdict.startsWith('beats')?'TRENDING_UP':'VOLATILE'}">${esc(c.verdict.toUpperCase())}</span>
 <span class="mute">Per trade after costs: strategy Rs ${fmt(c.strategy_avg_net_per_trade_inr)}, baseline Rs ${fmt(c.baseline_avg_net_per_trade_inr)}</span></div>
 <table><thead><tr><th></th><th></th><th>Trades</th><th>Win %</th><th>Avg win</th><th>Avg loss</th><th>Net</th><th>Costs</th><th>Max DD</th><th>PF</th><th>Sample</th></tr></thead>
 <tbody>${row('strategy',rep.overall,'')}${row('baseline (all)',b.overall,'')}${variants}${unders}</tbody></table>
 ${bothWays(rep)}${policies(rep)}${effects(rep)}${reversals(rep)}
 <div class="mute">Baseline skipped: ${esc(JSON.stringify(b.skipped_signals))}; dropped for missing data: ${esc(JSON.stringify(b.data_exclusions))}. Strategy dropped for missing data: ${esc(JSON.stringify(rep.data_exclusions))}.${rep.download&&rep.download.contracts_wanted_but_not_stored?` <b class="down">INCOMPLETE: ${rep.download.contracts_wanted_but_not_stored} option contracts were wanted but are not stored (run with --download).</b>`:''}</div>`;
}
function reversals(rep){
 const o=rep.reversal_outcomes; if(!o) return '';
 const nl=o.new_low_afterwards;
 return `<h2 style="margin-top:12px">Every confirmed reversal: what happened next <span class="tag warn">${esc(o.label)}</span></h2>
 <div class="mute">${o.confirmations} confirmations in ${o.stocks} stocks (daily prices from 2019-01-22, development period). New low within ${nl.within_sessions} sessions: <b>${fmt(nl.share_pct,1)}%</b> of ${nl.cases} cases${nl.low_sample?' <b class="STALE">LOW SAMPLE</b>':''}.</div>
 <table><thead><tr><th>Sessions later</th><th>Cases</th><th>Median %</th><th>Mean %</th><th>25th pct</th><th>75th pct</th><th>Worst %</th><th>Best %</th><th>Up in % of cases</th><th>Sample</th></tr></thead><tbody>${o.outcomes.map(r=>`<tr><td>${r.sessions}</td><td>${r.cases}</td><td>${fmt(r.median_pct)}</td><td>${fmt(r.mean_pct)}</td><td>${fmt(r.p25_pct)}</td><td>${fmt(r.p75_pct)}</td><td>${fmt(r.worst_pct)}</td><td>${fmt(r.best_pct)}</td><td>${fmt(r.up_pct_of_cases,1)}</td><td class="${r.low_sample?'STALE':''}">${r.low_sample?'LOW SAMPLE':'ok'}</td></tr>`).join('')}</tbody></table>`;
}
function policies(rep){
 const all=rep.exit_policy_comparison; if(!all) return '';
 const tr=(g,r)=>`<tr><td>${g}: ${esc(r.label)}</td><td>${r.campaigns}</td><td>${r.rolls} (max ${r.max_rolls_in_a_campaign})</td><td>${fmt(r.win_rate_pct,1)}</td><td>${fmt(r.avg_win_inr)}</td><td>${fmt(r.avg_loss_inr)}</td><td class="${r.net_pnl_inr>0?'up':(r.net_pnl_inr<0?'down':'')}">${fmt(r.net_pnl_inr)}</td><td>${fmt(r.per_campaign_inr)}</td><td>${fmt(r.worst_campaign_inr)}</td><td>${fmt(r.max_drawdown_inr)}</td><td>${fmt(r.profit_factor)}</td><td>${esc(Object.keys(r.roll_refusals||{}).length?JSON.stringify(r.roll_refusals):'-')}</td><td>${r.data_incomplete_campaigns||'-'}</td><td class="${r.low_sample?'STALE':''}">${r.low_sample?'LOW SAMPLE':'ok'}</td></tr>`;
 return Object.values(all).map(pc=>`<h2 style="margin-top:12px">Exit policies on the same entries (per campaign, ${esc(pc.exit_check)} execution)</h2>
 <table><thead><tr><th></th><th>Campaigns</th><th>Rolls</th><th>Win %</th><th>Avg win</th><th>Avg loss</th><th>Net</th><th>Per campaign</th><th>Worst campaign</th><th>Max DD</th><th>PF</th><th>Roll refused by</th><th>Data-incomplete (left out)</th><th>Sample</th></tr></thead>
 <tbody>${pc.strategy.map(r=>tr('strategy',r)).join('')}${pc.baseline.map(r=>tr('baseline',r)).join('')}</tbody></table><div class="mute">${esc(pc.note)}</div>`).join('');
}
function effects(rep){
 const fx=rep.rule_effects; if(!fx) return '';
 const tr=r=>`<tr><td>${esc(r.label)}</td><td>${r.trades}</td><td>${fmt(r.win_rate_pct,1)}</td><td class="${r.net_pnl_inr>0?'up':(r.net_pnl_inr<0?'down':'')}">${fmt(r.net_pnl_inr)}</td><td>${fmt(r.per_trade_inr)}</td><td>${fmt(r.profit_factor)}</td><td>${fmt(r.max_drawdown_inr)}</td><td>${esc(Object.keys(r.blocked||{}).length?JSON.stringify(r.blocked):'-')}</td><td>${r.alert_exits||'-'}</td><td class="${r.low_sample?'STALE':''}">${r.low_sample?'LOW SAMPLE':'ok'}</td></tr>`;
 const cond=Object.entries(rep.baseline_by_entry_condition||{}).map(([k,rows])=>`<h3>Baseline by ${esc(k)}</h3><table><thead><tr><th></th><th>Trades</th><th>Win %</th><th>Net</th><th>Per trade</th><th>PF</th><th>Stopped or strike passed %</th><th>Sample</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${esc(r.label)}</td><td>${r.trades}</td><td>${fmt(r.win_rate_pct,1)}</td><td>${fmt(r.net_pnl_inr)}</td><td>${fmt(r.per_trade_inr)}</td><td>${fmt(r.profit_factor)}</td><td>${fmt(r.closed_beyond_sold_strike_or_stopped_pct,1)}</td><td class="${r.low_sample?'STALE':''}">${r.low_sample?'LOW SAMPLE':'ok'}</td></tr>`).join('')}</tbody></table>`).join('');
 return `<h2 style="margin-top:12px">Effect of each rule and alert (${esc(fx.exit_check)} exit check)</h2>
 <table><thead><tr><th></th><th>Trades</th><th>Win %</th><th>Net</th><th>Per trade</th><th>PF</th><th>Max DD</th><th>Signals blocked</th><th>Alert exits</th><th>Sample</th></tr></thead><tbody>${fx.rows.map(tr).join('')}</tbody></table>
 <div class="mute">${esc(fx.note)}</div>${cond}`;
}
function bothWays(rep){
 const x=rep.exit_check_comparison; if(!x||Object.keys(x).length<2) return rep.exit_check_label?`<div class="mute">Exits: ${esc(rep.exit_check_label)}.</div>`:'';
 const rows=Object.entries(x).map(([m,o])=>row(esc(m)+' check: strategy',o.strategy,'')+row(esc(m)+' check: baseline',o.baseline,'')
  +Object.entries(o.baseline_by_variant||{}).map(([k,v])=>row(esc(m)+' check: '+esc(k),v,'')).join('')).join('');
 const how=Object.entries(x).map(([m,o])=>`<li class="mute"><b>${esc(m)}</b>: ${esc(o.label)}. Baseline exits ${esc(JSON.stringify(o.baseline_by_exit))}, decided on ${esc(JSON.stringify(o.baseline_by_exit_basis))}${o.data_incomplete&&o.data_incomplete.baseline.DATA_INCOMPLETE_CAMPAIGNS?`; ${o.data_incomplete.baseline.DATA_INCOMPLETE_CAMPAIGNS} baseline campaign(s) and ${o.data_incomplete.strategy.DATA_INCOMPLETE_CAMPAIGNS} strategy campaign(s) left out as data-incomplete (a session without a synchronized bar)`:''}.</li>`).join('');
 return `<h2 style="margin-top:12px">Target and stop checked both ways (main result: ${esc(rep.exit_check)})</h2>
 <table><thead><tr><th></th><th></th><th>Trades</th><th>Win %</th><th>Avg win</th><th>Avg loss</th><th>Net</th><th>Costs</th><th>Max DD</th><th>PF</th><th>Sample</th></tr></thead><tbody>${rows}</tbody></table><ul>${how}</ul>`;
}
function stat(rep){
 const s=rep.strategy_status; if(!s) return '';
 return `<span class="tag warn">${esc(s.label)}</span><span class="tag">${s.alerts?'alerts on':'no alerts'}</span><span class="tag">${s.mode==='live'?'live':'paper only'}</span>`;
}
async function load(){
 const r=await (await fetch('/api/backtests')).json(); const seen=new Set(); let html='';
 for(const run of r.runs){ if(seen.has(run.strategy_id))continue; seen.add(run.strategy_id); const rep=run.report;
  html+=row(`<a href="#" onclick="show('${esc(run.run_id)}');return false">${esc(run.strategy_id)}</a><div>${stat(rep)}</div><div class="mute">${esc(rep.cost_label)}</div>${rep.kind==='option'?`<div class="mute">${rep.payoff_simulation?'PAYOFF SIMULATION':'real option prices'}; baseline net Rs ${fmt(rep.baseline.overall.net_pnl_inr)} in ${rep.baseline.overall.trades} trades: <b>${esc(rep.comparison.verdict)}</b></div>`:''}`,
            rep.overall,(rep.period.from||'-')+' .. '+(rep.period.to||'-'));}
 document.getElementById('runs').innerHTML=html||'<tr><td colspan="11" class="mute">No backtests yet. Run python -m app.backtest</td></tr>';
}
load();
</script></body></html>"""



OPTIONS_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Options View</title>
<style>
 :root{--bg:#f6f7f9;--card:#fff;--ink:#16202a;--mute:#5b6875;--line:#dde2e8;--ok:#0a7a45;--bad:#b3261e;--warn:#9a6200}
 @media (prefers-color-scheme:dark){:root{--bg:#11161c;--card:#1a212a;--ink:#e8edf2;--mute:#9aa7b4;--line:#2c3743;--ok:#4cc38a;--bad:#ff8a80;--warn:#f0b849}}
 body{margin:0;font:15px/1.45 system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
 header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:8px 20px;align-items:baseline}
 h1{font-size:18px;margin:0} h2{font-size:14px;margin:0 0 8px;color:var(--mute);font-weight:600} h3{font-size:14px;margin:10px 0 4px}
 .tag{font-size:12px;border:1px solid var(--line);border-radius:4px;padding:2px 8px;color:var(--mute)}
 .warn{color:var(--warn);border-color:var(--warn)}
 main{padding:16px 20px;display:grid;gap:16px;max-width:1100px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px;overflow-x:auto}
 table{border-collapse:collapse;width:100%} th,td{padding:5px 8px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
 th{font-size:12px;color:var(--mute);font-weight:600} th:first-child,td:first-child{text-align:left}
 .up{color:var(--ok)} .down{color:var(--bad)} .kv{display:flex;flex-wrap:wrap;gap:6px 24px} .kv b{font-weight:600}
 .badge{font-weight:700;font-size:13px;border-radius:6px;padding:3px 9px;border:2px solid currentColor}
 .SIGNAL{color:var(--ok)} .NO_TRADE{color:var(--mute)} .mute{color:var(--mute);font-size:13px} .atm td{font-weight:700}
 ul{margin:4px 0 0;padding-left:20px} li{margin:2px 0} .mid{text-align:center !important;font-weight:700}
 textarea{width:100%;box-sizing:border-box;min-height:70px;font:13px ui-monospace,Consolas,monospace;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:6px}
 select,button{font:inherit;padding:4px 10px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--ink)}
 details summary{cursor:pointer;color:var(--mute);font-size:13px;font-weight:600}
 .idea{border:1px solid var(--line);border-radius:8px;padding:10px;margin-top:10px}
</style></head><body>
<header><h1>Options view</h1>
 <span class="tag">RESEARCH ONLY: this app cannot place orders</span>
 <a class="tag" href="/">Back to prices</a> <a class="tag" href="/backtests">Backtest reports</a>
 <a class="tag" href="/stock-option-selling">Stock option selling</a>
 <span class="tag" id="feed"></span></header>
<main>
 <div class="card"><h2>Underlying</h2>
  <select id="sym"></select> <span class="mute" id="asof"></span>
  <div class="kv" id="top" style="margin-top:8px"></div>
  <div id="problems"></div>
  <div class="mute" id="expiries" style="margin-top:6px"></div></div>
 <div class="card"><h2>Option ideas (figures for ONE lot, computed by code)</h2><div id="ideas" class="mute">loading...</div></div>
 <div class="card"><h2>Option chain: open interest, implied volatility and delta</h2><div id="chains" class="mute">loading...</div></div>
 <div class="card"><h2>Margin for one lot (you enter it from ICICI Direct's margin calculator)</h2>
  <table><thead><tr><th>Underlying</th><th>Structure</th><th>Width</th><th>Margin per lot (Rs)</th><th>As of</th><th>Usable</th></tr></thead><tbody id="margins"></tbody></table>
  <p class="mute">A spread signal needs its margin here, otherwise it shows MARGIN_UNKNOWN. Paste CSV rows:
  <code>underlying,structure,width,margin_per_lot_inr,as_of</code> with structure CREDIT_SPREAD or IRON_CONDOR and width = distance between the sold and bought strike.</p>
  <textarea id="mtext" placeholder="underlying,structure,width,margin_per_lot_inr,as_of&#10;NIFTY,CREDIT_SPREAD,250,38500,2026-11-13"></textarea>
  <button id="mbtn" type="button">Save margins</button> <span class="mute" id="mresult"></span></div>
 <div class="card"><ul id="notes" class="mute"></ul></div>
</main>
<script>
const f=(v,d=2)=>v===null||v===undefined?"-":Number(v).toLocaleString("en-IN",{minimumFractionDigits:d,maximumFractionDigits:d});
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
async function j(u){const r=await fetch(u);return r.json()}
const sel=document.getElementById("sym");
function side(x){return x?`<td>${f(x.oi,0)}</td><td>${f(x.oi_change,0)}</td><td>${f(x.iv_pct,1)}</td><td>${f(x.delta,2)}</td><td>${f(x.bid)}</td><td>${f(x.ask)}</td>`:"<td colspan=6 class=mute>no usable quote</td>"}
function idea(i){
 let h=`<div class="idea"><span class="badge ${i.status}">${i.status==="SIGNAL"?"SIGNAL":"NO TRADE"}</span> <b>${esc(i.strategy_id)}</b> <span class="mute">${esc(i.strategy_version)}</span>
  <span class="tag warn">${esc(i.label)}</span><span class="tag">${i.alerts?"alerts on":"no alerts"}</span>`;
 if(i.status!=="SIGNAL") return h+`<ul>${i.reasons.map(r=>`<li>${esc(r)}</li>`).join("")}</ul></div>`;
 for(const s of i.signals){const d=s.details;
  h+=`<h3>${esc(s.symbol)} ${esc(d.structure.replace("_"," "))}, expiry ${esc(d.expiry)} (${d.days_to_expiry} days, ${esc(d.expiry_kind)})</h3>
  <table><thead><tr><th>Leg</th><th>Strike</th><th>Price</th><th>Bid</th><th>Ask</th><th>IV %</th><th>Delta</th><th>OI</th></tr></thead><tbody>
  ${d.legs.map(l=>`<tr><td>${esc(l.action)} ${esc(l.right)}</td><td>${f(l.strike,0)}</td><td>${f(l.price)}</td><td>${f(l.bid)}</td><td>${f(l.ask)}</td><td>${f(l.iv_pct,1)}</td><td>${f(l.delta,2)}</td><td>${f(l.open_interest,0)}</td></tr>`).join("")}</tbody></table>
  <div class="kv" style="margin-top:6px"><span>${d.net_credit!==undefined?"Net credit":"Premium"} per share: <b>${f(d.premium)}</b></span><span>Lot size: <b>${d.lot_size}</b></span>
  <span>${d.net_premium_inr<0?"Total cost":"Credit received"}: <b>&#8377;${f(Math.abs(d.net_premium_inr))}</b></span>
  <span>Max loss: <b class="down">&#8377;${f(d.max_loss_inr)}</b> (${f(d.max_loss_pct_of_capital)}% of capital)</span>
  <span>Max profit: <b class="up">${d.max_profit_inr===null?"unlimited":"&#8377;"+f(d.max_profit_inr)}</b></span>
  <span>Breakeven: <b>${d.breakevens.map(b=>f(b)).join(" / ")}</b></span>
  <span>Margin blocked: <b>${d.margin_inr===null?"unknown":"&#8377;"+f(d.margin_inr)}</b> <span class="mute">${esc(d.margin_note)}</span></span>
  <span>Est. costs: <b>&#8377;${f(d.costs_inr)}</b></span><span>IV rank: <b>${f(d.iv_rank,0)}</b></span></div>
  ${d.warnings.map(w=>`<div class="down">${esc(w)}</div>`).join("")}
  <ul>${s.why_now.map(r=>`<li>${esc(r)}</li>`).join("")}</ul>
  <div class="mute">Entry reference ${f(s.entry)}, stop ${f(s.stop)}, target ${f(s.target)} (per share of the ${d.net_credit!==undefined?"spread":"option"}). Valid until ${new Date(s.valid_until).toLocaleTimeString()}. Estimate, not a forecast.</div>`;}
 return h+"</div>";
}
async function load(){
 const sym=sel.value; if(!sym) return;
 try{
  const v=await j("/api/options/"+encodeURIComponent(sym));
  document.getElementById("asof").textContent="As of "+new Date(v.as_of_utc).toLocaleTimeString();
  const feed=document.getElementById("feed"); feed.textContent=v.feed==="sim"?"SIMULATED option chain, not market data":"Feed: ICICI Breeze"; feed.className="tag"+(v.feed==="sim"?" warn":"");
  document.getElementById("top").innerHTML=`<span>Spot: <b>${f(v.spot)}</b> <span class="${v.spot?"up":"down"}">${esc(v.spot_status)}</span></span><span>Regime: <b>${esc(v.regime)}</b></span>
   <span>IV measure: <b>${f(v.iv.value_pct)}</b></span><span>IV rank: <b>${v.iv.rank===null?"unknown":f(v.iv.rank,0)}</b></span><span class="mute">${esc(v.iv.basis)} ${esc(v.iv.note)}</span>`;
  document.getElementById("problems").innerHTML=v.problems.map(p=>`<div class="down">${esc(p)}</div>`).join("");
  document.getElementById("expiries").innerHTML="Lot size and expiries from the instrument list"+(v.instruments_as_of_utc?" (read "+new Date(v.instruments_as_of_utc).toLocaleString()+")":"")+": "+(v.expiries.map(e=>`${esc(e.expiry)} ${esc(e.kind)} lot ${e.lot_size}`).join("; ")||"none");
  document.getElementById("ideas").innerHTML=v.ideas.map(idea).join("")||"no option strategies for this symbol";
  document.getElementById("chains").innerHTML=v.chains.map(c=>`<h3>${esc(c.expiry)} (${esc(c.kind)}, ${c.days_to_expiry} days, lot ${c.lot_size}) ${c.stale?'<span class="down">STALE or not loaded</span>':'<span class="up">fresh</span>'} <span class="mute">${c.as_of_utc?"snapshot "+new Date(c.as_of_utc).toLocaleTimeString():""}</span></h3>
   <div class="kv"><span>Put-call ratio: <b>${f(c.put_call_ratio)}</b></span><span>Support (highest-OI put): <b>${f(c.support_highest_oi_put,0)}</b></span><span>Resistance (highest-OI call): <b>${f(c.resistance_highest_oi_call,0)}</b></span>
   <span>OI change today: calls <b>${f(c.oi_change.calls,0)}</b>, puts <b>${f(c.oi_change.puts,0)}</b></span><span>ATM IV: <b>${f(c.atm_iv_pct,1)}%</b></span></div>
   <details><summary>Strikes near the money (${c.rows_stored} quotes stored)</summary><table><thead><tr><th>Call OI</th><th>OI chg</th><th>IV %</th><th>Delta</th><th>Bid</th><th>Ask</th><th class="mid">Strike</th><th>Put OI</th><th>OI chg</th><th>IV %</th><th>Delta</th><th>Bid</th><th>Ask</th></tr></thead>
   <tbody>${c.table.map(r=>`<tr>${side(r.ce)}<td class="mid">${f(r.strike,0)}</td>${side(r.pe)}</tr>`).join("")}</tbody></table></details>`).join("")||"no chain loaded";
  document.getElementById("margins").innerHTML=v.margins.map(m=>`<tr><td>${esc(m.underlying)}</td><td>${esc(m.structure)}</td><td>${f(m.width,0)}</td><td>${f(m.margin_per_lot_inr)}</td><td>${esc(m.as_of)}</td><td class="${m.usable?"up":"down"}">${m.usable?"yes":"out of date"}</td></tr>`).join("")||"<tr><td colspan=6 class=mute>none entered for this underlying</td></tr>";
  document.getElementById("notes").innerHTML=v.notes.map(n=>`<li>${esc(n)}</li>`).join("");
 }catch(e){document.getElementById("asof").textContent="Server not reachable"}
}
document.getElementById("mbtn").onclick=async()=>{const out=document.getElementById("mresult");
 try{const r=await fetch("/data/import/margins",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text:document.getElementById("mtext").value})});
  const x=await r.json();out.textContent=x.ok?`Saved ${x.rows_saved} row(s)`:("Not saved: "+(x.errors||[x.detail]).join("; "));out.className=x.ok?"up":"down";if(x.ok)load();
 }catch(e){out.textContent="Import failed";out.className="down"}};
(async()=>{const o=await j("/api/options");sel.innerHTML=o.underlyings.map(u=>`<option>${esc(u)}</option>`).join("");sel.onchange=load;load();setInterval(load,30000)})();
</script></body></html>"""


SHORTLIST_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stock Option Selling</title>
<style>
 :root{--bg:#f6f7f9;--card:#fff;--ink:#16202a;--mute:#5b6875;--line:#dde2e8;--ok:#0a7a45;--bad:#b3261e;--warn:#9a6200}
 @media (prefers-color-scheme:dark){:root{--bg:#11161c;--card:#1a212a;--ink:#e8edf2;--mute:#9aa7b4;--line:#2c3743;--ok:#4cc38a;--bad:#ff8a80;--warn:#f0b849}}
 body{margin:0;font:15px/1.45 system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
 header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:8px 20px;align-items:baseline}
 h1{font-size:18px;margin:0} h2{font-size:14px;margin:0 0 8px;color:var(--mute);font-weight:600} h3{font-size:14px;margin:10px 0 4px}
 .tag{font-size:12px;border:1px solid var(--line);border-radius:4px;padding:2px 8px;color:var(--mute)}
 .warn{color:var(--warn);border-color:var(--warn)}
 main{padding:16px 20px;display:grid;gap:16px;max-width:1100px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px;overflow-x:auto}
 table{border-collapse:collapse;width:100%} th,td{padding:5px 8px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
 th{font-size:12px;color:var(--mute);font-weight:600} th:first-child,td:first-child{text-align:left}
 .up{color:var(--ok)} .down{color:var(--bad)} .kv{display:flex;flex-wrap:wrap;gap:6px 24px} .kv b{font-weight:600}
 .badge{font-weight:700;font-size:13px;border-radius:6px;padding:3px 9px;border:2px solid currentColor}
 .SIGNAL{color:var(--ok)} .NO_TRADE{color:var(--mute)} .mute{color:var(--mute);font-size:13px} .atm td{font-weight:700}
 ul{margin:4px 0 0;padding-left:20px} li{margin:2px 0} .mid{text-align:center !important;font-weight:700}
 textarea{width:100%;box-sizing:border-box;min-height:70px;font:13px ui-monospace,Consolas,monospace;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:6px}
 select,button{font:inherit;padding:4px 10px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--ink)}
 details summary{cursor:pointer;color:var(--mute);font-size:13px;font-weight:600}
 .idea{border:1px solid var(--line);border-radius:8px;padding:10px;margin-top:10px}
</style></head><body>
<header><h1>Stock option selling: daily shortlist</h1>
 <span class="tag">RESEARCH ONLY: this app cannot place orders</span>
 <a class="tag" href="/">Back to prices</a> <a class="tag" href="/options">Options view</a>
 <a class="tag" href="/backtests">Backtest reports</a> <span class="tag warn" id="label"></span></header>
<main>
 <div class="card"><h2>Shortlist</h2><div class="mute" id="asof"></div>
  <table><thead><tr><th>Rank</th><th>Stock</th><th>Side</th><th>Expiry</th><th>Score (0-100)</th><th>IV rank</th><th>IV falling</th><th>Rel. strength</th><th>Strike distance</th><th>Range profile</th><th>Liquidity</th><th style="text-align:left">Why</th></tr></thead><tbody id="top"></tbody></table>
  <div class="mute">The score ranks candidates; it is not a probability. Each factor shows its points (0-100) and, in brackets, its contribution to the score.</div>
  <div class="mute" id="note" style="margin-top:8px"></div></div>
 <div class="card"><h2>Stock profile and scenario history <span class="tag warn">history, not a forecast</span></h2>
  <select id="psym"></select> <span class="mute">daily bars from 2019-01-22 only</span>
  <div id="prof" class="mute" style="margin-top:8px">Pick a stock.</div></div>
 <div class="card"><h2>Reversal watch <span class="tag warn">watch only, never a signal</span></h2>
  <div class="mute" id="revnote"></div>
  <table><thead><tr><th>Stock</th><th>State</th><th>Close</th><th>Below 52-week high</th><th>Base low</th><th>Stall zones (prior breakdown levels)</th><th style="text-align:left">Detail</th></tr></thead><tbody id="rev"></tbody></table></div>
 <div class="card"><h2>Campaigns (an original trade and its rolls)</h2>
  <table><thead><tr><th>Stock</th><th>Side</th><th>Rolls</th><th>Realised P&amp;L</th><th>Hedge cost paid</th><th>Charges</th><th>Best possible outcome</th><th style="text-align:left">Note</th></tr></thead><tbody id="camps"></tbody></table></div>
 <div class="card"><h2>Settlement readiness <span class="tag warn">alerts and estimates only</span></h2>
  <div class="mute" id="setnote"></div>
  <table><thead><tr><th style="text-align:left">ICICI rule</th><th style="text-align:left">As entered</th><th>Confirmed</th><th style="text-align:left">Shown on cards and alerts</th></tr></thead><tbody id="setrules"></tbody></table></div>
 <div class="card"><h2>Not on the list today, and why</h2>
  <table><thead><tr><th>Stock</th><th>Side</th><th>Expiry</th><th style="text-align:left">Reason</th></tr></thead><tbody id="rest"></tbody></table></div>
</main>
<script>
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const f=x=>x==null?"-":Number(x).toFixed(2);
const side=s=>s==="PE"?"bull put":(s==="CE"?"bear call":"-");
const n=(x,u="")=>x==null?"-":(Number(x).toFixed(1)+u);
async function profile(sym){
 const el=document.getElementById("prof"); el.textContent="Loading...";
 const r=await (await fetch("/api/stock-option-selling/profile/"+encodeURIComponent(sym))).json(); const p=r.profile;
 if(!p){el.innerHTML=r.notes.map(esc).join("<br>");return}
 const w=Object.values(p.windows).map(x=>`<tr><td>last ${x.years} years (${x.bars} sessions from ${esc(x.from)})</td><td>${n(x.monthly_range_median_pct,"%")}</td><td>${n(x.monthly_range_p80_pct,"%")}</td><td>${n(x.avg_sessions_low_to_high)} (${x.upswings})</td><td>${n(x.avg_sessions_high_to_low)} (${x.downswings})</td><td>${n(x.typical_move_pct,"%")}</td><td>${n(x.move_p80_pct,"%")}</td><td>${x.breach?`${n(x.breach.breached_pct,"%")} of ${x.breach.cases} (strike ${n(x.breach.distance_pct,"%")} away)`:"-"}</td></tr>`).join("");
 const sc=r.scenarios.map(x=>`<tr><td>${esc(x.scenario)}</td><td>${x.cases}</td><td class="${x.status==="ok"?"":"warn"}">${esc(x.status)}</td><td>${n(x.median_pct,"%")}</td><td>${n(x.p10_pct,"%")}</td><td>${n(x.p90_pct,"%")}</td><td>${n(x.worst_pct,"%")}</td><td>${n(x.down_pct_of_cases,"%")}</td><td style="text-align:left;white-space:normal">${esc(x.note)}</td></tr>`).join("");
 el.innerHTML=`<div class="kv"><span>State now: <b>${esc(p.state_now||"unknown")}</b></span><span>Move implied by option prices over ${Object.values(p.windows)[0].holding_sessions} sessions: <b>${n(p.implied_move_pct,"%")}</b></span><span>As of ${esc(p.as_of)}</span></div>
 <div class="mute">${esc(p.state_note)}. ${esc(p.implied_move_note)}.${r.setup?` Rules today: ${esc(r.setup.status)}${r.setup.short_strike?`, sold strike ${r.setup.short_strike} (${n(r.setup.distance_pct,"%")} away)`:""}.`:""} ${r.notes.map(esc).join(" ")}</div>
 <table style="margin-top:8px"><thead><tr><th>Window</th><th>Monthly high-low range (median)</th><th>80th pct</th><th>Sessions swing low to high (count)</th><th>Sessions high to low (count)</th><th>Typical move over the holding period</th><th>80th pct</th><th>Strike passed within the holding period</th></tr></thead><tbody>${w}</tbody></table>
 <h3>30-day moves after past events: history, not a forecast</h3>
 <table><thead><tr><th>Scenario</th><th>Cases</th><th>Sample</th><th>Median</th><th>10th pct</th><th>90th pct</th><th>Worst</th><th>Share of cases down</th><th style="text-align:left">Note</th></tr></thead><tbody>${sc}</tbody></table>`;
}
(async()=>{
 const r=await (await fetch("/api/stock-option-selling/shortlist")).json();
 document.getElementById("label").textContent=r.label+(r.alerts?"":" - no alerts");
 document.getElementById("asof").textContent=r.session_date?`Session ${r.session_date}, built ${r.created_utc} (feed ${r.feed}). Top ${r.shortlist_size} by weighted score.`:"No shortlist stored yet. After the close run:  python -m app.options.shortlist";
 const cg=r.correlation_groups||{};
 document.getElementById("note").textContent=r.note+" Weights: "+JSON.stringify(r.weights)+"."
  +(cg.date?` Correlated groups on ${cg.date} (same sector or correlation >= 0.7): ${(cg.groups||[]).filter(g=>g.length>1).map(g=>g.join(", ")).join(" | ")||"none"}; group sizes ${JSON.stringify(cg.sizes)}${cg.problem_count?`; ${cg.problem_count} pair(s) without a measurable relationship`:""}.`:"");
 const top=r.rows.filter(x=>x.status==="SHORTLISTED"), rest=r.rows.filter(x=>x.status!=="SHORTLISTED");
 const rv=await (await fetch("/api/stock-option-selling/reversals")).json();
 document.getElementById("revnote").textContent=rv.note+(rv.alerts_approved?"":" Alerts: not approved.")+(rv.session_date?` Session ${rv.session_date}.`:"");
 const cand=c=>c.trade?`<b>${esc(c.candidate)}</b>: ${esc(c.trade.why_now.slice(1,3).join("; "))}`:`<b>${esc(c.candidate)}</b>: no trade (${esc((c.reasons||[]).join("; "))})`;
 document.getElementById("rev").innerHTML=rv.rows.map(x=>`<tr><td>${esc(x.stock)}</td><td><span class="tag ${x.state==="REVERSAL_CONFIRMED"?"warn":""}">watch: ${esc(x.state.replace(/_/g," ").toLowerCase())}</span></td><td>${f(x.close)}</td><td>${f(x.drawdown_pct)}%</td><td>${f(x.base_low)}</td><td>${(x.stall_zones||[]).map(f).join(", ")||"-"}</td><td style="text-align:left;white-space:normal">${x.confirmed_on?`REVERSAL_CONFIRMED on ${esc(x.confirmed_on)}: ${x.reasons.map(esc).join("; ")}<br>${x.candidates.map(cand).join("<br>")}`:""}</td></tr>`).join("")||'<tr><td colspan="7" class="mute">no stock is washed out today</td></tr>';
 const cs=await (await fetch("/api/stock-option-selling/campaigns")).json();
 const sr=await (await fetch("/api/stock-option-selling/settlement")).json();
 document.getElementById("setnote").textContent=`${sr.note} Alerts start ${sr.alerts_from.from_trading_days} trading days before expiry for a sold leg in the money or within ${sr.alerts_from.near_money_pct}% of spot; URGENT from ${sr.alerts_from.urgent_from_trading_days} trading days (the forced exit). ${sr.positions_note}`+(sr.calendar&&!sr.calendar.covered?` CALENDAR: ${sr.calendar.note}`:"");
 if(sr.calendar&&!sr.calendar.covered)document.getElementById("setnote").className="down";
 document.getElementById("setrules").innerHTML=sr.rules.map(x=>`<tr><td style="text-align:left">${esc(x.rule)}</td><td style="text-align:left">${esc(x.value)}</td><td class="${x.confirmed?'':'down'}">${x.confirmed?'yes':'NO'}</td><td style="text-align:left" class="down">${esc(x.label)}</td></tr>`).join("");
 document.getElementById("camps").innerHTML=cs.campaigns.map(c=>`<tr><td>${esc(c.stock)}</td><td>${side(c.side)}</td><td>${c.rolls}</td><td>${f(c.realised_pnl_inr)}</td><td>${f(c.hedge_cost_inr)}</td><td>${f(c.charges_inr)}</td><td>${f(c.best_possible_outcome_inr)}</td><td style="text-align:left" class="${c.message?'down':''}">${esc(c.message||(c.open?'open':'closed'))}</td></tr>`).join("")||`<tr><td colspan="8" class="mute">${esc(cs.note)}</td></tr>`;
 const ps=document.getElementById("psym"); ps.innerHTML='<option value="">-</option>'+(r.stocks||[]).map(s=>`<option>${esc(s)}</option>`).join(""); ps.onchange=()=>ps.value&&profile(ps.value);
 document.getElementById("top").innerHTML=top.map(x=>`<tr><td>${x.rank}</td><td>${esc(x.stock)}</td><td>${side(x.side)}</td><td>${esc(x.expiry)}</td><td>${x.score==null?"incomplete":f(x.score)}</td>${["iv_rank","iv_falling","rel_strength","strike_distance","range_profile","liquidity"].map(k=>`<td>${x.scores[k]==null?"-":f(x.scores[k])} <span class="mute">(${x.contributions&&x.contributions[k]!=null?f(x.contributions[k]):"-"})</span></td>`).join("")}<td style="text-align:left;white-space:normal"><span class="tag">${esc(x.label||"research candidate")}${x.snapshot?` - ${esc(x.snapshot.session_date)} close`:""}</span> ${x.actionable?"<b>actionable</b>":`<span class="mute">not actionable: ${esc((x.not_actionable_because||[]).map(s=>s.split(":")[0]).join(", "))}</span>`}<br>${x.reasons.map(esc).join("<br>")}</td></tr>`).join("")||'<tr><td colspan="12" class="mute">no stock passes every rule today</td></tr>';
 document.getElementById("rest").innerHTML=rest.map(x=>`<tr><td>${esc(x.stock)}</td><td>${side(x.side)}</td><td>${esc(x.expiry||"-")}</td><td style="text-align:left;white-space:normal">${x.reasons.map(esc).join("<br>")}</td></tr>`).join("")||'<tr><td colspan="4" class="mute">-</td></tr>';
})();
</script></body></html>"""
