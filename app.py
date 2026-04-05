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
BETFAIR_COMMISSION = 0.05

# ── Config ─────────────────────────────────────────────────────────
ODDS_API_KEY      = os.environ.get("ODDS_API_KEY", "")
GMAIL_USER        = "tony8judge@gmail.com"
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")  # set in Render env vars
ALERT_EMAIL       = "tony8judge@gmail.com"
SCAN_INTERVAL     = 300  # 5 minutes
HEALTH_CHECK_HOUR = 12.45   # Send daily "still alive" email at 12.45pm if no alerts fired

# UK leagues only — no cups, no European
UK_LEAGUES = [
    "soccer_epl",
    "soccer_efl_champ",
    "soccer_england_league1",
    "soccer_england_league2",
    "soccer_scotland_premiership",
]

# All markets available from UK bookmakers
MARKETS = [
    "h2h",                       # Match Result 1X2
    "totals",                    # Over/Under Goals
    "btts",                      # Both Teams to Score
    "draw_no_bet",               # Draw No Bet
    "double_chance",             # Double Chance
    "h2h_h1",                    # 1st Half Result
    "totals_h1",                 # 1st Half Over/Under
    "alternate_totals_corners",  # Corners Over/Under
    "alternate_totals_cards",    # Cards / Bookings Over/Under
]

# UK fixed-odds bookmakers only (no exchanges as back bets)
UK_BOOKMAKERS = [
    "sport888", "betfair_sb_uk", "betvictor", "betway", "boylesports",
    "casumo", "coral", "grosvenor", "ladbrokes_uk", "leovegas",
    "livescorebet", "paddypower", "skybet", "unibet_uk", "virginbet",
    "williamhill",
]

BOOK_LABELS = {
    "sport888": "888sport", "betfair_sb_uk": "Betfair SB",
    "betvictor": "Bet Victor", "betway": "Betway",
    "boylesports": "BoyleSports", "casumo": "Casumo",
    "coral": "Coral", "grosvenor": "Grosvenor",
    "ladbrokes_uk": "Ladbrokes", "leovegas": "LeoVegas",
    "livescorebet": "LiveScore Bet", "paddypower": "Paddy Power",
    "skybet": "Sky Bet", "unibet_uk": "Unibet",
    "virginbet": "Virgin Bet", "williamhill": "William Hill",
}

LEAGUE_LABELS = {
    "soccer_epl": "Premier League",
    "soccer_efl_champ": "Championship",
    "soccer_england_league1": "League One",
    "soccer_england_league2": "League Two",
    "soccer_scotland_premiership": "Scottish Prem",
}

MARKET_LABELS = {
    "h2h": "Match Result (1X2)",
    "totals": "Over/Under Goals",
    "btts": "Both Teams to Score",
    "draw_no_bet": "Draw No Bet",
    "double_chance": "Double Chance",
    "h2h_h1": "1st Half Result",
    "totals_h1": "1st Half Over/Under",
    "alternate_totals_corners": "Corners Over/Under",
    "alternate_totals_cards": "Cards / Bookings Over/Under",
}

already_alerted   = set()
last_alert_date   = None   # tracks last date an alert email was sent
health_check_sent = None   # tracks last date a health check email was sent
credits_used_today = 0     # running total of API credits used today
credits_today_date = None  # date the counter was last reset


