# ═══════════════════════════════════════════════════════════════
# AURO Relay — Flask прокси для Tinkoff Invest API + Brent
# Deploy: Render.com | Env: TINKOFF_TOKEN (readonly токен!)
# ═══════════════════════════════════════════════════════════════
import os
import time
import traceback
import requests
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify

app = Flask(__name__)

TINKOFF_TOKEN = os.environ.get('TINKOFF_TOKEN', '')
TINKOFF_BASE = 'https://invest-public-api.tinkoff.ru/rest/tinkoff.public.invest.api.contract.v1'

# ── CORS для браузера ──
@app.after_request
def add_cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return resp

def _tk_post(method, body):
    r = requests.post(
        f'{TINKOFF_BASE}.{method}',
        json=body,
        headers={'Authorization': f'Bearer {TINKOFF_TOKEN}',
                 'Content-Type': 'application/json'},
        timeout=15
    )
    r.raise_for_status()
    return r.json()

def _q(quotation):
    """Tinkoff quotation {units, nano} → float"""
    if not quotation:
        return None
    return float(quotation.get('units', 0)) + quotation.get('nano', 0) / 1e9

# ── Кэш ответов (candles тяжёлые — держим 20 сек) ──
_resp_cache = {}
def _cache_get(key, ttl=20):
    e = _resp_cache.get(key)
    if e and time.time() - e[0] < ttl:
        return e[1]
    return None
def _cache_set(key, data):
    _resp_cache[key] = (time.time(), data)
    if len(_resp_cache) > 200:
        _resp_cache.clear()

# ── Кэш тикер → instrument_uid (резолвим один раз) ──
_uid_cache = {}

def resolve_uid(ticker):
    if ticker in _uid_cache:
        return _uid_cache[ticker]
    uid = None
    # Способ 1: ShareBy
    try:
        d = _tk_post('InstrumentsService/ShareBy', {
            'idType': 'INSTRUMENT_ID_TYPE_TICKER',
            'classCode': 'TQBR',
            'id': ticker
        })
        uid = d.get('instrument', {}).get('uid')
    except Exception:
        pass
    # Способ 2 (fallback): FindInstrument
    if not uid:
        try:
            d = _tk_post('InstrumentsService/FindInstrument', {
                'query': ticker,
                'instrumentKind': 'INSTRUMENT_TYPE_SHARE'
            })
            for inst in d.get('instruments', []):
                if inst.get('ticker') == ticker and inst.get('classCode') == 'TQBR':
                    uid = inst.get('uid')
                    break
            # если точного не нашли — берём первый
            if not uid and d.get('instruments'):
                uid = d['instruments'][0].get('uid')
        except Exception:
            pass
    if uid:
        _uid_cache[ticker] = uid
    return uid

# ── Интервалы: наш tf → Tinkoff + макс. окно одного запроса (дней) ──
TF_MAP = {
    '1M':  ('CANDLE_INTERVAL_1_MIN', 1),
    '5M':  ('CANDLE_INTERVAL_5_MIN', 1),
    '15M': ('CANDLE_INTERVAL_15_MIN', 1),
    '1H':  ('CANDLE_INTERVAL_HOUR', 7),
    '4H':  ('CANDLE_INTERVAL_4_HOUR', 30),
    '1D':  ('CANDLE_INTERVAL_DAY', 365),
    '1W':  ('CANDLE_INTERVAL_WEEK', 1825),
    '1Mo': ('CANDLE_INTERVAL_MONTH', 3650),
}
# Сколько истории грузить по умолчанию (дней)
TF_DEPTH = {'1M': 2, '5M': 5, '15M': 10, '1H': 30, '4H': 90,
            '1D': 365, '1W': 1825, '1Mo': 3650}

