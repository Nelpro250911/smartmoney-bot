import os, asyncio, time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any

import aiohttp
import redis.asyncio as redis
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.client.default import DefaultBotProperties
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

# =========================
# CONFIG
# =========================
load_dotenv()

BOT_TOKEN       = os.getenv("BOT_TOKEN")
CHAT_ID         = int(os.getenv("CHAT_ID", "0") or "0")
COVALENT_KEY    = os.getenv("COVALENT_KEY", "").strip()  # опціонально, але для моніторингу бажано
REDIS_URL       = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Кома-сепарований список гаманців (fallback замість DeBank)
# приклад: SEED_WALLETS=0xabc...,0xdef...,0x123...
SEED_WALLETS    = [w.strip().lower() for w in os.getenv("SEED_WALLETS", "").split(",") if w.strip()]

TOP_WALLETS_COUNT   = int(os.getenv("TOP_WALLETS_COUNT", "30"))
POLL_SECONDS        = int(os.getenv("POLL_SECONDS", "60"))
MIN_TRADE_USD       = float(os.getenv("MIN_TRADE_USD", "1000"))
MIN_LIQ_USD         = float(os.getenv("MIN_LIQ_USD", "500000"))
CO_WHALES_24H       = int(os.getenv("STRONG_SIGNAL_COWhales", "2"))

EXPLORERS = {
    1: "https://etherscan.io/tx/",
    56: "https://bscscan.com/tx/",
    137: "https://polygonscan.com/tx/",
    42161: "https://arbiscan.io/tx/",
    10: "https://optimistic.etherscan.io/tx/",
    8453: "https://basescan.org/tx/",
}

if not BOT_TOKEN or not CHAT_ID:
    raise SystemExit("❌ Set BOT_TOKEN and CHAT_ID in environment")

# =========================
# GLOBALS
# =========================
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
rds: redis.Redis = redis.from_url(REDIS_URL, decode_responses=True)
scheduler = AsyncIOScheduler()

# =========================
# MODELS
# =========================
@dataclass
class WalletMeta:
    address: str
    est_roi30: float = 30.0
    winrate: float = 60.0
    chain_id: int = 1

@dataclass
class TradeSignal:
    wallet: str
    action: str
    token: str
    token_symbol: str
    volume_usd: float
    roi: float
    winrate: float
    liquidity_usd: float
    whales_count_24h: int
    token_age_days: int
    tx_hash: str
    chain_id: int
    link: str  # DexScreener pair url якщо є

# =========================
# REDIS KEYS
# =========================
def k_top_wallets() -> str: return "sm:top_wallets"
def k_wallet_meta(addr:str) -> str: return f"sm:wm:{addr}"
def k_seen_tx(addr:str) -> str: return f"sm:seen:{addr}"
def k_token_whales(token:str) -> str: return f"sm:whales:{token}"

# =========================
# SCORE
# =========================
def calc_score(roi, winrate, volume_usd, liquidity_usd, whales_count, token_age_days):
    ROI_score = -1 if roi < 0 else 1 if roi < 20 else 2 if roi < 50 else 3
    WR_score  = 0 if winrate < 40 else 1 if winrate < 60 else 2 if winrate < 80 else 3
    VOL_score = 0 if volume_usd < 1000 else 1 if volume_usd < 10000 else 2 if volume_usd < 100000 else 3
    LIQ_score = 0 if liquidity_usd < 500_000 else 1 if liquidity_usd < 5_000_000 else 2 if liquidity_usd < 50_000_000 else 3
    WHALE_score = 0 if whales_count == 1 else 2 if whales_count <= 3 else 3 if whales_count <= 5 else 4
    AGE_score = -1 if token_age_days < 7 else 0 if token_age_days < 30 else 1 if token_age_days < 180 else 2
    total = ROI_score + WR_score + VOL_score + LIQ_score + WHALE_score + AGE_score
    if total <= 3: stars = 1
    elif total <= 6: stars = 2
    elif total <= 9: stars = 3
    elif total <= 12: stars = 4
    else: stars = 5
    return total, stars

# =========================
# HTTP
# =========================
class Http:
    def __init__(self, headers: Optional[Dict[str, str]] = None):
        self.session: Optional[aiohttp.ClientSession] = None
        self.headers = headers or {}

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=25),
            headers=self.headers
        )
        return self

    async def __aexit__(self, *_):
        if self.session:
            await self.session.close()

    async def get_json(self, url:str, params:Dict[str,Any]=None) -> Dict[str,Any]:
        async with self.session.get(url, params=params) as r:
            if r.status != 200:
                return {}
            ct = r.headers.get("Content-Type","")
            if "application/json" not in ct:
                return {}
            return await r.json()

