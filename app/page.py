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
 <span class="tag" id="feed"></span><span class="tag" id="asof"></span></header>
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
cfg();tick();regime();setInterval(tick,2000);setInterval(regime,15000);
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
 <a class="tag" href="/">Back to prices</a></header>
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
 <div class="badges"><span class="tag warn">${esc(rep.cost_label)}</span><span class="tag">${esc(rep.costs.plan)}</span>
 <span class="tag">source ${esc(rep.data_source)}</span>${rep.data_note?`<span class="tag warn">${esc(rep.data_note)}</span>`:''}</div>
 <div class="mute">${esc(rep.overall.sample_note)}.${rep.exposure?` Peak exposure: ${rep.exposure.max_open_trades} trades open, Rs ${fmt(rep.exposure.max_position_value_inr,0)} (${rep.exposure.max_position_value_pct_of_capital}% of capital).`:''} Regimes: ${esc(rep.allowed_regimes.join(', '))}. Skipped signals: ${esc(JSON.stringify(rep.skipped_signals))}</div>
 <h2 style="margin-top:12px">By regime</h2><table><thead><tr><th>Regime</th><th></th><th>Trades</th><th>Win %</th><th>Avg win</th><th>Avg loss</th><th>Net</th><th>Costs</th><th>Max DD</th><th>PF</th><th>Sample</th></tr></thead><tbody>${reg}</tbody></table>
 <h2 style="margin-top:12px">Walk-forward windows (fixed rules)</h2><table><thead><tr><th>Window</th><th></th><th>Trades</th><th>Win %</th><th>Avg win</th><th>Avg loss</th><th>Net</th><th>Costs</th><th>Max DD</th><th>PF</th><th>Sample</th></tr></thead><tbody>${wf}</tbody></table>
 <h2 style="margin-top:12px">Holdout</h2><table><tbody>${hold}</tbody></table>
 <ul>${rep.notes.map(n=>`<li class="mute">${esc(n)}</li>`).join('')}</ul></div>`;
}
async function load(){
 const r=await (await fetch('/api/backtests')).json(); const seen=new Set(); let html='';
 for(const run of r.runs){ if(seen.has(run.strategy_id))continue; seen.add(run.strategy_id); const rep=run.report;
  html+=row(`<a href="#" onclick="show('${esc(run.run_id)}');return false">${esc(run.strategy_id)}</a><div class="mute">${esc(rep.cost_label)}</div>`,
            rep.overall,(rep.period.from||'-')+' .. '+(rep.period.to||'-'));}
 document.getElementById('runs').innerHTML=html||'<tr><td colspan="11" class="mute">No backtests yet. Run python -m app.backtest</td></tr>';
}
load();
</script></body></html>"""
