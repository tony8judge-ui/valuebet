from flask import Flask, jsonify, request, Response
from flask_cors import CORS
import requests
import os
import smtplib
import threading
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

app = Flask(__name__)
CORS(app)

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
BOOKMAKERS = "pinnacle,betfair_ex_best_odds,bet365,williamhill,ladbrokes,skybet,paddypower,coral,betvictor,unibet,betway"
BETFAIR_COMMISSION = 0.05
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
GMAIL_USER = "tony8judge@gmail.com"
GMAIL_APP_PASSWORD = "YOUR_APP_PASSWORD_HERE"
ALERT_EMAIL = "tony8judge@gmail.com"
SCAN_INTERVAL = 900
SPORTS_TO_SCAN = ["soccer_epl", "soccer_fa_cup", "soccer_efl_champ", "soccer_uefa_champs_league"]
already_alerted = set()


def send_email(arbs):
    if not arbs:
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"ARB FOUND - {len(arbs)} opportunity"
        msg["From"] = GMAIL_USER
        msg["To"] = ALERT_EMAIL
        text = "ARB OPPORTUNITIES\n\n"
        for a in arbs:
            text += f"Match: {a['match']}\n"
            text += f"Back: {a['outcome']} @ {a['odds']} ({a['bookmaker']})\n"
            text += f"BF Lay: {a['bf_lay_price']}\n"
            text += f"Edge: +{a['edge']}%\n\n"
        msg.attach(MIMEText(text, "plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, ALERT_EMAIL, msg.as_string())
        print(f"Alert sent: {len(arbs)} arbs")
    except Exception as e:
        print(f"Email error: {e}")


def remove_vig(probs):
    total = sum(probs)
    return [p / total for p in probs]


def build_average(books):
    outcome_map = {}
    for b in books:
        market = next((m for m in b.get("markets", []) if m["key"] == "h2h"), None)
        if not market:
            continue
        for o in market.get("outcomes", []):
            outcome_map.setdefault(o["name"], []).append(o["price"])
    outcomes = [{"name": k, "price": sum(v)/len(v)} for k, v in outcome_map.items()]
    return {"key": "average", "title": "Market Average", "markets": [{"key": "h2h", "outcomes": outcomes}]}


def analyse(events, min_edge, ref):
    results = []
    for ev in events:
        books = ev.get("bookmakers", [])
        bf_book = next((b for b in books if b["key"] == "betfair_ex_best_odds"), None)
        bf_back_map = {}
        if bf_book:
            bf_market = next((m for m in bf_book.get("markets", []) if m["key"] == "h2h"), None)
            if bf_market:
                for o in bf_market.get("outcomes", []):
                    bf_back_map[o["name"]] = round(o["price"] / (1 - BETFAIR_COMMISSION), 2)
        ref_book = None
        if ref == "pinnacle":
            ref_book = next((b for b in books if b["key"] == "pinnacle"), None)
        elif ref == "betfair":
            ref_book = next((b for b in books if b["key"] == "betfair_ex_best_odds"), None)
        if not ref_book or ref == "average":
            ref_book = build_average(books)
        if not ref_book:
        continue
        ref_market = next((m for m in ref_book.get("markets", []) if m["key"] == "h2h"), None)
        if not ref_market or not ref_market.get("outcomes"):
            continue
        outcomes = ref_market["outcomes"]
        fair_probs = remove_vig([1/o["price"] for o in outcomes])
        fair_map = {o["name"]: 1/fp for o, fp in zip(outcomes, fair_probs)}
        ref_label = ref_book.get("title", ref) if ref != "average" else "Mkt Avg"
        for book in books:
            if book["key"] in ("pinnacle", "betfair_ex_best_odds"):
                continue
            market = next((m for m in book.get("markets", []) if m["key"] == "h2h"), None)
            if not market:
                continue
            for o in market.get("outcomes", []):
                fair = fair_map.get(o["name"])
                if not fair:
                    continue
                edge = ((o["price"] / fair) - 1) * 100
                if edge < 1:
                    continue
                bf_lay = bf_back_map.get(o["name"])
                is_arb = bf_lay is not None and o["price"] > bf_lay
                results.append({
                    "match": f"{ev['home_team']} v {ev['away_team']}",
                    "sport": ev["sport_key"],
                    "time": ev["commence_time"],
                    "outcome": o["name"],
                    "bookmaker": book.get("title", book["key"]),
                    "odds": round(o["price"], 2),
                    "fair_odds": round(fair, 2),
                    "bf_lay_price": bf_lay,
                    "is_arb": is_arb,
                    "edge": round(edge, 2),
                    "ref_label": ref_label,
                    "signal": "high" if edge >= 10 else "medium" if edge >= 5 else "low"
                })
    results.sort(key=lambda x: (not x["is_arb"], -x["edge"]))
    return results


def auto_scan():
    while True:
        try:
            if ODDS_API_KEY:
                new_arbs = []
                for sport in SPORTS_TO_SCAN:
                    url = (f"{ODDS_API_BASE}/sports/{sport}/odds/"
                           f"?apiKey={ODDS_API_KEY}&regions=uk&markets=h2h"
                           f"&oddsFormat=decimal&bookmakers={BOOKMAKERS}")
                    r = requests.get(url, timeout=15)
                    if not r.ok:
                        continue
                    data = r.json()
                    if not isinstance(data, list):
                        continue
                    for result in analyse(data, 1, "pinnacle"):
                        if result["is_arb"]:
                            key = f"{result['match']}_{result['outcome']}_{result['bookmaker']}"
                            if key not in already_alerted:
                                new_arbs.append(result)
                                already_alerted.add(key)
                if new_arbs:
                    send_email(new_arbs)
        except Exception as e:
            print(f"Auto-scan error: {e}")
        time.sleep(SCAN_INTERVAL)


@app.route("/scan")
def scan():
    api_key = request.args.get("apiKey", "").strip()
    sport = request.args.get("sport", "soccer_epl")
    min_edge = float(request.args.get("minEdge", 3))
    ref = request.args.get("ref", "pinnacle")
    if not api_key:
        return jsonify({"error": "No API key provided"}), 400
    url = (f"{ODDS_API_BASE}/sports/{sport}/odds/"
           f"?apiKey={api_key}&regions=uk&markets=h2h"
           f"&oddsFormat=decimal&bookmakers={BOOKMAKERS}")
    try:
        r = requests.get(url, timeout=15)
        if not r.ok:
            msg = r.json().get("message", f"API error {r.status_code}")
            return jsonify({"error": msg}), r.status_code
        data = r.json()
    except Exception as e:
        return jsonify({"error": str(e)}), 500<head>
        if not isinstance(data, list):
        return jsonify({"error": "Unexpected response"}), 500
    return jsonify({"events_scanned": len(data), "value_bets": analyse(data, min_edge, ref)})


@app.route("/")
def index():
    return Response("""<!DOCTYPE html>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Value Bet Finder</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&family=IBM+Plex+Sans:wght@300;400;500&display=swap');
:root{--bg:#0a0a0a;--surface:#111;--border:#1e1e1e;--bb:#2a2a2a;--green:#00ff87;--gd:#00ff8720;--gm:#00ff8760;--red:#ff3b3b;--rd:#ff3b3b20;--amber:#ffb800;--ad:#ffb80020;--blue:#00b4ff;--bd:#00b4ff20;--bm:#00b4ff60;--text:#e8e8e8;--muted:#555;--m2:#333;}
*{margin:0;padding:0;box-sizing:border-box;}
body{background:var(--bg);color:var(--text);font-family:'IBM Plex Sans',sans-serif;min-height:100vh;padding:20px 16px;}
header{margin-bottom:24px;padding-bottom:16px;border-bottom:1px solid var(--border);}
.logo{font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;letter-spacing:.2em;color:var(--green);text-transform:uppercase;margin-bottom:4px;}
.logo span{color:var(--muted);}
.sub{font-size:13px;color:var(--muted);font-weight:300;}
.box{background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:18px;margin-bottom:18px;display:grid;gap:12px;}
.row{display:grid;grid-template-columns:1fr 1fr;gap:10px;}
@media(max-width:550px){.row{grid-template-columns:1fr;}}
label{font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:600;letter-spacing:.15em;color:var(--muted);text-transform:uppercase;display:block;margin-bottom:5px;}
input,select{width:100%;background:var(--bg);border:1px solid var(--bb);color:var(--text);padding:10px 12px;font-family:'IBM Plex Mono',monospace;font-size:13px;border-radius:2px;outline:none;}
input::placeholder{color:var(--muted);}
select option{background:#1a1a1a;}
.btn{background:var(--green);color:#000;border:none;padding:13px;font-family:'IBM Plex Mono',monospace;font-size:12px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;cursor:pointer;border-radius:2px;width:100%;}
.btn:disabled{opacity:.4;cursor:not-allowed;}
.note{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--amber);padding:10px 12px;background:var(--ad);border:1px solid var(--amber);border-radius:2px;line-height:1.7;}
.note a{color:var(--amber);}
.arb-legend{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--blue);padding:10px 12px;background:var(--bd);border:1px solid var(--blue);border-radius:2px;line-height:1.7;}
.sbar{display:none;gap:5px;margin-bottom:12px;grid-template-columns:repeat(5,1fr);}
.sbar.on{display:grid;}
.stat{background:var(--surface);border:1px solid var(--border);padding:11px 13px;border-radius:2px;}
.sl{font-family:'IBM Plex Mono',monospace;font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);margin-bottom:3px;}
.sv{font-family:'IBM Plex Mono',monospace;font-size:18px;font-weight:700;color:var(--green);}
.sv.a{color:var(--amber);}.sv.b{color:var(--blue);}
.fbar{display:none;gap:7px;margin-bottom:10px;flex-wrap:wrap;}
.fbar.on{display:flex;}
.fb{font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:600;padding:5px 11px;border:1px solid var(--bb);background:transparent;color:var(--muted);cursor:pointer;border-radius:2px;}
.fb.active,.fb:hover{border-color:var(--green);color:var(--green);background:var(--gd);}
.fb.arb-btn.active,.fb.arb-btn:hover{border-color:var(--blue);color:var(--blue);background:var(--bd);}
.ch{display:none;font-family:'IBM Plex Mono',monospace;font-size:10px;text-transform:uppercase;color:var(--muted);grid-template-columns:1.8fr 1fr .7fr .7fr .7fr .7fr .8fr;gap:6px;padding:7px 13px;border-bottom:1px solid var(--border);}
.ch.on{display:grid;}
@media(max-width:750px){.ch{display:none!important;}}
.card{background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--bb);margin-bottom:5px;padding:13px;border-radius:0 2px 2px 0;display:grid;grid-template-columns:1.8fr 1fr .7fr .7fr .7fr .7fr .8fr;gap:6px;align-items:center;}
.card.high{border-left-color:var(--green);background:#0d1a12;}
.card.medium{border-left-color:var(--amber);background:#1a1500;}
.card.arb{border-left-color:var(--blue);background:#001a2a;border-color:var(--blue);}
@media(max-width:750px){.card{grid-template-columns:1fr 1fr;}.card>*:first-child{grid-column:1/-1;}}
.mn{font-size:13px;font-weight:500;margin-bottom:2px;}
.mm{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--muted);}
.ob{display:inline-block;font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:600;padding:3px 7px;border-radius:2px;background:var(--m2);color:var(--text);margin-top:3px;}
.cl{font-family:'IBM Plex Mono',monospace;font-size:9px;text-transform:uppercase;color:var(--muted);margin-bottom:2px;display:none;}
@media(max-width:750px){.cl{display:block;}}
.cv{font-family:'IBM Plex Mono',monospace;font-size:13px;}
.bn{font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;text-transform:uppercase;}
.rl{font-size:10px;color:var(--muted);font-family:'IBM Plex Mono',monospace;margin-top:2px;}
.edg{font-family:'IBM Plex Mono',monospace;font-size:15px;font-weight:700;color:var(--green);}
.edg.medium{color:var(--amber);}.edg.low{color:var(--muted);}
.lay-price{font-family:'IBM Plex Mono',monospace;font-size:13px;color:var(--blue);}
.no-arb{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--muted);}
.arb-badge{display:inline-flex;align-items:center;gap:4px;font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:700;padding:5px 10px;border-radius:2px;background:var(--bd);color:var(--blue);border:1px solid var(--bm);}
.sig{display:inline-flex;align-items:center;gap:4px;font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:700;text-transform:uppercase;padding:4px 9px;border-radius:2px;}
.sig.strong{background:var(--gd);color:var(--green);border:1px solid var(--gm);}
.sig.moderate{background:var(--ad);color:var(--amber);border:1px solid var(--amber);}
.sig.weak{background:var(--m2);color:var(--muted);border:1px solid var(--bb);}
.sig::before{content:'●';font-size:7px;}
.sp{display:inline-block;font-family:'IBM Plex Mono',monospace;font-size:9px;padding:2px 5px;border:1px solid var(--bb);border-radius:2px;color:var(--muted);margin-left:6px;}
.stbox{text-align:center;padding:50px 20px;border:1px dashed var(--bb);border-radius:4px;}
.sti{font-size:32px;margin-bottom:10px;}
.stt{font-family:'IBM Plex Mono',monospace;font-size:14px;font-weight:600;margin-bottom:6px;}
.sts{font-size:12px;color:var(--muted);max-width:340px;margin:0 auto;line-height:1.7;}
.spin{display:inline-block;width:18px;height:18px;border:2px solid var(--bb);border-top-color:var(--green);border-radius:50%;animation:spin .8s linear infinite;margin-bottom:10px;}
@keyframes spin{to{transform:rotate(360deg);}}
.err{background:var(--rd);border:1px solid var(--red);border-radius:4px;padding:14px;font-family:'IBM Plex Mono',monospace;font-size:12px;color:var(--red);line-height:1.7;}
footer{margin-top:32px;padding-top:14px;border-top:1px solid var(--border);font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--muted);}
.dots::after{content:'';animation:dt 1.2s infinite;}
@keyframes dt{0%,20%{content:'';}40%{content:'.';}60%{content:'..';}80%,100%{content:'...';}}
</style>
</head>
<body>
<header>
<div class="logo">Value<span>/</span>Edge</div>
<div class="sub">Odds anomaly + Betfair arb detector — auto email alerts active</div>
</header>
<div class="box">
<div class="note">Free API key from <a href="https://the-odds-api.com" target="_blank">the-odds-api.com</a> — 500 requests/month free.</div>
<div class="arb-legend">Blue rows = arb — back the bookie and lay on Betfair for guaranteed profit. Email alert sent automatically.</div>
<div><label>Odds API Key</label><input type="password" id="apiKey" placeholder="Paste your key here..."/></div>
<div class="row">
<div><label>Sport</label><select id="sport"><option value="soccer_epl">Premier League</option><option value="soccer_fa_cup">FA Cup</option><option value="soccer_efl_champ">Championship</option><option value="soccer_uefa_champs_league">Champions League</option><option value="soccer_spain_la_liga">La Liga</option><option value="soccer_italy_serie_a">Serie A</option><option value="soccer_germany_bundesliga">Bundesliga</option><option value="soccer_efl_league_one">League One</option></select></div>
<div><label>Min Edge %</label><input type="number" id="minEdge" value="3" min="1" max="50"/></div>
</div>
<div><label>Sharp reference</label><select id="ref"><option value="pinnacle">Pinnacle (recommended)</option><option value="betfair">Betfair Exchange</option><option value="average">Market Average</option></select></div>
<button class="btn" id="scanBtn" onclick="go()">Scan for Value Bets + Arbs</button>
</div>
<div class="sbar" id="sb">
<div class="stat"><div class="sl">Events</div><div class="sv" id="sM">-</div></div>
<div class="stat"><div class="sl">Value Bets</div><div class="sv" id="sV">-</div></div>
<div class="stat"><div class="sl">Avg Edge</div><div class="sv a" id="sE">-</div></div>
<div class="stat"><div class="sl">Best Edge</div><div class="sv" id="sB">-</div></div>
<div class="stat"><div class="sl">Arbs</div><div class="sv b" id="sA">-</div></div>
</div>
<div class="fbar" id="fb">
<button class="fb active" onclick="filt('all',this)">All</button>
<button class="fb arb-btn" onclick="filt('arb',this)">Arbs Only</button>
<button class="fb" onclick="filt('high',this)">Strong 10%+</button>
<button class="fb" onclick="filt('medium',this)">Moderate 5-10%</button>
<button class="fb" onclick="filt('low',this)">Weak</button>
</div>
<div class="ch" id="ch"><span>Match</span><span>Bookmaker</span><span>Back</span><span>Fair</span><span>BF Lay</span><span>Edge</span><span>Signal</span></div>
<div id="area"><div class="stbox"><div class="sti">&#128269;</div><div class="stt">Ready to scan</div><div class="sts">Enter your Odds API key and hit Scan. Auto email alerts active every 15 mins.</div></div></div>
<footer>The Odds API · BF Lay = back/0.95 · Auto-scan every 15 mins · Not financial advice</footer>
<script>
const S={soccer_epl:'Premier League',soccer_fa_cup:'FA Cup',soccer_efl_champ:'Championship',soccer_uefa_champs_league:'Champions League',soccer_spain_la_liga:'La Liga',soccer_italy_serie_a:'Serie A',soccer_germany_bundesliga:'Bundesliga',soccer_efl_league_one:'League One'};
let results=[],filter='all';
function sl(k){return S[k]||k;}
function fmt(d){return Number(d).toFixed(2);}
function fd(s){return new Date(s).toLocaleDateString('en-GB',{weekday:'short',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'});}
function slbl(e){return e>=10?'Strong':e>=5?'Moderate':'Weak';}
async function go(){
var key=document.getElementById('apiKey').value.trim();
var sport=document.getElementById('sport').value;
var minE=document.getElementById('minEdge').value||3;
var ref=document.getElementById('ref').value;
if(!key){showErr('Please enter your Odds API key.');return;}
var btn=document.getElementById('scanBtn');
btn.disabled=true;btn.textContent='Scanning...';
hide();showLoad('Fetching '+sl(sport)+' odds');
try{
var res=await fetch('/scan?apiKey='+encodeURIComponent(key)+'&sport='+sport+'&minEdge='+minE+'&ref='+ref);
var data=await res.json();
if(!res.ok||data.error)throw new Error(data.error||'Server error');
results=data.value_bets||[];
filter='all';
document.querySelectorAll('.fb').forEach(function(b,i){b.classList.toggle('active',i===0);});
render();stats(data.events_scanned);
}catch(e){showErr(e.message);}
finally{btn.disabled=false;btn.textContent='Scan for Value Bets + Arbs';}
}
function render(){
var area=document.getElementById('area');
var show=results;
if(filter==='arb')show=results.filter(function(r){return r.is_arb;});
else if(filter!=='all')show=results.filter(function(r){return r.signal===filter;});
if(!show.length){area.innerHTML='<div class="stbox"><div class="sti">&#128202;</div><div class="stt">No results</div><div class="sts">Try lowering min edge or switch to All.</div></div>';return;}
area.innerHTML=show.map(function(r){
var isArb=r.is_arb;
var cc=isArb?'arb':r.signal;
var sc=r.signal==='high'?'strong':r.signal==='medium'?'moderate':'weak';
var ec=r.signal!=='high'?r.signal:'';
var lh=r.bf_lay_price?'<div class="lay-price">'+fmt(r.bf_lay_price)+'</div>':'<div class="no-arb">-</div>';
var sh=isArb?'<div class="arb-badge">ARB</div>':'<div class="sig '+sc+'">'+slbl(r.edge)+'</div>';
return '<div class="card '+cc+'"><div><div class="mn">'+r.match+'<span class="sp">'+sl(r.sport)+'</span></div><div class="mm">'+fd(r.time)+'</div><div class="ob">'+r.outcome+'</div></div><div><div class="cl">Bookmaker</div><div class="bn">'+r.bookmaker+'</div><div class="rl">ref: '+r.ref_label+'</div></div><div><div class="cl">Back</div><div class="cv" style="color:var(--green);font-weight:600">'+fmt(r.odds)+'</div></div><div><div class="cl">Fair</div><div class="cv" style="color:var(--muted)">'+fmt(r.fair_odds)+'</div></div><div><div class="cl">BF Lay</div>'+lh+'</div><div><div class="cl">Edge</div><div class="edg '+ec+'">+'+Number(r.edge).toFixed(1)+'%</div></div><div><div class="cl">Signal</div>'+sh+'</div></div>';
}).join('');
document.getElementById('fb').classList.add('on');
document.getElementById('ch').classList.add('on');
}
function filt(t,b){filter=t;document.querySelectorAll('.fb').forEach(function(x){x.classList.remove('active');});b.classList.add('active');render();}
function stats(n){
document.getElementById('sb').classList.add('on');
document.getElementById('sM').textContent=n;
document.getElementById('sV').textContent=results.length;
var a=results.filter(function(r){return r.is_arb;}).length;
document.getElementById('sA').textContent=a;
if(results.length){
var avg=results.reduce(function(a,b){return a+b.edge;},0)/results.length;
document.getElementById('sE').textContent=avg.toFixed(1)+'%';
document.getElementById('sB').textContent='+'+results[0].edge.toFixed(1)+'%';
}}
function hide(){['sb','fb','ch'].forEach(function(id){document.getElementById(id).classList.remove('on');});}
function showLoad(m){document.getElementById('area').innerHTML='<div class="stbox"><div class="spin"></div><div class="stt dots">'+m+'</div><div class="sts">Fetching live odds...</div></div>';}
function showErr(m){document.getElementById('area').innerHTML='<div class="err">'+m+'</div>';}
</script>
</body>
</html>""", mimetype="text/html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    scanner = threading.Thread(target=auto_scan, daemon=True)
    scanner.start()
    app.run(host="0.0.0.0", port=port)
    