# =========================
# Covalent: останні транзакції
# =========================
async def covalent_wallet_txs(http: Http, wallet:str, chain_id:int=1, page_size:int=20) -> List[Dict[str,Any]]:
    if not COVALENT_KEY:
        return []
    url = f"https://api.covalenthq.com/v1/{chain_id}/address/{wallet}/transactions_v3/"
    params = {"key": COVALENT_KEY, "page-size": str(page_size), "no-logs":"false"}
    js = await http.get_json(url, params=params)
    return ((js.get("data") or {}).get("items") or [])

# Евристика BUY по ERC-20 Transfer -> to == wallet
def parse_erc20_buys(wallet:str, tx:Dict[str,Any]) -> List[Tuple[str,str,float]]:
    out = []
    for ev in (tx.get("log_events") or []):
        dec = (ev or {}).get("decoded") or {}
        if dec.get("name") != "Transfer":
            continue
        to_addr = None
        for p in dec.get("params") or []:
            if p.get("name") == "to":
                to_addr = (p.get("value") or "").lower()
        if to_addr != wallet.lower():
            continue
        token_addr = (ev.get("sender_address") or "").lower()
        token_symbol = ev.get("sender_contract_ticker_symbol") or "TOKEN"
        tx_usd = float(tx.get("value_quote") or 0.0)
        approx = tx_usd/3 if tx_usd>0 else 0.0
        out.append((token_addr, token_symbol, approx))
    return out

# =========================
# DexScreener info
# =========================
async def dexs_token_info(http: Http, token: str) -> tuple[float, float, int, str, str]:
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token.lower()}"
    js = await http.get_json(url)
    pairs = (js or {}).get("pairs") or []
    if not pairs:
        return 0.0, 0.0, 10**6, "", ""
    best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0.0))
    liq = float((best.get("liquidity") or {}).get("usd") or 0.0)
    price = float(best.get("priceUsd") or 0.0)
    created_ms = best.get("pairCreatedAt") or best.get("createdAt") or 0
    age_days = max(0, int((time.time()*1000 - float(created_ms))/1000/86400)) if created_ms else 10**6
    pair_url  = best.get("url") or ""
    chain_slug = (best.get("chainId") or "").lower()
    return liq, price, age_days, pair_url, chain_slug

# =========================
# Redis helpers
# =========================
async def save_top_wallets(wallets: List[WalletMeta]):
    if not wallets: return
    pipe = rds.pipeline()
    for w in wallets:
        pipe.sadd(k_top_wallets(), w.address)
        pipe.hset(k_wallet_meta(w.address), mapping={
            "roi30": f"{w.est_roi30:.6f}",
            "winrate": f"{w.winrate:.6f}",
            "chain_id": str(w.chain_id),
            "updated_at": str(int(time.time()))
        })
    await pipe.execute()

async def load_top_wallets() -> List[WalletMeta]:
    addrs = await rds.smembers(k_top_wallets())
    out: List[WalletMeta] = []
    for a in addrs:
        h = await rds.hgetall(k_wallet_meta(a))
        if not h: continue
        out.append(WalletMeta(
            address=a,
            est_roi30=float(h.get("roi30","30")),
            winrate=float(h.get("winrate","60")),
            chain_id=int(h.get("chain_id","1"))
        ))
    return out

async def mark_seen(addr:str, tx_hash:str, ttl:int=14*24*3600):
    await rds.sadd(k_seen_tx(addr), tx_hash)
    await rds.expire(k_seen_tx(addr), ttl)

async def is_seen(addr:str, tx_hash:str) -> bool:
    return bool(await rds.sismember(k_seen_tx(addr), tx_hash))

async def bump_token_whale(token:str, wallet:str):
    now = int(time.time())
    await rds.zadd(k_token_whales(token), {wallet: now})
    await rds.zremrangebyscore(k_token_whales(token), 0, now - 3*24*3600)

async def count_token_whales_24h(token:str) -> int:
    now = int(time.time())
    vals = await rds.zrangebyscore(k_token_whales(token), now-24*3600, now, withscores=False)
    return len(set(vals))

# =========================
# Message formatting
# =========================
def format_signal(sig: TradeSignal) -> str:
    _, stars = calc_score(
        sig.roi, sig.winrate, sig.volume_usd,
        sig.liquidity_usd, sig.whales_count_24h, sig.token_age_days
    )
    star_str = "⭐" * stars
    scan_tx = EXPLORERS.get(sig.chain_id, "") + sig.tx_hash if EXPLORERS.get(sig.chain_id) else sig.tx_hash
    token_scan = ""
    if sig.chain_id in (1,56,137,42161,10,8453):
        host = EXPLORERS[sig.chain_id].replace("/tx/","")
        token_scan = f"{host}/token/{sig.token}"
    primary_link = sig.link or scan_tx
    ds_search = f"https://dexscreener.com/search?q={sig.token}"
    parts = [
        f"{star_str} Whale Signal",
        f"<b>Кошелек:</b> {sig.wallet}",
        f"<b>Дія:</b> {sig.action}",
        f"<b>Токен:</b> {sig.token_symbol} (<code>{sig.token}</code>)",
        f"<b>Сума угоди:</b> ${sig.volume_usd:,.2f}",
        f"<b>ROI (30д):</b> {sig.roi:.0f}% | <b>Winrate:</b> {sig.winrate:.0f}%",
        f"<b>Ліквідність:</b> ${sig.liquidity_usd:,.0f}",
        f"<b>Інші кити за 24h у токені:</b> {sig.whales_count_24h}",
        f"<b>Вік токена:</b> {sig.token_age_days} днів",
        f"🔗 <a href='{primary_link}'>Переглянути</a>",
    ]
    if token_scan:
        parts.append(f"🧭 <a href='{token_scan}'>Token on Scan</a>")
    parts.append(f"🔎 <a href='{ds_search}'>Search on DexScreener</a>")
    return "\n".join(parts)