@app.route('/tinkoff/candles')
def tinkoff_candles():
    if not TINKOFF_TOKEN:
        return jsonify({'error': 'TINKOFF_TOKEN not set'}), 500
    ticker = request.args.get('ticker', 'SBER')
    tf = request.args.get('tf', '1D')
    days = int(request.args.get('days', TF_DEPTH.get(tf, 30)))

    cached = _cache_get(f'candles:{ticker}:{tf}:{days}', ttl=15)
    if cached:
        return jsonify(cached)

    interval, chunk_days = TF_MAP.get(tf, TF_MAP['1D'])
    try:
        uid = resolve_uid(ticker)
    except Exception as e:
        return jsonify({'error': f'resolve failed: {e}'}), 500
    if not uid:
        return jsonify({'error': f'ticker {ticker} not found'}), 404

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    candles = []

    # Пагинация чанками по лимиту интервала
    cursor = start
    while cursor < now:
        chunk_end = min(cursor + timedelta(days=chunk_days), now)
        try:
            d = _tk_post('MarketDataService/GetCandles', {
                'instrumentId': uid,
                'from': cursor.isoformat(),
                'to': chunk_end.isoformat(),
                'interval': interval
            })
            for c in d.get('candles', []):
                t = c.get('time', '')
                candles.append({
                    'time': int(datetime.fromisoformat(
                        t.replace('Z', '+00:00')).timestamp()),
                    'open': _q(c.get('open')),
                    'high': _q(c.get('high')),
                    'low': _q(c.get('low')),
                    'close': _q(c.get('close')),
                    'volume': int(c.get('volume', 0)),
                })
        except Exception as e:
            app.logger.warning(f'chunk fail {cursor}: {e}')
        cursor = chunk_end
        time.sleep(0.05)  # бережём rate limit (300 req/min)

    # Дедуп + сортировка
    seen, out = set(), []
    for c in sorted(candles, key=lambda x: x['time']):
        if c['time'] not in seen:
            seen.add(c['time'])
            out.append(c)
    payload = {'candles': out, 'source': 'tinkoff'}
    _cache_set(f'candles:{ticker}:{tf}:{days}', payload)
    return jsonify(payload)

@app.route('/tinkoff/price')
def tinkoff_price():
    if not TINKOFF_TOKEN:
        return jsonify({'error': 'TINKOFF_TOKEN not set'}), 500
    try:
        tickers = request.args.get('tickers', 'SBER').split(',')[:50]
        uids, tick_by_uid = [], {}
        errors = []
        for t in tickers:
            try:
                uid = resolve_uid(t.strip())
                if uid:
                    uids.append(uid)
                    tick_by_uid[uid] = t.strip()
                else:
                    errors.append(f'{t}: not found')
            except Exception as e:
                errors.append(f'{t}: {e}')
        if not uids:
            return jsonify({'error': 'no valid tickers', 'details': errors}), 404

        last = _tk_post('MarketDataService/GetLastPrices', {'instrumentId': uids})

        # ClosePrices — опционально: если упадёт, проценты будут null
        close_by_uid = {}
        try:
            close = _tk_post('MarketDataService/GetClosePrices', {
                'instruments': [{'instrumentId': u} for u in uids]})
            close_by_uid = {c.get('instrumentUid'): _q(c.get('price'))
                            for c in close.get('closePrices', [])}
        except Exception:
            pass

        result = {}
        for p in last.get('lastPrices', []):
            uid = p.get('instrumentUid')
            ticker = tick_by_uid.get(uid)
            price = _q(p.get('price'))
            prev = close_by_uid.get(uid)
            pct = ((price - prev) / prev * 100) if (price and prev) else None
            if ticker:
                result[ticker] = {'p': price, 'c': pct, 'prev': prev}
        return jsonify({'prices': result, 'source': 'tinkoff'})
    except Exception as e:
        return jsonify({'error': str(e),
                        'trace': traceback.format_exc().splitlines()[-5:]}), 500

@app.route('/brent')
def brent():
    try:
        r = requests.get(
            'https://query1.finance.yahoo.com/v8/finance/chart/BZ=F'
            '?interval=1d&range=5d',
            headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        res = r.json()['chart']['result'][0]
        closes = [c for c in res['indicators']['quote'][0]['close'] if c]
        price, prev = closes[-1], closes[-2] if len(closes) > 1 else closes[-1]
        return jsonify({'price': round(price, 2),
                        'change': round((price - prev) / prev * 100, 2)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/')
def health():
    return jsonify({'status': 'ok',
                    'tinkoff': bool(TINKOFF_TOKEN),
                    'endpoints': ['/tinkoff/candles?ticker=SBER&tf=5M',
                                  '/tinkoff/price?tickers=SBER,GAZP',
                                  '/brent']})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
