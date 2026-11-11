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
</style></head><body>
<header><h1>Trading Research System</h1>
 <span class="tag">RESEARCH ONLY: this app cannot place orders</span>
 <span class="tag" id="feed"></span><span class="tag" id="asof"></span></header>
<main>
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
cfg();tick();setInterval(tick,2000);
</script></body></html>
"""
