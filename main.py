#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台股即時看盤 (Taiwan Stock Monitor)

- 只使用 Python 標準庫即可執行 (Python 3.8+)；永豐 Shioaji 為選用
- 即時報價：證交所 MIS (預設) 或 永豐 Shioaji (選用，需 API 金鑰)
- 歷史 K 線：Yahoo Finance，失敗時退回證交所 / 永豐
- 介面：index.html (與本檔放同一資料夾)，由本機 HTTP 伺服器提供

用法：
    python3 main.py            啟動並自動開啟瀏覽器
    python3 main.py --demo     離線示範模式 (假資料)
    python3 main.py --help     所有參數

資料僅供參考，非投資建議。
"""
import argparse
import calendar
import datetime as dt
import http.cookiejar
import json
import math
import os
import random
import re
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
import zlib
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CTX = ssl.create_default_context()
DEMO = False
SJ = None              # 永豐 Shioaji 客戶端 (啟用時)
KNOWN_EX = {}          # code -> 'tse' / 'otc'
TAIPEI = dt.timezone(dt.timedelta(hours=8))


def taipei_now():
    return dt.datetime.now(TAIPEI)


def ts_of(d):
    """某日 09:00 (台北時間) 的 epoch 秒"""
    return calendar.timegm((d.year, d.month, d.day, 1, 0, 0))


def http_json(url, headers=None, timeout=12):
    h = {"User-Agent": UA, "Accept": "application/json,text/plain,*/*"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return json.loads(r.read().decode("utf-8"))


def fnum(x):
    try:
        v = float(str(x).replace(",", ""))
        return None if math.isnan(v) else v
    except Exception:
        return None


def tick_size(p):
    if p < 10: return 0.01
    if p < 50: return 0.05
    if p < 100: return 0.1
    if p < 500: return 0.5
    if p < 1000: return 1.0
    return 5.0


def rtick(p):
    t = tick_size(p)
    return round(round(p / t) * t, 2)


# ----------------------------------------------------------------- MIS 即時報價
class MIS:
    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj),
            urllib.request.HTTPSHandler(context=CTX))
        self.op.addheaders = [("User-Agent", UA), ("Accept-Language", "zh-TW,zh;q=0.9")]
        self.t0 = 0
        self.lock = threading.Lock()

    def ensure(self, force=False):
        if force or time.time() - self.t0 > 600:
            self.op.open("https://mis.twse.com.tw/stock/index.jsp", timeout=10).read()
            self.t0 = time.time()

    def fetch(self, ex_chs):
        with self.lock:
            for attempt in (0, 1):
                try:
                    self.ensure(force=attempt == 1)
                    url = ("https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=" +
                           urllib.parse.quote("|".join(ex_chs), safe="|_.") +
                           "&json=1&delay=0&_=" + str(int(time.time() * 1000)))
                    req = urllib.request.Request(
                        url, headers={"Referer": "https://mis.twse.com.tw/stock/index.jsp"})
                    data = json.loads(self.op.open(req, timeout=10).read().decode("utf-8"))
                    return data.get("msgArray", [])
                except Exception:
                    if attempt == 1:
                        raise
        return []


_mis = None


def lst(s):
    return [fnum(v) for v in (s or "").split("_") if v != ""]


def norm_quote(r):
    ts = fnum(r.get("tlong"))
    return {
        "code": r.get("c"), "name": r.get("n"), "full": r.get("nf"),
        "ex": r.get("ex", "tse"),
        "price": fnum(r.get("z")), "prev": fnum(r.get("y")),
        "open": fnum(r.get("o")), "high": fnum(r.get("h")), "low": fnum(r.get("l")),
        "vol": fnum(r.get("v")), "tv": fnum(r.get("tv")),
        "ask": lst(r.get("a")), "askVol": lst(r.get("f")),
        "bid": lst(r.get("b")), "bidVol": lst(r.get("g")),
        "up": fnum(r.get("u")), "down": fnum(r.get("w")),
        "time": r.get("t"), "date": r.get("d"),
        "ts": int(ts) if ts else None,
    }


def mis_quotes(codes):
    global _mis
    if _mis is None:
        _mis = MIS()
    chs = []
    for c in codes:
        if c == "t00": chs.append("tse_t00.tw"); continue
        if c == "o00": chs.append("otc_o00.otc"); continue
        ex = KNOWN_EX.get(c)
        if ex in (None, "tse"): chs.append("tse_%s.tw" % c)
        if ex in (None, "otc"): chs.append("otc_%s.otc" % c)
    rows = []
    for i in range(0, len(chs), 40):
        rows += _mis.fetch(chs[i:i + 40])
    out = {}
    for r in rows:
        if not r.get("c") or not r.get("n"):
            continue
        q = norm_quote(r)
        if q["code"] not in ("t00", "o00"):
            KNOWN_EX[q["code"]] = q["ex"]
        old = out.get(q["code"])
        if old is None or (old["price"] is None and q["price"] is not None):
            out[q["code"]] = q
    return out


def get_quotes(codes):
    if DEMO:
        return demo_quotes(codes)
    if SJ:
        stocks = [c for c in codes if c not in ("t00", "o00")]
        out = SJ.quotes(stocks)
        rest = [c for c in codes if c not in out]      # 指數、永豐查不到的代號 -> 證交所 MIS
        if rest:
            try:
                out.update(mis_quotes(rest))
            except Exception:
                if not out:
                    raise
        return out
    return mis_quotes(codes)



# ----------------------------------------------------------------- 永豐 Shioaji
def _f(x):
    try:
        v = float(x)
        return v if v == v else None
    except Exception:
        return None


def _fl(a):
    return [v for v in (_f(x) for x in (a or [])) if v is not None]


def sj_epoch(ts_ns_list):
    """Shioaji 的 ts (ns) -> 真正的 epoch 秒。自動判斷是否為台北牆上時間、是否標在分K結束時間。"""
    sec = [int(t) // 10 ** 9 for t in ts_ns_list]
    if not sec:
        return []
    shift = -28800 if 9 <= (sec[0] % 86400) // 3600 <= 14 else 0     # 牆上時間 -> UTC epoch
    if ((sec[0] + shift + 28800) % 86400) // 60 == 541:              # 第一根為 09:01 -> 標在結束時間
        shift -= 60
    return [s + shift for s in sec]


def agg_minutes(bars, size):
    out, cur = [], None
    for b in bars:
        k = b[0] // size * size
        if cur is None or cur[0] != k:
            cur = [k, b[1], b[2], b[3], b[4], b[5]]; out.append(cur)
        else:
            cur[2] = max(cur[2], b[2]); cur[3] = min(cur[3], b[3]); cur[4] = b[4]; cur[5] += b[5]
    return out


def agg_days(bars):
    out, cur, key = [], None, None
    for b in bars:
        k = (b[0] + 28800) // 86400
        if k != key:
            key = k
            cur = [ts_of(dt.datetime.fromtimestamp(b[0] + 28800, dt.timezone.utc).date()), b[1], b[2], b[3], b[4], b[5]]
            out.append(cur)
        else:
            cur[2] = max(cur[2], b[2]); cur[3] = min(cur[3], b[3]); cur[4] = b[4]; cur[5] += b[5]
    return out


class Sinopac:
    def __init__(self, api_key, secret_key, simulation=False):
        try:
            import shioaji as sj
        except ImportError:
            raise RuntimeError("找不到 shioaji 套件，請先執行：pip install shioaji")
        self.sj = sj
        self.api = sj.Shioaji(simulation=simulation)
        self.api.login(api_key=api_key, secret_key=secret_key)
        self.lock = threading.RLock()
        self.sub, self.tick, self.ba, self.snap = {}, {}, {}, {}
        api = self.api

        @api.on_tick_stk_v1()
        def _on_tick(exchange, tick):
            try:
                if getattr(tick, "simtrade", 0):
                    return                      # 試撮不算成交
                self.tick[tick.code] = {
                    "close": _f(tick.close), "open": _f(tick.open), "high": _f(tick.high), "low": _f(tick.low),
                    "total_volume": _f(tick.total_volume), "volume": _f(tick.volume),
                    "ts": int(time.time() * 1000), "time": tick.datetime.strftime("%H:%M:%S")}
            except Exception:
                pass

        @api.on_bidask_stk_v1()
        def _on_ba(exchange, ba):
            try:
                self.ba[ba.code] = {"bid": _fl(ba.bid_price), "bidVol": _fl(ba.bid_volume),
                                    "ask": _fl(ba.ask_price), "askVol": _fl(ba.ask_volume)}
            except Exception:
                pass

    # -- 合約 / 名稱
    def contract(self, code):
        try:
            return self.api.Contracts.Stocks[code]
        except Exception:
            return None

    def names(self):
        out = []
        for ex, grp in (("tse", "TSE"), ("otc", "OTC")):
            try:
                for c in getattr(self.api.Contracts.Stocks, grp):
                    out.append((c.code, c.name, ex))
            except Exception:
                pass
        return out

    # -- 即時報價
    def _ensure(self, codes):
        new = []
        for code in codes:
            if code in self.sub:
                continue
            c = self.contract(code)
            if c is None:
                continue
            q = self.sj.constant
            self.api.quote.subscribe(c, quote_type=q.QuoteType.Tick, version=q.QuoteVersion.v1)
            self.api.quote.subscribe(c, quote_type=q.QuoteType.BidAsk, version=q.QuoteVersion.v1)
            self.sub[code] = c
            new.append(c)
        if new:
            try:
                for s in self.api.snapshots(new):
                    self.snap[s.code] = s
            except Exception:
                pass

    def quotes(self, codes):
        with self.lock:
            self._ensure(codes)
        out = {}
        for code in codes:
            c = self.sub.get(code)
            if c is None:
                continue
            s, t, b = self.snap.get(code), self.tick.get(code), self.ba.get(code)
            price = (t or {}).get("close") or (_f(s.close) if s else None) or None
            sv = lambda k: (_f(getattr(s, k, None)) or None) if s else None
            tt = t or {}
            snap_ts = None
            if s is not None:
                try:
                    snap_ts = sj_epoch([s.ts])[0] * 1000
                except Exception:
                    pass
            out[code] = {
                "code": code, "name": c.name, "full": None,
                "ex": "otc" if "OTC" in str(c.exchange).upper() else "tse",
                "price": price, "prev": _f(c.reference),
                "open": tt.get("open") or sv("open"), "high": tt.get("high") or sv("high"),
                "low": tt.get("low") or sv("low"),
                "vol": tt.get("total_volume") or sv("total_volume"), "tv": tt.get("volume"),
                "ask": (b or {}).get("ask") or ([sv("sell_price")] if sv("sell_price") else []),
                "askVol": (b or {}).get("askVol") or ([sv("sell_volume")] if sv("sell_price") else []),
                "bid": (b or {}).get("bid") or ([sv("buy_price")] if sv("buy_price") else []),
                "bidVol": (b or {}).get("bidVol") or ([sv("buy_volume")] if sv("buy_price") else []),
                "up": _f(c.limit_up), "down": _f(c.limit_down),
                "time": tt.get("time") or taipei_now().strftime("%H:%M:%S"),
                "date": taipei_now().strftime("%Y%m%d"),
                "ts": tt.get("ts") or snap_ts,
            }
        return out

    # -- K 線 (1 分K 再自行聚合)
    def _kbars(self, c, start, end):
        with self.lock:
            k = self.api.kbars(c, start=start.isoformat(), end=end.isoformat())
        ts = sj_epoch(list(k.ts))
        bars = []
        for i, t in enumerate(ts):
            o, h, l, cl = _f(k.Open[i]), _f(k.High[i]), _f(k.Low[i]), _f(k.Close[i])
            if None in (o, h, l, cl) or cl == 0:
                continue
            bars.append([t, o, h, l, cl, float(k.Volume[i] or 0)])
        bars.sort(key=lambda b: b[0])
        return bars

    def history(self, code, tf):
        c = self.contract(code)
        if c is None:
            raise ValueError("永豐查無此代號")
        today = taipei_now().date()
        if tf == "rt":
            bars = []
            for back in range(0, 8):
                d = today - dt.timedelta(days=back)
                if d.weekday() >= 5:
                    continue
                bars = self._kbars(c, d, d)
                if bars:
                    break
        else:
            span = {"5d": 8, "5m": 30, "15m": 30, "60m": 120, "D": 400, "W": 800, "M": 1100}[tf]
            bars = self._kbars(c, today - dt.timedelta(days=span), today)
            if tf in ("5d", "5m"): bars = agg_minutes(bars, 300)
            elif tf == "15m": bars = agg_minutes(bars, 900)
            elif tf == "60m": bars = agg_minutes(bars, 3600)
            else:
                bars = agg_days(bars)
                if tf != "D":
                    bars = aggregate(bars, tf)
            if tf == "5d":
                days = sorted({(b[0] + 28800) // 86400 for b in bars})[-5:]
                bars = [b for b in bars if (b[0] + 28800) // 86400 in days]
        if not bars:
            raise RuntimeError("永豐 kbars 無資料")
        return {"bars": bars, "prevClose": _f(c.reference), "source": "永豐 Shioaji"}


def load_sj_credentials():
    k, sec = os.environ.get("SJ_API_KEY"), os.environ.get("SJ_SECRET_KEY")
    if k and sec:
        return k, sec
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sinopac.json")
    if os.path.exists(path):
        try:
            d = json.load(open(path, encoding="utf-8"))
            return d["api_key"], d["secret_key"]
        except Exception as e:
            print("讀取 sinopac.json 失敗：%s" % e)
    return None


# ----------------------------------------------------------------- 歷史資料
YF_MAP = {"rt": ("1d", "1m"), "5d": ("5d", "5m"), "5m": ("1mo", "5m"),
          "15m": ("1mo", "15m"), "60m": ("6mo", "60m"),
          "D": ("2y", "1d"), "W": ("5y", "1wk"), "M": ("max", "1mo")}
_hcache = {}


def yahoo_symbols(code, ex):
    if code == "t00": return ["^TWII"]
    if code == "o00": return ["^TWOII"]
    if ex == "otc": return [code + ".TWO", code + ".TW"]
    return [code + ".TW", code + ".TWO"]


def yahoo_history(code, ex, tf):
    rng, itv = YF_MAP[tf]
    last_err = None
    for sym in yahoo_symbols(code, ex):
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=%s&includePrePost=false"
               % (urllib.parse.quote(sym), rng, itv))
        try:
            j = http_json(url)
            res = j["chart"]["result"][0]
            ts = res.get("timestamp") or []
            qd = res["indicators"]["quote"][0]
            bars = []
            for i, t in enumerate(ts):
                o, h, l, c = qd["open"][i], qd["high"][i], qd["low"][i], qd["close"][i]
                if None in (o, h, l, c):
                    continue
                v = qd["volume"][i] or 0
                bars.append([int(t), round(o, 2), round(h, 2), round(l, 2), round(c, 2),
                             round(v / 1000.0, 1) if code not in ("t00", "o00") else 0])
            if not bars:
                raise ValueError("no data")
            meta = res.get("meta", {})
            return {"bars": bars, "prevClose": meta.get("chartPreviousClose") or meta.get("previousClose"),
                    "source": "Yahoo Finance"}
        except Exception as e:
            last_err = e
    raise RuntimeError("Yahoo 無資料 (%s)" % last_err)


def twse_daily(code, months=10):
    today = taipei_now().date()
    ym, y, m = [], today.year, today.month
    for _ in range(months):
        ym.append((y, m)); m -= 1
        if m == 0: m, y = 12, y - 1

    def one(p):
        url = ("https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date=%d%02d01&stockNo=%s"
               % (p[0], p[1], code))
        rows = []
        try:
            d = http_json(url)
        except Exception:
            return rows
        for r in d.get("data", []):
            try:
                ry, rm, rd = map(int, r[0].split("/"))
                o, h, l, c = [float(x.replace(",", "")) for x in (r[3], r[4], r[5], r[6])]
                rows.append([ts_of(dt.date(ry + 1911, rm, rd)), o, h, l, c,
                             round(float(r[1].replace(",", "")) / 1000.0, 1)])
            except Exception:
                continue
        return rows

    with ThreadPoolExecutor(2) as ex:
        res = list(ex.map(one, ym))
    bars = sorted([b for part in res for b in part], key=lambda b: b[0])
    if not bars:
        raise RuntimeError("證交所也沒有資料 (僅支援上市)")
    return bars


def aggregate(bars, kind):
    groups, order = {}, []
    for b in bars:
        d = dt.datetime.fromtimestamp(b[0], dt.timezone.utc).date()
        key = d.isocalendar()[:2] if kind == "W" else (d.year, d.month)
        if key not in groups:
            groups[key] = []; order.append(key)
        groups[key].append(b)
    out = []
    for k in order:
        g = groups[k]
        out.append([g[0][0], g[0][1], max(x[2] for x in g), min(x[3] for x in g), g[-1][4],
                    round(sum(x[5] for x in g), 1)])
    return out


def fetch_history(code, ex, tf):
    idx = code in ("t00", "o00")
    intra = tf in ("rt", "5d", "5m", "15m", "60m")
    order = []
    if SJ and not idx and intra: order.append("sj")
    order.append("yahoo")
    if tf in ("D", "W", "M") and not idx: order.append("twse")
    if SJ and not idx and not intra: order.append("sj")
    errs = []
    for name in order:
        try:
            if name == "sj":
                return SJ.history(code, tf)
            if name == "yahoo":
                return yahoo_history(code, ex, tf)
            bars = twse_daily(code)
            if tf != "D":
                bars = aggregate(bars, tf)
            return {"bars": bars, "prevClose": None, "source": "TWSE (備援)"}
        except Exception as e:
            errs.append("%s: %s" % (name, e))
    raise RuntimeError("; ".join(errs))


def get_history(code, ex, tf):
    if tf not in YF_MAP:
        raise ValueError("bad tf")
    key = (code, tf)
    ttl = 15 if tf == "rt" else 120
    hit = _hcache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    if DEMO:
        res = demo_history(code, tf)
    else:
        res = fetch_history(code, ex, tf)
    if tf == "rt" and res["bars"]:  # 只留最後一個交易日
        last_day = (res["bars"][-1][0] + 28800) // 86400
        res["bars"] = [b for b in res["bars"] if (b[0] + 28800) // 86400 == last_day]
    _hcache[key] = (time.time(), res)
    return res


# ----------------------------------------------------------------- 股票名稱搜尋
_names = {"t": 0, "data": []}
_names_lock = threading.Lock()
DEMO_NAMES = {
    "2330": "台積電", "2317": "鴻海", "2454": "聯發科", "2603": "長榮", "2881": "富邦金",
    "2882": "國泰金", "0050": "元大台灣50", "2412": "中華電", "2308": "台達電", "2303": "聯電",
    "3008": "大立光", "2891": "中信金", "1301": "台塑", "2002": "中鋼", "6488": "環球晶",
}
DEMO_PRICE = {"2330": 1000, "2317": 200, "2454": 1300, "2603": 200, "2881": 90, "2882": 70,
              "0050": 60, "2412": 120, "2308": 400, "2303": 50, "3008": 2200, "2891": 40,
              "1301": 50, "2002": 20, "6488": 400, "t00": 22000, "o00": 250}


def load_names():
    with _names_lock:
        if time.time() - _names["t"] < 12 * 3600:
            return _names["data"]
        data = []
        if DEMO:
            data = [(c, n, "otc" if c == "6488" else "tse") for c, n in DEMO_NAMES.items()]
        else:
            if SJ:
                data = SJ.names()
            if not data:
                try:
                    for r in http_json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"):
                        data.append((r["Code"], r["Name"], "tse"))
                except Exception:
                    pass
                try:
                    for r in http_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"):
                        data.append((r["SecuritiesCompanyCode"], r["CompanyName"], "otc"))
                except Exception:
                    pass
        for c, _, ex in data:
            KNOWN_EX[c] = ex
        _names["data"] = data
        _names["t"] = time.time() if data else time.time() - 12 * 3600 + 60  # 失敗 60 秒後重試
        return data


def search(q):
    q = q.strip().lower()
    if not q:
        return []
    data = load_names()
    exact, pre, sub = [], [], []
    for c, n, ex in data:
        if c.lower() == q: exact.append((c, n, ex))
        elif c.lower().startswith(q): pre.append((c, n, ex))
        elif q in n.lower(): sub.append((c, n, ex))
    return [{"code": c, "name": n, "ex": ex} for c, n, ex in (exact + pre + sub)[:10]]


# ----------------------------------------------------------------- 示範模式 (假資料)
_demo = {}
_demo_lock = threading.Lock()


def demo_seed(code):
    return zlib.crc32(code.encode())


def demo_state(code):
    s = _demo.get(code)
    if s is None:
        rnd = random.Random(demo_seed(code))
        base = DEMO_PRICE.get(code) or (20 + demo_seed(code) % 600)
        prev = rtick(base * (1 + rnd.uniform(-0.03, 0.03))) if code not in ("t00", "o00") else base
        price = prev * (1 + rnd.uniform(-0.02, 0.02))
        price = rtick(price) if code not in ("t00", "o00") else round(price, 2)
        s = {"prev": prev, "price": price, "open": price, "high": price, "low": price,
             "vol": rnd.randint(3000, 40000)}
        _demo[code] = s
    return s


def demo_ts():
    """示範用成交時間：盤中為現在；盤前/盤後/假日為最近一個交易日 13:30"""
    n = taipei_now()
    m = n.hour * 60 + n.minute
    if n.weekday() < 5 and 541 <= m <= 810:
        return int(time.time() * 1000)
    d = n.date()
    if n.weekday() >= 5 or m < 541:
        d -= dt.timedelta(days=1)
        while d.weekday() >= 5:
            d -= dt.timedelta(days=1)
    return int((ts_of(d) + 4.5 * 3600) * 1000)


def demo_quotes(codes):
    out = {}
    now = taipei_now()
    with _demo_lock:
        for c in codes:
            s = demo_state(c)
            idx = c in ("t00", "o00")
            step = (s["prev"] * 0.0006) if idx else tick_size(s["price"])
            s["price"] += random.choice([-2, -1, -1, 0, 1, 1, 2]) * step
            s["price"] = round(s["price"], 2) if idx else rtick(max(s["price"], 1))
            s["high"] = max(s["high"], s["price"]); s["low"] = min(s["low"], s["price"])
            tv = random.randint(1, 120)
            s["vol"] += tv
            p, t = s["price"], tick_size(s["price"])
            out[c] = {
                "code": c, "name": {"t00": "發行量加權股價指數", "o00": "櫃買指數"}.get(c, DEMO_NAMES.get(c, "示範" + c)),
                "full": None, "ex": "otc" if c in ("6488", "o00") else "tse",
                "price": p, "prev": s["prev"], "open": s["open"], "high": s["high"], "low": s["low"],
                "vol": s["vol"], "tv": tv,
                "ask": [] if idx else [round(p + t * (i + 1), 2) for i in range(5)],
                "askVol": [] if idx else [random.randint(5, 900) for _ in range(5)],
                "bid": [] if idx else [round(p - t * i, 2) for i in range(5)],
                "bidVol": [] if idx else [random.randint(5, 900) for _ in range(5)],
                "up": None if idx else rtick(s["prev"] * 1.1), "down": None if idx else rtick(s["prev"] * 0.9),
                "time": now.strftime("%H:%M:%S"), "date": now.strftime("%Y%m%d"),
                "ts": demo_ts(),
            }
    return out


def demo_history(code, tf):
    s = demo_state(code)
    rnd = random.Random(demo_seed(code) * 131 + zlib.crc32(tf.encode()))
    idx = code in ("t00", "o00")
    now = taipei_now()
    today = now.date()
    spec = {"rt": (60, 270), "5d": (300, 54 * 5), "5m": (300, 54 * 20),
            "15m": (900, 18 * 20), "60m": (3600, 5 * 20 * 3), "D": (86400, 500),
            "W": (604800, 260), "M": (2592000, 120)}[tf]
    step, n = spec
    ts = []
    if tf in ("D", "W", "M"):
        d = today
        for _ in range(n):
            ts.append(ts_of(d))
            d -= dt.timedelta(days=1 if tf == "D" else 7 if tf == "W" else 30)
            while tf == "D" and d.weekday() >= 5:
                d -= dt.timedelta(days=1)
        ts.reverse()
    else:
        per_day = 270 if tf == "rt" else {"5d": 54, "5m": 54, "15m": 18, "60m": 4}[tf]
        days = 1 if tf == "rt" else {"5d": 5, "5m": 20, "15m": 20, "60m": 60}[tf]
        d, dl = today, []
        if tf == "rt" and now.hour * 60 + now.minute < 541:   # 盤前：顯示上一個交易日
            d = today - dt.timedelta(days=1)
        while len(dl) < days:
            if d.weekday() < 5: dl.append(d)
            d -= dt.timedelta(days=1)
        dl.reverse()
        for dd in dl:
            base = ts_of(dd)  # 09:00 台北
            for k in range(per_day):
                ts.append(base + k * step)
        if tf == "rt":  # 盤中只到現在
            nowts = int(time.time())
            if dl[-1] == today and now.hour * 60 + now.minute < 810:
                ts = [t for t in ts if t <= nowts]
    if not ts:
        return {"bars": [], "prevClose": s["prev"], "source": "示範資料"}
    n = len(ts)
    p1 = s["price"]
    p0 = p1 * (1 + rnd.uniform(-0.2, 0.2)) if tf in ("D", "W", "M") else (s["open"] if tf == "rt" else p1 * (1 + rnd.uniform(-0.05, 0.05)))
    vol = 0.012 if tf in ("D", "W", "M") else 0.0015
    w, acc = [], 0
    for _ in range(n):
        acc += rnd.gauss(0, 1); w.append(acc)
    closes = []
    for i in range(n):
        frac = i / max(n - 1, 1)
        val = p0 + (p1 - p0) * frac + (w[i] - frac * w[-1]) * vol * p1 / 3
        closes.append(max(val, 1))
    closes[-1] = p1
    bars, prev = [], closes[0]
    for i in range(n):
        c = closes[i]; o = prev if i else p0
        h = max(o, c) * (1 + abs(rnd.gauss(0, vol / 2)))
        l = min(o, c) * (1 - abs(rnd.gauss(0, vol / 2)))
        r2 = (lambda x: round(x, 2)) if idx else rtick
        bars.append([ts[i], r2(o), r2(h), r2(l), r2(c), 0 if idx else rnd.randint(50, 3000)])
        prev = c
    return {"bars": bars, "prevClose": s["prev"], "source": "示範資料"}


# ----------------------------------------------------------------- HTTP 伺服器
def load_html():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        sys.exit("找不到 index.html，請確認它和 main.py 放在同一個資料夾。")


HTML = load_html()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        one = lambda k, d="": (q.get(k) or [d])[0]
        try:
            if u.path in ("/", "/index.html"):
                self.reply(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif u.path == "/api/info":
                self.reply(200, {"demo": DEMO, "source": "sinopac" if SJ else "mis"})
            elif u.path == "/api/quote":
                codes = [c for c in one("codes").split(",") if re.fullmatch(r"[0-9A-Za-z]{1,8}", c)][:80]
                self.reply(200, {"quotes": get_quotes(codes)})
            elif u.path == "/api/history":
                code, tf = one("code"), one("tf", "D")
                ex = one("ex")
                if not re.fullmatch(r"[0-9A-Za-z]{1,8}", code):
                    raise ValueError("bad code")
                self.reply(200, get_history(code, ex, tf))
            elif u.path == "/api/search":
                self.reply(200, {"results": search(one("q"))})
            else:
                self.reply(404, {"error": "not found"})
        except Exception as e:
            self.reply(502, {"error": "%s: %s" % (type(e).__name__, e)})


def main():
    global DEMO, CTX, SJ
    ap = argparse.ArgumentParser(description="台股即時看盤")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="預設只開放本機")
    ap.add_argument("--demo", action="store_true", help="示範模式 (假資料，不連網)")
    ap.add_argument("--source", choices=["auto", "mis", "sinopac"], default="auto",
                    help="資料來源：auto=有永豐金鑰就用永豐，否則用證交所 MIS")
    ap.add_argument("--sj-sim", action="store_true", help="永豐模擬環境")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--insecure", action="store_true", help="略過 SSL 憑證驗證 (macOS 憑證問題時使用)")
    a = ap.parse_args()
    DEMO = a.demo
    if a.insecure:
        CTX = ssl._create_unverified_context()
    if not DEMO and a.source != "mis":
        cred = load_sj_credentials()
        if cred:
            try:
                print("正在登入永豐 Shioaji …")
                SJ = Sinopac(cred[0], cred[1], simulation=a.sj_sim)
                print("永豐登入成功：即時報價/五檔/分鐘K 使用永豐 API，指數使用證交所 MIS")
            except Exception as e:
                print("⚠ 永豐登入失敗，改用證交所 MIS：%s" % e)
        elif a.source == "sinopac":
            print("⚠ 找不到金鑰 (環境變數 SJ_API_KEY / SJ_SECRET_KEY 或 sinopac.json)，改用證交所 MIS")
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    url = "http://%s:%d/" % ("127.0.0.1" if a.host in ("0.0.0.0", "") else a.host, a.port)
    print("台股即時看盤已啟動：%s %s" % (url, "(示範模式)" if DEMO else ""))
    print("按 Ctrl+C 結束")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已結束")


if __name__ == "__main__":
    main()
