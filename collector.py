"""
Coletor v2 — Magic Makers Arts
================================
Gera `historico.json` com o histórico completo de vendas da loja.

COMO FUNCIONA:
  - PRIMEIRA execução (historico.json não existe): busca TODOS os pedidos
    e transações desde a criação da loja (carga histórica completa).
  - Execuções seguintes: busca só os últimos 7 dias e mescla no histórico
    (rápido e econômico com o limite de requisições da Etsy).

SAÍDA (historico.json):
  {
    generated_at, currency, shop_id,
    daily:    { "2026-08-08": {gross, net, fees, orders}, ... },
    cadence:  { days_since_last, active_count, created_last_30d },
    products: { listing_id: { name, favorites,
                monthly: { "2026-08": {sales, revenue} } } }
  }
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

HIST_FILE = "historico.json"

# Fuso da LOJA na Etsy. A Etsy agrupa "vendas do dia" pelo fuso configurado
# na conta. Deixamos aqui num único lugar para ajustar quando confirmarmos.
# UTC (offset 0) costuma alinhar bem com o painel; se a loja usar horário
# dos EUA, trocar para -5 (Eastern) ou -8 (Pacific), por exemplo.
SHOP_TZ_OFFSET = -4  # em horas; -4 = Leste EUA (horário de verão)
TZ_SHOP = timezone(timedelta(hours=SHOP_TZ_OFFSET))

TZ_BR = timezone(timedelta(hours=-3))  # usado só para o carimbo "gerado em"

SHOP_ID = ""


# ------------------------- infra -------------------------
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
        "x-api-key": f"{API_KEY}:{SHARED_SECRET}",
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
    if isinstance(data, dict) and data.get("results"):
        return str(data["results"][0]["shop_id"])
    raise RuntimeError(f"Shop_id não encontrado: {data}")


def paged(path, token, extra_params=None, page_limit=100, max_pages=200):
    """Percorre um endpoint paginado até o fim (ou max_pages)."""
    results, offset, pages = [], 0, 0
    while pages < max_pages:
        params = {"limit": page_limit, "offset": offset}
        if extra_params:
            params.update(extra_params)
        data = api_get(path, token, params)
        batch = data.get("results", [])
        results.extend(batch)
        pages += 1
        if len(batch) < page_limit:
            break
        offset += page_limit
        time.sleep(0.15)  # gentileza com o rate limit
    return results


# ------------------------- coleta -------------------------
def fetch_receipts(token, since_ts=None):
    extra = {}
    if since_ts:
        extra["min_created"] = since_ts
    return paged(f"/shops/{SHOP_ID}/receipts", token, extra)


def fetch_transactions(token):
    """Todas as transações (itens vendidos) da loja — para o breakdown por produto."""
    try:
        return paged(f"/shops/{SHOP_ID}/transactions", token)
    except Exception as e:
        print(f"AVISO: transações indisponíveis ({e}). Produtos ficarão sem breakdown de vendas.")
        return []


def fetch_ledger(token, since_ts=None):
    try:
        extra = {}
        if since_ts:
            extra["min_created"] = since_ts
        return paged(f"/shops/{SHOP_ID}/payment-account/ledger-entries", token, extra)
    except Exception as e:
        print(f"AVISO: ledger indisponível ({e}). Líquido = bruto por enquanto.")
        return []


def fetch_listings(token):
    all_listings = []
    for state in ("active", "sold_out", "expired", "inactive"):
        try:
            all_listings.extend(paged(f"/shops/{SHOP_ID}/listings", token, {"state": state}))
        except Exception as e:
            print(f"AVISO: listings state={state} falhou ({e})")
    return all_listings


# ------------------------- agregação -------------------------
def day_key(ts):
    # Agrupa pelo fuso da LOJA (alinha com o painel da Etsy).
    return datetime.fromtimestamp(ts, tz=TZ_SHOP).strftime("%Y-%m-%d")


def receipt_amount(r):
    gt = r.get("grandtotal") or {}
    return (gt.get("amount", 0) or 0) / max(gt.get("divisor", 100) or 100, 1)


def receipt_currency(r):
    gt = r.get("grandtotal") or {}
    return gt.get("currency_code", "USD")


def merge_receipts_into_daily(daily, receipts):
    currency = None
    for r in receipts:
        ts = r.get("created_timestamp") or r.get("create_timestamp")
        if not ts:
            continue
        dk = day_key(ts)
        d = daily.setdefault(dk, {"gross": 0.0, "net": 0.0, "fees": 0.0, "orders": 0, "_ids": []})
        rid = r.get("receipt_id")
        # evita contar o mesmo pedido duas vezes ao mesclar períodos
        if rid and rid in d.get("_ids", []):
            continue
        amt = receipt_amount(r)
        d["gross"] = round(d["gross"] + amt, 2)
        d["orders"] += 1
        if rid:
            d.setdefault("_ids", []).append(rid)
        if currency is None:
            currency = receipt_currency(r)
    return currency


def merge_fees_into_daily(daily, ledger_entries):
    for e in ledger_entries:
        ts = e.get("create_date") or e.get("created_timestamp")
        if not ts:
            continue
        amt_obj = e.get("amount")
        if isinstance(amt_obj, dict):
            amount = (amt_obj.get("amount", 0) or 0) / max(amt_obj.get("divisor", 100) or 100, 1)
        else:
            amount = (amt_obj or 0) / 100
        if amount >= 0:
            continue  # só nos interessam débitos (taxas)
        dk = day_key(ts)
        d = daily.setdefault(dk, {"gross": 0.0, "net": 0.0, "fees": 0.0, "orders": 0, "_ids": []})
        d["fees"] = round(d["fees"] + abs(amount), 2)


def finalize_daily(daily):
    for dk, d in daily.items():
        d["net"] = round(max(0.0, d["gross"] - d.get("fees", 0.0)), 2)
        d.pop("_ids", None)


def build_products(listings, transactions):
    products = {}
    for l in listings:
        lid = str(l.get("listing_id"))
        products[lid] = {
            "name": l.get("title", "Sem título"),
            "favorites": l.get("num_favorers", 0),
            "monthly": {},
        }
    for t in transactions:
        lid = str(t.get("listing_id"))
        ts = t.get("paid_timestamp") or t.get("created_timestamp") or t.get("create_timestamp")
        if not ts:
            continue
        mk = datetime.fromtimestamp(ts, tz=TZ_SHOP).strftime("%Y-%m")
        price_obj = t.get("price") or {}
        price = (price_obj.get("amount", 0) or 0) / max(price_obj.get("divisor", 100) or 100, 1)
        qty = t.get("quantity", 1) or 1
        p = products.setdefault(lid, {"name": t.get("title", "Sem título"), "favorites": 0, "monthly": {}})
        m = p["monthly"].setdefault(mk, {"sales": 0, "revenue": 0.0})
        m["sales"] += qty
        m["revenue"] = round(m["revenue"] + price * qty, 2)
    return products


def compute_cadence(listings):
    now = datetime.now(timezone.utc)
    active = [l for l in listings if l.get("state") == "active"]
    dates = []
    for l in active:
        # IMPORTANTE: usar a criação ORIGINAL, não created_timestamp —
        # este último muda toda vez que o anúncio é renovado (a cada 4 meses),
        # o que faria todos os produtos parecerem "criados hoje".
        # Diagnóstico confirmou: o campo certo é original_creation_timestamp.
        ts = (l.get("original_creation_timestamp")
              or l.get("original_created_timestamp")
              or l.get("creation_timestamp"))
        if ts:
            dates.append(datetime.fromtimestamp(ts, tz=timezone.utc))
    if not dates:
        return {"days_since_last": None, "active_count": len(active), "created_last_30d": 0}
    most_recent = max(dates)
    return {
        "days_since_last": (now - most_recent).days,
        "active_count": len(active),
        "created_last_30d": sum(1 for d in dates if (now - d).days <= 30),
    }


# ------------------------- main -------------------------
def main():
    global SHOP_ID

    hist = None
    if os.path.exists(HIST_FILE):
        with open(HIST_FILE, "r", encoding="utf-8") as f:
            hist = json.load(f)

    full_load = hist is None
    token = refresh_access_token()
    SHOP_ID = get_shop_id(token)
    print(f"Shop ID: {SHOP_ID} | Carga: {'COMPLETA (primeira vez)' if full_load else 'incremental'}")

    daily = hist.get("daily", {}) if hist else {}
    # restaura _ids vazios (não persistimos ids antigos; dedupe vale por execução)
    for d in daily.values():
        d.setdefault("_ids", [])

    if full_load:
        since = None  # tudo, desde sempre
    else:
        # janela dos últimos 8 dias, no fuso da loja (alinhado com day_key)
        cutoff = datetime.now(TZ_SHOP) - timedelta(days=8)
        since = int(cutoff.timestamp())
        # zera os últimos 8 dias para recalcular do zero (evita duplicar)
        for i in range(9):
            dk = (datetime.now(TZ_SHOP) - timedelta(days=i)).strftime("%Y-%m-%d")
            daily.pop(dk, None)

    receipts = fetch_receipts(token, since)
    print(f"Pedidos obtidos: {len(receipts)}")
    currency_code = merge_receipts_into_daily(daily, receipts)

    ledger = fetch_ledger(token, since)
    print(f"Lançamentos do ledger: {len(ledger)}")
    merge_fees_into_daily(daily, ledger)
    finalize_daily(daily)

    listings = fetch_listings(token)
    print(f"Anúncios: {len(listings)}")

    transactions = fetch_transactions(token) if full_load else fetch_transactions(token)
    print(f"Transações: {len(transactions)}")
    products = build_products(listings, transactions)

    cadence = compute_cadence(listings)

    cur_symbol = {"USD": "US$", "BRL": "R$", "EUR": "€"}.get(currency_code or "USD", currency_code or "US$")
    out = {
        "generated_at": datetime.now(TZ_BR).isoformat(),
        "currency": cur_symbol,
        "shop_id": SHOP_ID,
        "daily": daily,
        "cadence": cadence,
        "products": products,
    }
    with open(HIST_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    total = sum(d.get("gross", 0) for d in daily.values())
    print(f"OK — {len(daily)} dias no histórico, faturamento acumulado {cur_symbol}{total:.2f}")


if __name__ == "__main__":
    main()
