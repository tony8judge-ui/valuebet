from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import os

app = Flask(__name__)
CORS(app)  # Allow requests from anywhere (your phone, browser, etc.)

ODDS_API_BASE = "https://api.the-odds-api.com/v4"

BOOKMAKERS = "pinnacle,betfair_ex_best_odds,bet365,williamhill,ladbrokes,skybet,paddypower,coral,betvictor,unibet,betway"

def remove_vig(probs):
    total = sum(probs)
    return [p / total for p in probs]

def analyse(events, min_edge, ref):
    results = []

    for ev in events:
        books = ev.get("bookmakers", [])

        # Find reference bookmaker (sharp line)
        ref_book = None
        if ref == "pinnacle":
            ref_book = next((b for b in books if b["key"] == "pinnacle"), None)
        elif ref == "betfair":
            ref_book = next((b for b in books if b["key"] == "betfair_ex_best_odds"), None)

        # Fall back to market average
        if not ref_book or ref == "average":
            ref_book = build_average(books)

        if not ref_book:
            continue

        ref_market = next((m for m in ref_book.get("markets", []) if m["key"] == "h2h"), None)
        if not ref_market or not ref_market.get("outcomes"):
            continue

        outcomes = ref_market["outcomes"]
        probs = [1 / o["price"] for o in outcomes]
        fair_probs = remove_vig(probs)
        fair_map = {o["name"]: 1 / fp for o, fp in zip(outcomes, fair_probs)}
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
                results.append({
                    "match": f"{ev['home_team']} v {ev['away_team']}",
                    "sport": ev["sport_key"],
                    "time": ev["commence_time"],
                    "outcome": o["name"],
                    "bookmaker": book.get("title", book["key"]),
                    "odds": round(o["price"], 2),
                    "fair_odds": round(fair, 2),
                    "edge": round(edge, 2),
                    "ref_label": ref_label,
                    "signal": "high" if edge >= 10 else "medium" if edge >= 5 else "low"
                })

    results.sort(key=lambda x: x["edge"], reverse=True)
    return results

def build_average(books):
    outcome_map = {}
    for b in books:
        market = next((m for m in b.get("markets", []) if m["key"] == "h2h"), None)
        if not market:
            continue
        for o in market.get("outcomes", []):
            outcome_map.setdefault(o["name"], []).append(o["price"])
    outcomes = [{"name": k, "price": sum(v) / len(v)} for k, v in outcome_map.items()]
    return {"key": "average", "title": "Market Average", "markets": [{"key": "h2h", "outcomes": outcomes}]}


@app.route("/scan")
def scan():
    api_key = request.args.get("apiKey", "").strip()
    sport   = request.args.get("sport", "soccer_epl")
    min_edge = float(request.args.get("minEdge", 5))
    ref     = request.args.get("ref", "pinnacle")

    if not api_key:
        return jsonify({"error": "No API key provided"}), 400

    url = (
        f"{ODDS_API_BASE}/sports/{sport}/odds/"
        f"?apiKey={api_key}&regions=uk&markets=h2h"
        f"&oddsFormat=decimal&bookmakers={BOOKMAKERS}"
    )

    try:
        r = requests.get(url, timeout=15)
        if not r.ok:
            msg = r.json().get("message", f"API error {r.status_code}")
            return jsonify({"error": msg}), r.status_code
        data = r.json()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if not isinstance(data, list):
        return jsonify({"error": "Unexpected response from Odds API"}), 500

    results = analyse(data, min_edge, ref)

    return jsonify({
        "events_scanned": len(data),
        "value_bets": results
    })


@app.route("/")
def health():
    return jsonify({"status": "ok", "message": "Value Bet Finder API running"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