# ── Email ──────────────────────────────────────────────────────────
def send_email(arbs, value_bets):
    total = len(arbs) + len(value_bets)
    if total == 0:
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Value/Edge Alert — {len(arbs)} arb(s), {len(value_bets)} value bet(s)"
        msg["From"]    = GMAIL_USER
        msg["To"]      = ALERT_EMAIL

        lines = ["VALUE/EDGE — UK FOOTBALL ALERT\n"]

        if arbs:
            lines.append("=" * 40)
            lines.append(f"ARB OPPORTUNITIES ({len(arbs)})")
            lines.append("=" * 40)
            for a in arbs:
                lines.append(f"Match:     {a['match']} [{a['league']}]")
                lines.append(f"Market:    {a['market_label']}")
                lines.append(f"Selection: {a['selection']}")
                lines.append(f"Back:      {a['odds']} @ {a['bookmaker']}")
                lines.append(f"BF Lay:    {a['bf_lay_price']}")
                lines.append(f"Edge:      +{a['edge']}%")
                lines.append("")

        if value_bets:
            lines.append("=" * 40)
            lines.append(f"STRONG VALUE BETS ({len(value_bets)})")
            lines.append("=" * 40)
            for v in value_bets:
                lines.append(f"Match:     {v['match']} [{v['league']}]")
                lines.append(f"Market:    {v['market_label']}")
                lines.append(f"Selection: {v['selection']}")
                lines.append(f"Back:      {v['odds']} @ {v['bookmaker']}")
                lines.append(f"Fair:      {v['fair_odds']} (Pinnacle)")
                lines.append(f"Edge:      +{v['edge']}%")
                lines.append("")

        lines.append("Not financial advice. 18+ BeGambleAware.org")
        msg.attach(MIMEText("\n".join(lines), "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, ALERT_EMAIL, msg.as_string())
        print(f"Alert sent: {len(arbs)} arbs, {len(value_bets)} value bets")
    except Exception as e:
        print(f"Email error: {e}")


# ── Outcome key helpers ────────────────────────────────────────────
def oc_key(mkt_key, oc):
    """Unique key for an outcome — includes line for totals/corners/cards."""
    if any(x in mkt_key for x in ["totals", "corners", "cards"]):
        return f"{oc['name']}_{oc.get('point', '')}"
    return oc["name"]


def fmt_outcome(mkt_key, oc):
    """Human-readable outcome label."""
    if any(x in mkt_key for x in ["totals", "corners", "cards"]):
        return f"{oc['name']} {oc.get('point', '')}".strip()
    if mkt_key == "double_chance":
        return {"Home/Draw": "1X", "Away/Draw": "X2", "Home/Away": "12"}.get(oc["name"], oc["name"])
    return oc["name"]


# ── Remove vig from Pinnacle prices ───────────────────────────────
def remove_vig(outcomes):
    """Return fair prices with overround removed."""
    total_prob = sum(1 / o["price"] for o in outcomes)
    return {
        oc_key("h2h", o): round(1 / ((1 / o["price"]) / total_prob), 3)
        for o in outcomes
    }


# ── Core analysis ─────────────────────────────────────────────────
def analyse(events, min_edge, selected_markets):
    results = []

    for ev in events:
        books = ev.get("bookmakers", [])
        league = LEAGUE_LABELS.get(ev.get("sport_key", ""), ev.get("sport_key", ""))
        match_name = f"{ev['home_team']} v {ev['away_team']}"
        kickoff = ev.get("commence_time", "")

        # 1. Build Pinnacle fair prices per market
        pinnacle = next((b for b in books if b["key"] == "pinnacle"), None)
        pinn_prices = {}  # mkt_key → { oc_key → fair_price }
        if pinnacle:
            for mkt in pinnacle.get("markets", []):
                outcomes = mkt.get("outcomes", [])
                if not outcomes:
                    continue
                total_prob = sum(1 / o["price"] for o in outcomes)
                pinn_prices[mkt["key"]] = {
                    oc_key(mkt["key"], o): round(1 / ((1 / o["price"]) / total_prob), 3)
                    for o in outcomes
                }

        # 2. Build Betfair Exchange lay prices per market
        bf = next((b for b in books if b["key"] == "betfair_ex_uk"), None)
        bf_lay = {}  # mkt_key → { oc_key → lay_price }
        if bf:
            for mkt in bf.get("markets", []):
                is_lay = mkt["key"].endswith("_lay")
                base = mkt["key"][:-4] if is_lay else mkt["key"]
                if base not in bf_lay:
                    bf_lay[base] = {}
                for o in mkt.get("outcomes", []):
                    k = oc_key(base, o)
                    if is_lay:
                        bf_lay[base][k] = o["price"]
                    elif k not in bf_lay[base]:
                        # Approximate lay from BF back price
                        bf_lay[base][k] = round(o["price"] / (1 - BETFAIR_COMMISSION), 2)

        # 3. Score each UK fixed-odds bookmaker
        for bk in books:
            if bk["key"] not in UK_BOOKMAKERS:
                continue
            bk_label = BOOK_LABELS.get(bk["key"], bk["key"])

            for mkt in bk.get("markets", []):
                mkt_key = mkt["key"]
                if mkt_key not in selected_markets:
                    continue
                fair_map = pinn_prices.get(mkt_key)
                if not fair_map:
                    continue  # No Pinnacle price for this market

                for o in mkt.get("outcomes", []):
                    back_odds = o["price"]
                    k = oc_key(mkt_key, o)
                    fair_odds = fair_map.get(k)
                    if not fair_odds:
                        continue

                    edge = round(((back_odds / fair_odds) - 1) * 100, 2)
                    if edge < min_edge:
                        continue

                    lay_price = (bf_lay.get(mkt_key) or {}).get(k)
                    is_arb = (lay_price is not None and
                              back_odds > lay_price)

                    results.append({
                        "match":        match_name,
                        "league":       league,
                        "time":         kickoff,
                        "market_key":   mkt_key,
                        "market_label": MARKET_LABELS.get(mkt_key, mkt_key),
                        "selection":    fmt_outcome(mkt_key, o),
                        "bookmaker":    bk_label,
                        "odds":         round(back_odds, 2),
                        "fair_odds":    fair_odds,
                        "bf_lay_price": lay_price,
                        "is_arb":       is_arb,
                        "edge":         edge,
                        "signal":       "high" if edge >= 10 else "medium" if edge >= 5 else "low",
                    })

    results.sort(key=lambda x: (not x["is_arb"], -x["edge"]))
    return results


# ── Health check email ─────────────────────────────────────────────
def send_health_check(events_scanned, quota_remaining, quota_used_today):
    global health_check_sent
    today = time.strftime("%Y-%m-%d")
    if health_check_sent == today:
        return  # Already sent today
    try:
        # Work out when credits renew — same day next month
        now = time.localtime()
        renew_month = now.tm_mon + 1 if now.tm_mon < 12 else 1
        renew_year  = now.tm_year if now.tm_mon < 12 else now.tm_year + 1
        try:
            import calendar
            max_day = calendar.monthrange(renew_year, renew_month)[1]
            renew_day = min(now.tm_mday, max_day)
            renew_date = f"{renew_day:02d}/{renew_month:02d}/{renew_year}"
        except Exception:
            renew_date = "next month"

        msg = MIMEMultipart("alternative")
        msg["Subject"] = "✅ Value/Edge — Daily Health Check (10am)"
        msg["From"]    = GMAIL_USER
        msg["To"]      = ALERT_EMAIL
        body = (
            "VALUE/EDGE — DAILY HEALTH CHECK\n\n"
            f"Time:              {time.strftime('%A %d %B %Y, %H:%M')}\n"
            f"Status:            ✅ Scanner running normally\n"
            f"Events scanned:    {events_scanned}\n\n"
            "── API CREDITS ──────────────────────\n"
            f"Remaining:         {quota_remaining}\n"
            f"Used today:        {quota_used_today}\n"
            f"Renews on:         {renew_date}\n\n"
            "── LEAGUES MONITORED ────────────────\n"
            "  · Premier League\n"
            "  · Championship\n"
            "  · League One\n"
            "  · League Two\n"
            "  · Scottish Premiership\n\n"
            "You will receive an alert as soon as an arb or 10%+ edge is found.\n\n"
            "Not financial advice. 18+ BeGambleAware.org"
        )
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, ALERT_EMAIL, msg.as_string())
        health_check_sent = today
        print(f"Health check sent for {today}")
    except Exception as e:
        print(f"Health check email error: {e}")


# ── Auto-scan background thread ────────────────────────────────────
def auto_scan():
    global last_alert_date, health_check_sent, credits_used_today, credits_today_date

    while True:
        try:
            if ODDS_API_KEY:
                all_arbs     = []
                all_strong   = []
                total_events = 0
                quota_left   = "?"
                market_param = ",".join(MARKETS)
                now          = time.localtime()
                today        = time.strftime("%Y-%m-%d")

                # Reset daily credit counter at midnight
                if credits_today_date != today:
                    credits_used_today = 0
                    credits_today_date = today

                for league in UK_LEAGUES:
                    url = (
                        f"{ODDS_API_BASE}/sports/{league}/odds/"
                        f"?apiKey={ODDS_API_KEY}"
                        f"&regions=uk,eu"
                        f"&markets={market_param}"
                        f"&oddsFormat=decimal"
                    )
                    r = requests.get(url, timeout=15)
                    if not r.ok:
                        print(f"Auto-scan skip {league}: {r.status_code}")
                        continue

                    # Track credits
                    rem  = r.headers.get("x-requests-remaining", "?")
                    used = r.headers.get("x-requests-last", "0")
                    if rem != "?":
                        quota_left = rem
                    try:
                        credits_used_today += int(used)
                    except (ValueError, TypeError):
                        pass

                    data = r.json()
                    if not isinstance(data, list):
                        continue
                    total_events += len(data)

                    for result in analyse(data, 1, MARKETS):
                        alert_key = (
                            f"{result['match']}_{result['selection']}"
                            f"_{result['bookmaker']}_{result['market_key']}"
                        )
                        if alert_key in already_alerted:
                            continue
                        if result["is_arb"]:
                            all_arbs.append(result)
                            already_alerted.add(alert_key)
                        elif result["edge"] >= 10:
                            all_strong.append(result)
                            already_alerted.add(alert_key)

                # Send alert email if new arbs or strong value found
                if all_arbs or all_strong:
                    send_email(all_arbs, all_strong)
                    last_alert_date = today

                # Send daily 12.45pm health check
                if (now.tm_hour == HEALTH_CHECK_HOUR and
                        now.tm_min < 11 and
                        health_check_sent != today):
                    send_health_check(total_events, quota_left, credits_used_today)

        except Exception as e:
            print(f"Auto-scan error: {e}")

        time.sleep(SCAN_INTERVAL)


# ── /scan endpoint (called by frontend) ───────────────────────────
@app.route("/scan")
def scan():
    api_key    = request.args.get("apiKey", "").strip()
    league     = request.args.get("league", "soccer_epl")
    min_edge   = float(request.args.get("minEdge", 3))
    markets_in = request.args.get("markets", "h2h,totals,btts")
    selected   = [m.strip() for m in markets_in.split(",") if m.strip()]

    if not api_key:
        return jsonify({"error": "No API key provided"}), 400
    if league not in LEAGUE_LABELS:
        return jsonify({"error": "Invalid league"}), 400

    market_param = ",".join(selected)
    url = (
        f"{ODDS_API_BASE}/sports/{league}/odds/"
        f"?apiKey={api_key}"
        f"&regions=uk,eu"
        f"&markets={market_param}"
        f"&oddsFormat=decimal"
    )
    try:
        r = requests.get(url, timeout=15)
        remaining = r.headers.get("x-requests-remaining", "?")
        used      = r.headers.get("x-requests-last", "?")
        if not r.ok:
            msg = r.json().get("message", f"API error {r.status_code}")
            return jsonify({"error": msg}), r.status_code
        data = r.json()
        if not isinstance(data, list):
            return jsonify({"error": "Unexpected API response"}), 500
        return jsonify({
            "events_scanned":    len(data),
            "value_bets":        analyse(data, min_edge, selected),
            "quota_remaining":   remaining,
            "quota_used":        used,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Frontend ───────────────────────────────────────────────────────
@app.route("/")
def index():
    return Response("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Value Bet Finder – UK Football</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap');
:root{
  --bg:#0a0d12;--surf:#111520;--surf2:#171c2a;--bord:#1e2640;
  --acc:#00e5a0;--acc2:#0099ff;--arbbg:#00112a;
  --txt:#c8d0e0;--dim:#566080;--bright:#eef2ff;
  --strong:#22ff99;--mod:#ffcc00;--weak:#8899bb;
  --mono:'IBM Plex Mono',monospace;--sans:'IBM Plex Sans',sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:var(--sans);font-size:14px;min-height:100vh}
body::before{content:'';position:fixed;inset:0;background-image:linear-gradient(rgba(0,229,160,.013)1px,transparent 1px),linear-gradient(90deg,rgba(0,229,160,.013)1px,transparent 1px);background-size:40px 40px;pointer-events:none;z-index:0}
.wrap{position:relative;z-index:1;max-width:1180px;margin:0 auto;padding:20px 14px}
header{display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-bottom:18px;padding-bottom:14px;border-bottom:1px solid var(--bord)}
.logo{font-family:var(--mono);font-size:20px;font-weight:700;color:var(--acc);letter-spacing:-.5px}
.logo em{color:var(--dim);font-style:normal}
.tagline{font-size:11px;color:var(--dim);font-family:var(--mono);margin-top:2px}
.badges{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.badge{font-family:var(--mono);font-size:10px;padding:3px 8px;border-radius:3px;border:1px solid;letter-spacing:.3px}
.bg{border-color:var(--acc);color:var(--acc);background:rgba(0,229,160,.07)}
.bb{border-color:var(--acc2);color:var(--acc2);background:rgba(0,153,255,.07)}
.bd{border-color:var(--bord);color:var(--dim)}
.dot{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--acc);margin-right:4px;animation:pulse 2s infinite;vertical-align:middle}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.panel{background:var(--surf);border:1px solid var(--bord);border-radius:6px;padding:16px;margin-bottom:12px}
.ptitle{font-family:var(--mono);font-size:10px;letter-spacing:1.2px;text-transform:uppercase;color:var(--dim);margin-bottom:12px;padding-bottom:7px;border-bottom:1px solid var(--bord)}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.g4{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:12px}
@media(max-width:660px){.g2,.g4{grid-template-columns:1fr 1fr}}
@media(max-width:400px){.g2,.g4{grid-template-columns:1fr}}
.flabel{font-family:var(--mono);font-size:10px;color:var(--dim);letter-spacing:.9px;text-transform:uppercase;margin-bottom:5px;display:block}
input,select{width:100%;background:var(--surf2);border:1px solid var(--bord);color:var(--bright);font-family:var(--mono);font-size:13px;padding:8px 11px;border-radius:4px;outline:none;transition:border-color .2s;-webkit-appearance:none;appearance:none}
input:focus,select:focus{border-color:var(--acc)}
.hint{font-size:11px;color:var(--dim);font-family:var(--mono);margin-top:5px}
.hint a{color:var(--acc2);text-decoration:none}
select option{background:#111520}
.chips{display:flex;flex-wrap:wrap;gap:5px;margin-top:4px}
.chip,.mchip{font-family:var(--mono);font-size:11px;padding:4px 11px;border-radius:3px;border:1px solid var(--bord);background:var(--surf2);cursor:pointer;transition:all .15s;user-select:none;white-space:nowrap;color:var(--dim)}
.chip:hover,.chip.on{border-color:var(--acc);color:var(--acc)}
.chip.on{background:rgba(0,229,160,.1)}
.mchip:hover,.mchip.on{border-color:var(--acc2);color:var(--acc2)}
.mchip.on{background:rgba(0,153,255,.1)}
.infobox{font-family:var(--mono);font-size:11px;line-height:1.6;color:var(--dim);background:rgba(0,153,255,.04);border:1px solid rgba(0,153,255,.14);border-radius:4px;padding:10px 13px;margin-top:10px}
.infobox b{color:var(--acc2)}
.scanbtn{width:100%;margin-top:14px;padding:13px;background:var(--acc);color:#000;font-family:var(--mono);font-size:14px;font-weight:700;letter-spacing:1px;border:none;border-radius:4px;cursor:pointer;transition:all .2s;text-transform:uppercase}
.scanbtn:hover{background:#00ffb0;transform:translateY(-1px)}
.scanbtn:disabled{background:var(--dim);cursor:not-allowed;transform:none}
.prog{height:2px;background:var(--bord);border-radius:1px;overflow:hidden;margin:10px 0 4px;display:none}
.progfill{height:100%;background:linear-gradient(90deg,var(--acc),var(--acc2));width:0%;transition:width .4s}
.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:12px}
@media(max-width:540px){.stats{grid-template-columns:repeat(3,1fr)}}
.scard{background:var(--surf);border:1px solid var(--bord);border-radius:5px;padding:11px;text-align:center}
.sval{font-family:var(--mono);font-size:20px;font-weight:700;color:var(--bright);display:block;line-height:1;margin-bottom:3px}
.svg{color:var(--acc)}.svb{color:var(--acc2)}
.slbl{font-family:var(--mono);font-size:9px;color:var(--dim);letter-spacing:.8px;text-transform:uppercase}
.ftabs{display:flex;gap:4px;margin-bottom:10px;flex-wrap:wrap}
.ftab{font-family:var(--mono);font-size:11px;padding:5px 11px;border-radius:3px;border:1px solid var(--bord);background:var(--surf);color:var(--dim);cursor:pointer;transition:all .15s}
.ftab:hover{border-color:var(--dim);color:var(--txt)}
.ftab.on{border-color:var(--acc);color:var(--acc);background:rgba(0,229,160,.08)}
.reswrap{background:var(--surf);border:1px solid var(--bord);border-radius:6px;overflow:hidden}
table{width:100%;border-collapse:collapse}
thead tr{background:var(--surf2)}
th{font-family:var(--mono);font-size:10px;letter-spacing:.9px;text-transform:uppercase;color:var(--dim);padding:9px 12px;text-align:left;border-bottom:1px solid var(--bord);white-space:nowrap}
td{padding:9px 12px;border-bottom:1px solid rgba(30,38,64,.5);font-size:13px;vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,.016)}
tr.arb td{background:var(--arbbg)!important}
tr.arb td:first-child{border-left:3px solid var(--acc2)}
.mname{font-weight:500;color:var(--bright);font-size:13px}
.mtag{font-family:var(--mono);font-size:9px;color:var(--dim);display:block;margin-top:1px}
.mktlbl{font-family:var(--mono);font-size:9px;color:var(--acc2);display:block;margin-top:1px}
.bkname{font-family:var(--mono);font-size:12px;color:var(--txt)}
.oddc{font-family:var(--mono);font-size:13px;font-weight:600;color:var(--bright)}
.fairo{font-family:var(--mono);font-size:13px;color:var(--dim)}
.layo{font-family:var(--mono);font-size:13px}
.edgec{font-family:var(--mono);font-size:13px;font-weight:700}
.es{color:var(--strong)}.em{color:var(--mod)}.ew{color:var(--weak)}
.sig{font-family:var(--mono);font-size:10px;padding:3px 7px;border-radius:3px;white-space:nowrap}
.sarb{background:rgba(0,68,204,.25);color:#6699ff;border:1px solid rgba(0,68,204,.5)}
.ss{background:rgba(34,255,153,.1);color:var(--strong);border:1px solid rgba(34,255,153,.3)}
.sm{background:rgba(255,204,0,.08);color:var(--mod);border:1px solid rgba(255,204,0,.3)}
.sw{background:rgba(136,153,187,.07);color:var(--weak);border:1px solid rgba(136,153,187,.2)}
.empty{padding:54px 20px;text-align:center}
.emico{font-size:30px;margin-bottom:10px;opacity:.35}
.emmsg{font-family:var(--mono);font-size:13px;color:var(--dim)}
.errmsg{font-family:var(--mono);font-size:12px;color:#ff6b6b;padding:11px 14px;background:rgba(255,60,60,.07);border:1px solid rgba(255,60,60,.2);border-radius:4px;margin-bottom:12px;display:none}
.quotabar{font-family:var(--mono);font-size:11px;color:var(--dim);text-align:right;margin-bottom:8px;display:none}
.quotabar span{color:var(--acc)}
footer{margin-top:18px;padding-top:13px;border-top:1px solid var(--bord);font-family:var(--mono);font-size:10px;color:var(--dim);display:flex;gap:14px;flex-wrap:wrap}
</style>
</head>
<body>
<div class="wrap">

<header>
  <div>
    <div class="logo">VALUE<em>/</em>EDGE <em style="font-size:13px">· UK Football</em></div>
    <div class="tagline">Betfair arb + value detector · UK leagues · Pinnacle sharp reference · auto email alerts</div>
  </div>
  <div class="badges">
    <span class="badge bg"><span class="dot"></span>AUTO-SCAN</span>
    <span class="badge bb">BF ARB</span>
    <span class="badge bd">UK BOOKS</span>
    <span class="badge bd">PINNACLE REF</span>
  </div>
</header>

<div class="errmsg" id="errMsg"></div>

<div class="panel">
  <div class="ptitle">API &amp; Settings</div>
  <div class="g2" style="margin-bottom:12px">
    <div>
      <label class="flabel">Odds API Key</label>
      <input type="password" id="apiKey" placeholder="Paste key from the-odds-api.com"/>
      <div class="hint">Free key → <a href="https://the-odds-api.com" target="_blank">the-odds-api.com</a> · 500 requests/month</div>
    </div>
    <div>
      <label class="flabel">Min Edge %</label>
      <input type="number" id="minEdge" value="3" min="0" max="50" step="0.5"/>
      <div class="hint">Edge vs Pinnacle fair price · arbs always shown regardless</div>
    </div>
  </div>
</div>

<div class="panel">
  <div class="ptitle">Leagues</div>
  <div class="chips">
    <div class="chip on" data-key="soccer_epl">⚽ Premier League</div>
    <div class="chip on" data-key="soccer_efl_champ">Championship</div>
    <div class="chip on" data-key="soccer_england_league1">League One</div>
    <div class="chip on" data-key="soccer_england_league2">League Two</div>
    <div class="chip"    data-key="soccer_scotland_premiership">🏴󠁧󠁢󠁳󠁣󠁴󠁿 Scottish Prem</div>
  </div>
</div>

<div class="panel">
  <div class="ptitle">Markets</div>
  <div class="chips">
    <div class="mchip on"  data-key="h2h">Match Result (1X2)</div>
    <div class="mchip on"  data-key="totals">Over/Under Goals</div>
    <div class="mchip on"  data-key="btts">Both Teams to Score</div>
    <div class="mchip"     data-key="draw_no_bet">Draw No Bet</div>
    <div class="mchip"     data-key="double_chance">Double Chance</div>
    <div class="mchip"     data-key="h2h_h1">1st Half Result</div>
    <div class="mchip"     data-key="totals_h1">1st Half Over/Under</div>
    <div class="mchip"     data-key="alternate_totals_corners">Corners Over/Under</div>
    <div class="mchip"     data-key="alternate_totals_cards">Cards / Bookings O/U</div>
  </div>
  <div class="infobox">
    <b>Sharp reference:</b> Pinnacle fetched via EU region — used only to calculate fair odds, never shown as a bet. All back bets are UK fixed-odds bookmakers only. Blue rows = confirmed arb vs Betfair Exchange.
  </div>
</div>

<button class="scanbtn" id="scanBtn" onclick="runScan()">⚡ Scan for Value Bets + Arbs</button>
<div class="prog" id="progBar"><div class="progfill" id="progFill"></div></div>

<div class="quotabar" id="quotaBar">
  API credits remaining: <span id="quotaRem">—</span> &nbsp;·&nbsp; used this call: <span id="quotaUsed">—</span>
</div>

<div class="stats">
  <div class="scard"><span class="sval" id="sEvents">—</span><span class="slbl">Events</span></div>
  <div class="scard"><span class="sval svg" id="sValue">—</span><span class="slbl">Value Bets</span></div>
  <div class="scard"><span class="sval" id="sAvg">—</span><span class="slbl">Avg Edge</span></div>
  <div class="scard"><span class="sval svg" id="sBest">—</span><span class="slbl">Best Edge</span></div>
  <div class="scard"><span class="sval svb" id="sArbs">—</span><span class="slbl">Arbs</span></div>
</div>

<div class="ftabs">
  <div class="ftab on"  onclick="setFilter('all',this)">All</div>
  <div class="ftab"     onclick="setFilter('arb',this)">Arbs Only</div>
  <div class="ftab"     onclick="setFilter('strong',this)">Strong 10%+</div>
  <div class="ftab"     onclick="setFilter('moderate',this)">Moderate 5–10%</div>
  <div class="ftab"     onclick="setFilter('weak',this)">Weak &lt;5%</div>
</div>

<div class="reswrap">
  <table>
    <thead>
      <tr>
        <th>Match</th><th>Market</th><th>Selection</th>
        <th>Bookmaker</th><th>Back</th><th>Fair (Pinnacle)</th>
        <th>BF Lay</th><th>Edge</th><th>Signal</th>
      </tr>
    </thead>
    <tbody id="resultsBody">
      <tr><td colspan="9">
        <div class="empty"><div class="emico">🔍</div>
        <div class="emmsg">Enter your API key and hit Scan</div></div>
      </td></tr>
    </tbody>
  </table>
</div>

<footer>
  <span>The Odds API · regions=uk,eu · Pinnacle as sharp ref · BF Lay ≈ back÷0.95</span>
  <span>Auto email alerts every 5 mins server-side · Daily 10am health check · Not financial advice · 18+ BeGambleAware.org</span>
</footer>
</div>

<script>
let allResults=[], currentFilter='all';

document.querySelectorAll('.chip,.mchip').forEach(c=>{
  c.addEventListener('click',()=>c.classList.toggle('on'));
});

function selLeagues(){ return [...document.querySelectorAll('.chip.on')].map(c=>c.dataset.key); }
function selMarkets(){ return [...document.querySelectorAll('.mchip.on')].map(c=>c.dataset.key); }

function setFilter(f,el){
  currentFilter=f;
  document.querySelectorAll('.ftab').forEach(t=>t.classList.remove('on'));
  el.classList.add('on');
  renderTable();
}

async function runScan(){
  const apiKey=document.getElementById('apiKey').value.trim();
  if(!apiKey){ return showErr('Please enter your Odds API key.'); }
  const leagues=selLeagues();
  if(!leagues.length){ return showErr('Please select at least one league.'); }
  const markets=selMarkets();
  if(!markets.length){ return showErr('Please select at least one market.'); }

  clearErr(); setBtn(true); showProg(true); setProg(0);
  allResults=[];
  let totalEvents=0;

  try{
    const mktParam=markets.join(',');
    for(let i=0;i<leagues.length;i++){
      setProg(Math.round(((i+0.5)/leagues.length)*90));
      const url=`/scan?apiKey=${encodeURIComponent(apiKey)}&league=${leagues[i]}&minEdge=1&markets=${mktParam}`;
      const resp=await fetch(url);
      const data=await resp.json();
      if(!resp.ok||data.error) throw new Error(data.error||'Server error');
      totalEvents+=data.events_scanned||0;
      allResults.push(...(data.value_bets||[]));
      if(data.quota_remaining){
        document.getElementById('quotaBar').style.display='block';
        document.getElementById('quotaRem').textContent=data.quota_remaining;
        document.getElementById('quotaUsed').textContent=data.quota_used||'?';
      }
    }
    // Filter client-side by user's min edge
    const minEdge=parseFloat(document.getElementById('minEdge').value)||3;
    allResults=allResults.filter(r=>r.edge>=minEdge||r.is_arb);
    allResults.sort((a,b)=>(b.is_arb-a.is_arb)||b.edge-a.edge);
    setProg(100);
    updateStats(totalEvents);
    renderTable();
  }catch(e){
    showErr(e.message||'Scan failed — check your API key.');
  }finally{
    setBtn(false);
    setTimeout(()=>{showProg(false);setProg(0);},600);
  }
}

function renderTable(){
  const tbody=document.getElementById('resultsBody');
  const filtered=allResults.filter(r=>{
    if(currentFilter==='arb') return r.is_arb;
    if(currentFilter==='strong') return r.edge>=10;
    if(currentFilter==='moderate') return r.edge>=5&&r.edge<10;
    if(currentFilter==='weak') return r.edge<5;
    return true;
  });
  if(!filtered.length){
    tbody.innerHTML='<tr><td colspan="9"><div class="empty"><div class="emico">📭</div><div class="emmsg">No bets match this filter</div></div></td></tr>';
    return;
  }
  tbody.innerHTML=filtered.map(r=>{
    const ec=r.edge>=10?'es':r.edge>=5?'em':'ew';
    const sig=r.is_arb
      ?'<span class="sig sarb">⚡ ARB</span>'
      :r.edge>=10?'<span class="sig ss">STRONG</span>'
      :r.edge>=5 ?'<span class="sig sm">MODERATE</span>'
      :'<span class="sig sw">WEAK</span>';
    const lay=r.bf_lay_price
      ?`<span class="layo" style="${r.is_arb?'color:#6699ff':''}">${r.bf_lay_price.toFixed(2)}</span>`
      :'<span style="color:var(--dim)">—</span>';
    return `<tr class="${r.is_arb?'arb':''}">
      <td><div class="mname">${r.match}</div><span class="mtag">${r.league} · ${new Date(r.time).toLocaleDateString('en-GB',{weekday:'short',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})}</span></td>
      <td><span class="mktlbl">${r.market_label}</span></td>
      <td><span style="font-family:var(--mono);font-size:12px;color:var(--bright)">${r.selection}</span></td>
      <td><span class="bkname">${r.bookmaker}</span></td>
      <td><span class="oddc">${r.odds.toFixed(2)}</span></td>
      <td><span class="fairo">${r.fair_odds.toFixed(2)}</span></td>
      <td>${lay}</td>
      <td><span class="edgec ${ec}">+${r.edge.toFixed(1)}%</span></td>
      <td>${sig}</td>
    </tr>`;
  }).join('');
}

function updateStats(n){
  document.getElementById('sEvents').textContent=n;
  document.getElementById('sValue').textContent=allResults.length;
  document.getElementById('sArbs').textContent=allResults.filter(r=>r.is_arb).length;
  if(allResults.length){
    const avg=allResults.reduce((s,r)=>s+r.edge,0)/allResults.length;
    const best=Math.max(...allResults.map(r=>r.edge));
    document.getElementById('sAvg').textContent='+'+avg.toFixed(1)+'%';
    document.getElementById('sBest').textContent='+'+best.toFixed(1)+'%';
  }
}

function setBtn(on){ const b=document.getElementById('scanBtn'); b.disabled=on; b.textContent=on?'⏳ Scanning…':'⚡ Scan for Value Bets + Arbs'; }
function showProg(on){ document.getElementById('progBar').style.display=on?'block':'none'; }
function setProg(p){ document.getElementById('progFill').style.width=p+'%'; }
function showErr(m){ const e=document.getElementById('errMsg'); e.textContent='⚠ '+m; e.style.display='block'; }
function clearErr(){ const e=document.getElementById('errMsg'); e.style.display='none'; e.textContent=''; }
</script>
</body>
</html>""", mimetype="text/html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    scanner = threading.Thread(target=auto_scan, daemon=True)
    scanner.start()
    app.run(host="0.0.0.0", port=port)