# =========================
# CORE: refresh + monitor (без DeBank)
# =========================
async def refresh_top_wallets_job():
    # Тільки з SEED_WALLETS — без зовнішніх API, щоб не падати
    if not SEED_WALLETS:
        return
    wl = [WalletMeta(address=a) for a in SEED_WALLETS]
    await save_top_wallets(wl)
    await bot.send_message(CHAT_ID, f"🔄 Оновив список китів (SEED_WALLETS): {len(wl)} адрес.")

async def monitor_job():
    try:
        wallets = await load_top_wallets()
        if not wallets:
            # якщо ще нічого немає — підтягуємо із SEED_WALLETS
            await refresh_top_wallets_job()
            wallets = await load_top_wallets()
        if not wallets:
            return

        async with Http() as http:
            for w in wallets:
                txs = await covalent_wallet_txs(http, w.address, w.chain_id, page_size=20)
                for tx in txs[:5]:
                    txh = tx.get("tx_hash") or ""
                    if not txh or await is_seen(w.address, txh):
                        continue

                    buys = parse_erc20_buys(w.address, tx)
                    for token_addr, token_symbol, approx_usd in buys:
                        if approx_usd < MIN_TRADE_USD:
                            continue
                        liq, price, age_days, pair_url, chain_slug = await dexs_token_info(http, token_addr)
                        if liq < MIN_LIQ_USD:
                            continue

                        await bump_token_whale(token_addr, w.address)
                        co_whales = await count_token_whales_24h(token_addr)

                        sig = TradeSignal(
                            wallet=w.address,
                            action="BUY",
                            token=token_addr,
                            token_symbol=token_symbol or "TOKEN",
                            volume_usd=approx_usd,
                            roi=w.est_roi30,
                            winrate=w.winrate,
                            liquidity_usd=liq,
                            whales_count_24h=co_whales,
                            token_age_days=age_days if age_days < 10**6 else 365,
                            tx_hash=txh,
                            chain_id=w.chain_id,
                            link=pair_url
                        )

                        _, stars = calc_score(sig.roi, sig.winrate, sig.volume_usd,
                                              sig.liquidity_usd, sig.whales_count_24h, sig.token_age_days)
                        if stars < 3:
                            continue

                        await bot.send_message(CHAT_ID, format_signal(sig), disable_web_page_preview=False)

                    await mark_seen(w.address, txh)
    except Exception as e:
        try:
            await bot.send_message(CHAT_ID, f"⚠️ Помилка моніторингу: {e}")
        except:
            pass

# =========================
# TELEGRAM
# =========================
@dp.message(Command("start"))
async def start_cmd(m: Message):
    await m.answer("👋 Привіт! Я SmartMoney Bot. Команди: /test, /refresh")

@dp.message(Command("refresh"))
async def refresh_cmd(m: Message):
    await refresh_top_wallets_job()
    await m.answer("✅ Оновив список китів із SEED_WALLETS.")

@dp.message(Command("test"))
async def test_cmd(m: Message):
    # демо: USDC, щоб перевірити лінки
    async with Http() as http:
        token = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48".lower()
        liq, price, age_days, pair_url, _ = await dexs_token_info(http, token)
    demo = TradeSignal(
        wallet="0x123...abc",
        action="BUY",
        token=token,
        token_symbol="USDC",
        volume_usd=25000,
        roi=42, winrate=70,
        liquidity_usd=max(liq, 12_500_000),
        whales_count_24h=3,
        token_age_days=max(age_days, 365),
        tx_hash="0xabc123",
        chain_id=1,
        link=pair_url
    )
    await m.answer(format_signal(demo), disable_web_page_preview=False)

# =========================
# MAIN
# =========================
async def main():
    scheduler.add_job(monitor_job, "interval", seconds=POLL_SECONDS, id="monitor")
    scheduler.add_job(refresh_top_wallets_job, "cron", hour=6, minute=0, id="refresh_daily")
    scheduler.start()
    await bot.send_message(CHAT_ID, "✅ SmartMoney запущено. Починаю моніторинг.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
