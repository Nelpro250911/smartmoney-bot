# smartmoney_full.py
import os, asyncio, time, datetime as dt
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
COVALENT_KEY    = os.getenv("COVALENT_KEY")  # не обов'язково для демо
REDIS_URL       = os.getenv("REDIS_URL", "redis://localhost:6379/0")

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

# Globals
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
rds: redis.Redis = redis.from_url(REDIS_URL, decode_responses=True)
scheduler = AsyncIOScheduler()

# =========================
# DATA MODELS
# =========================
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
    link: str  # може бути DexScreener pair url або пусто

# =========================
# SCORING (скорочений з нашої матриці)
# =========================
def calc_score(roi, winrate, volume_usd, liquidity_usd, whales_count, token_age_days):
    # ROI
    ROI_score = -1 if roi < 0 else 1 if roi < 20 else 2 if roi < 50 else 3
    # Winrate
    WR_score = 0 if winrate < 40 else 1 if winrate < 60 else 2 if winrate < 80 else 3
    # Volume
    VOL_score = 0 if volume_usd < 1000 else 1 if volume_usd < 10000 else 2 if volume_usd < 100000 else 3
    # Liquidity
    LIQ_score = 0 if liquidity_usd < 500_000 else 1 if liquidity_usd < 5_000_000 else 2 if liquidity_usd < 50_000_000 else 3
    # Whales count
    WHALE_score = 0 if whales_count == 1 else 2 if whales_count <= 3 else 3 if whales_count <= 5 else 4
    # Token age
    AGE_score = -1 if token_age_days < 7 else 0 if token_age_days < 30 else 1 if token_age_days < 180 else 2
    total = ROI_score + WR_score + VOL_score + LIQ_score + WHALE_score + AGE_score
    if total <= 3: stars = 1
    elif total <= 6: stars = 2
    elif total <= 9: stars = 3
    elif total <= 12: stars = 4
    else: stars = 5
    return total, stars

# =========================
# HTTP helper
# =========================
class Http:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25))
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
# DexScreener: безпечна інфа по токену
# =========================
async def dexs_token_info(http: Http, token: str) -> tuple[float, float, int, str, str]:
    """
    Повертає:
      (best_liquidity_usd, price_usd, token_age_days, pair_url, chain_slug)
    pair_url — готова правильна URL пари від DexScreener (якщо є),
    chain_slug — рядок мережі (ethereum, bsc, arbitrum, base, ...).
    """
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token.lower()}"
    js = await http.get_json(url)
    pairs = (js or {}).get("pairs") or []
    if not pairs:
        return 0.0, 0.0, 10**6, "", ""
    best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0.0))
    liq = float((best.get("liquidity") or {}).get("usd") or 0.0)
    price = float(best.get("priceUsd") or 0.0)
    created_ms = best.get("pairCreatedAt") or best.get("createdAt") or 0
    if created_ms:
        age_days = max(0, int((time.time()*1000 - float(created_ms))/1000/86400))
    else:
        age_days = 10**6
    pair_url  = best.get("url") or ""
    chain_slug = (best.get("chainId") or "").lower()
    return liq, price, age_days, pair_url, chain_slug

# =========================
# Формування повідомлення з безпечними лінками
# =========================
def format_signal(sig: TradeSignal) -> str:
    score, stars = calc_score(
        sig.roi, sig.winrate, sig.volume_usd,
        sig.liquidity_usd, sig.whales_count_24h, sig.token_age_days
    )
    star_str = "⭐" * stars

    # лінк на транзакцію у сканері (завжди є)
    scan_tx = EXPLORERS.get(sig.chain_id, "") + sig.tx_hash if EXPLORERS.get(sig.chain_id) else sig.tx_hash

    # лінк на токен у сканері мережі (etherscan-сумісний)
    token_scan = ""
    if sig.chain_id in (1, 56, 137, 42161, 10, 8453):
        host = EXPLORERS[sig.chain_id].replace("/tx/","")
        token_scan = f"{host}/token/{sig.token}"

    # якщо DexScreener дав url пари — використовуємо, інакше tx
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
# DEMO monitor (залишив простий приклад)
# =========================
async def monitor_job():
    # демо-сигнал — щоб перевірити формат і посилання
    demo = TradeSignal(
        wallet="0x123...abc",
        action="BUY",
        token="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48".lower(),  # приклад контракту (USDC на ETH)
        token_symbol="USDC",
        volume_usd=25000,
        roi=42,
        winrate=70,
        liquidity_usd=12_500_000,
        whales_count_24h=3,
        token_age_days=365*3,
        tx_hash="0xabc123",
        chain_id=1,
        link=""  # у бою підставляй pair_url із dexs_token_info(...)
    )
    await bot.send_message(CHAT_ID, format_signal(demo))

# =========================
# TELEGRAM HANDLERS
# =========================
@dp.message(Command("start"))
async def start_cmd(m: Message):
    await m.answer(
        f"👋 Привіт! Я SmartMoney Bot.\n"
        f"Команди: /test — тест-сигнал. У продакшені підключи свої API і логику моніторингу."
    )

@dp.message(Command("test"))
async def test_cmd(m: Message):
    await monitor_job()

# =========================
# MAIN
# =========================
async def main():
    scheduler.add_job(monitor_job, "interval", seconds=POLL_SECONDS)
    scheduler.start()
    await bot.send_message(CHAT_ID, "✅ SmartMoney запущено. Починаю моніторинг.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
