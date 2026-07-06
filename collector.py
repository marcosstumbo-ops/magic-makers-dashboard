"""
Coletor de dados da Etsy — Magic Makers Arts
==============================================
Roda periodicamente (via GitHub Actions) e gera `data.json`, que o
dashboard (index.html) lê para exibir os números.

O QUE ESTE SCRIPT FAZ:
  1. Renova o token de acesso usando o refresh token (OAuth 2.0).
  2. Busca os pedidos (receipts) recentes -> faturamento bruto.
  3. Busca o "ledger" (extrato financeiro) -> taxas da Etsy -> líquido real
     (o valor que efetivamente cai no Payoneer).
  4. Busca os anúncios (listings) -> favoritos, visitas acumuladas,
     data de criação (para calcular a cadência de publicação).
  5. Compara as visitas acumuladas de agora com a última leitura salva
     (state.json) para aproximar "visitas no período".
  6. Agrega tudo em daily / weekly / monthly e escreve em data.json.

CREDENCIAIS (nunca ficam no código — vêm de variáveis de ambiente,
que no GitHub Actions são preenchidas a partir dos "GitHub Secrets"):
  ETSY_API_KEY        -> Keystring do app
  ETSY_SHARED_SECRET   -> Shared secret do app
  ETSY_REFRESH_TOKEN   -> gerado na autorização OAuth do seu irmão
  ETSY_SHOP_ID         -> ID numérico da loja na Etsy

STATUS: primeira versão funcional. Os nomes exatos de alguns campos
de resposta da API (ex.: no ledger) podem precisar de pequenos ajustes
na primeira execução real — deixei comentários nos pontos mais
sensíveis a isso.
"""

import os
import json
import time
from datetime import datetime, timezone, timedelta
import requests

API_BASE = "https://api.etsy.com/v3/application"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"

API_KEY = os.environ["ETSY_API_KEY"]
SHARED_SECRET = os.environ["ETSY_SHARED_SECRET"]
REFRESH_TOKEN = os.environ["ETSY_REFRESH_TOKEN"]
SHOP_ID = os.environ["ETSY_SHOP_ID"]

STATE_FILE = "state.json"
OUTPUT_FILE = "data.json"

TZ_BR = timezone(timedelta(hours=-3))  # horário de Brasília


# ---------------------------------------------------------------------
# 1. AUTENTICAÇÃO
# ---------------------------------------------------------------------
def refresh_access_token():
    """Troca o refresh_token por um access_token novo (válido por ~1h)."""
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


# ---------------------------------------------------------------------
# 2. PEDIDOS / FATURAMENTO
# ---------------------------------------------------------------------
def fetch_receipts(token, since_ts):
    """Pedidos (receipts) criados a partir de `since_ts` (epoch seconds)."""
    results, offset = [], 0
    while True:
        data = api_get(f"/shops/{SHOP_ID}/receipts", token, {
            "min_created": since_ts,
            "limit": 100,
            "offset": offset,
        })
        results.extend(data["results"])
        if len(data["results"]) < 100:
            break
        offset += 100
    return results


# ---------------------------------------------------------------------
# 3. LEDGER (TAXAS / LÍQUIDO REAL)
# ---------------------------------------------------------------------
def fetch_ledger_entries(token, since_ts):
    """
    Extrato financeiro da loja: cada entrada tem o valor bruto e as taxas
    associadas. É daqui que tiramos o "líquido real" (o que cai no Payoneer).
    NOTA: confirmar o nome exato do endpoint/campos na primeira execução —
    a Etsy por vezes ajusta esse recurso (ex.: /shops/{shop_id}/payment-account/ledger-entries).
    """
    results, offset = [], 0
    while True:
        data = api_get(f"/shops/{SHOP_ID}/payment-account/ledger-entries", token, {
            "min_created": since_ts,
            "limit": 100,
            "offset": offset,
        })
        results.extend(data["results"])
        if len(data["results"]) < 100:
            break
        offset += 100
    return results


# ---------------------------------------------------------------------
# 4. ANÚNCIOS (favoritos, visitas acumuladas, cadência)
# ---------------------------------------------------------------------
def fetch_all_listings(token, state_filter="active"):
    results, offset = [], 0
    while True:
        data = api_get(f"/shops/{SHOP_ID}/listings", token, {
            "state": state_filter,
            "limit": 100,
            "offset": offset,
        })
        results.extend(data["results"])
        if len(data["results"]) < 100:
            break
        offset += 100
    return results


def compute_cadence(listings):
    now = datetime.now(timezone.utc)
    created_dates = [datetime.fromtimestamp(l["created_timestamp"], tz=timezone.utc) for l in listings]
    if not created_dates:
        return {"days_since_last": None, "active_count": 0, "created_last_30d": 0}
    most_recent = max(created_dates)
    days_since_last = (now - most_recent).days
    created_last_30d = sum(1 for d in created_dates if (now - d).days <= 30)
    return {
        "days_since_last": days_since_last,
        "active_count": len(listings),
        "created_last_30d": created_last_30d,
    }


def compute_visit_deltas(listings, previous_state):
    """
    A Etsy só devolve visitas ACUMULADAS por anúncio (não por período).
    Aproximamos "visitas no intervalo" comparando com a última leitura salva.
    """
    prev_views = previous_state.get("listing_views", {})
    deltas = {}
    new_views_snapshot = {}
    for l in listings:
        lid = str(l["listing_id"])
        views_now = l.get("views", 0) or 0
        new_views_snapshot[lid] = views_now
        prev = prev_views.get(lid, views_now)  # se não tinha leitura anterior, delta=0
        deltas[lid] = max(0, views_now - prev)
    return deltas, new_views_snapshot


# ---------------------------------------------------------------------
# 5. MONTAGEM DO data.json
# ---------------------------------------------------------------------
def build_output(listings, receipts, ledger_entries, visit_deltas, cadence):
    gross = sum(r.get("grandtotal", {}).get("amount", 0) / 100 for r in receipts)
    fees = sum(abs(e.get("amount", {}).get("amount", 0) / 100)
               for e in ledger_entries if e.get("amount", {}).get("amount", 0) < 0)
    net = gross - fees
    orders = len(receipts)

    products = []
    for l in listings:
        lid = str(l["listing_id"])
        title = l.get("title", "Sem título")
        favorites = l.get("num_favorers", 0)
        visits = visit_deltas.get(lid, 0)
        products.append({
            "listing_id": lid,
            "name": title,
            "visits": visits,
            "favorites": favorites,
        })

    now_br = datetime.now(TZ_BR)
    active_window = 8 <= now_br.hour < 24

    return {
        "last_updated": now_br.isoformat(),
        "active_window": active_window,
        "today": {
            "gross": round(gross, 2),
            "net": round(net, 2),
            "orders": orders,
            "avg_ticket": round(gross / orders, 2) if orders else 0,
        },
        "cadence": cadence,
        "products": products,
    }


# ---------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------
def main():
    # Carrega estado anterior (para o cálculo de visitas por diferença)
    previous_state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            previous_state = json.load(f)

    token = refresh_access_token()

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
