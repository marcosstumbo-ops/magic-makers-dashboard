"""
Coletor de dados da Etsy — Magic Makers Arts
"""

import os
import json
import time
from datetime import datetime, timezone, timedelta
import requests

API_BASE = "https://api.etsy.com/v3/application"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"

API_KEY = os.environ["ETSY_API_KEY"]
SHARED_SECRET = os.environ.get("ETSY_SHARED_SECRET", "")
REFRESH_TOKEN = os.environ["ETSY_REFRESH_TOKEN"]
SHOP_ID = os.environ.get("ETSY_SHOP_ID", "")

STATE_FILE = "state.json"
OUTPUT_FILE = "data.json"

TZ_BR = timezone(timedelta(hours=-3))


def refresh_access_token():
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "client_id": API_KEY,
        "refresh_token": REFRESH_TOKEN,
    })
    resp.raise_for_status()
    return resp.json()["access_token"]


def api_get(path, token, params=None):
    headers = {
        "x-api-key": API_KEY,
        "Authorization": f"Bearer {token}",
    }
    resp = requests.get(f"{API_BASE}{path}", headers=headers, params=params or {})
    resp.raise_for_status()
    return resp.json()


def get_shop_id(token):
    user_id = token.split(".")[0]
    data = api_get(f"/users/{user_id}/shops", token)
    if isinstance(data, dict) and "shop_id" in data:
        return str(data["shop_id"])
    if isinstance(data, dict) and "results" in data and data["results"]:
        return str(data["results"][0]["shop_id"])
    raise RuntimeError(f"Não consegui descobrir o shop_id. Resposta da API: {data}")


def fetch_receipts(token, since_ts):
    results, offset = [], 0
    while True:
        data = api_get(f"/shops/{SHOP_ID}/receipts", token, {
            "min_created": since_ts,
            "limit": 100,
            "offset": offset,
        })
        results.extend(data.get("results", []))
        if len(data.get("results", [])) < 100:
            break
        offset += 100
    return results


def fetch_ledger_entries(token, since_ts):
    results, offset = [], 0
    try:
        while True:
            data = api_get(f"/shops/{SHOP_ID}/payment-account/ledger-entries", token, {
                "min_created": since_ts,
                "limit": 100,
                "offset": offset,
            })
            results.extend(data.get("results", []))
            if len(data.get("results", [])) < 100:
                break
            offset += 100
    except Exception as e:
        print(f"AVISO: não consegui ler o ledger ({e}). Seguindo sem as taxas por enquanto.")
        return []
    return results


def fetch_all_listings(token, state_filter="active"):
    results, offset = [], 0
    while True:
        data = api_get(f"/shops/{SHOP_ID}/listings", token, {
            "state": state_filter,
            "limit": 100,
            "offset": offset,
        })
        results.extend(data.get("results", []))
        if len(data.get("results", [])) < 100:
            break
        offset += 100
    return results


def compute_cadence(listings):
    now = datetime.now(timezone.utc)
    created_dates = []
    for l in listings:
        ts = l.get("created_timestamp") or l.get("creation_timestamp")
        if ts:
            created_dates.append(datetime.fromtimestamp(ts, tz=timezone.utc))
    if not created_dates:
        return {"days_since_last": None, "active_count": len(listings), "created_last_30d": 0}
    most_recent = max(created_dates)
    days_since_last = (now - most_recent).days
    created_last_30d = sum(1 for d in created_dates if (now - d).days <= 30)
    return {
        "days_since_last": days_since_last,
        "active_count": len(listings),
        "created_last_30d": created_last_30d,
    }


def compute_visit_deltas(listings, previous_state):
    prev_views = previous_state.get("listing_views", {})
    deltas = {}
    new_views_snapshot = {}
    for l in listings:
        lid = str(l["listing_id"])
        views_now = l.get("views", 0) or 0
        new_views_snapshot[lid] = views_now
        prev = prev_views.get(lid, views_now)
        deltas[lid] = max(0, views_now - prev)
    return deltas, new_views_snapshot


def build_output(listings, receipts, ledger_entries, visit_deltas, cadence):
    gross = sum(r.get("grandtotal", {}).get("amount", 0) / 100 for r in receipts)
    fees = sum(abs(e.get("amount", {}).get("amount", 0) / 100)
               for e in ledger_entries if e.get("amount", {}).get("amount", 0) < 0)
    net = gross - fees if ledger_entries else gross
    orders = len(receipts)

    products = []
    for l in listings:
        lid = str(l["listing_id"])
        products.append({
            "listing_id": lid,
            "name": l.get("title", "Sem título"),
            "visits": visit_deltas.get(lid, 0),
            "favorites": l.get("num_favorers", 0),
        })

    now_br = datetime.now(TZ_BR)
    active_window = 8 <= now_br.hour < 24

    return {
        "last_updated": now_br.isoformat(),
        "active_window": active_window,
        "shop_id": SHOP_ID,
        "today": {
            "gross": round(gross, 2),
            "net": round(net, 2),
            "orders": orders,
            "avg_ticket": round(gross / orders, 2) if orders else 0,
        },
        "cadence": cadence,
        "products": products,
    }


def main():
    global SHOP_ID

    previous_state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            previous_state = json.load(f)

    token = refresh_access_token()

    SHOP_ID = get_shop_id(token)
    print(f"Shop ID detectado: {SHOP_ID}")

    midnight_today = datetime.now(TZ_BR).replace(hour=0, minute=0, second=0, microsecond=0)
    since_ts = int(midnight_today.timestamp())

    receipts = fetch_receipts(token, since_ts)
    ledger_entries = fetch_ledger_entries(token, since_ts)
    listings = fetch_all_listings(token)

    cadence = compute_cadence(listings)
    visit_deltas, new_views_snapshot = compute_visit_deltas(listings, previous_state)

    output = build_output(listings, receipts, ledger_entries, visit_deltas, cadence)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"listing_views": new_views_snapshot, "saved_at": time.time()}, f)

    print(f"OK — {output['today']['orders']} pedidos, líquido R${output['today']['net']:.2f}")


if __name__ == "__main__":
    main()
