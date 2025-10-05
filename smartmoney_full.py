import os, asyncio, json, math, time, datetime as dt
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any

import aiohttp
import redis.asyncio as redis
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

# =========================
# CONFIG
# =========================
load_dotenv()

BOT_TOKEN       = os.getenv("BOT_TOKEN")
CHAT_ID         = int(os.getenv("CHAT_ID", "0") or "0")
COVALENT_KEY    = os.getenv("COVALENT_KEY")
REDIS_URL       = os.getenv("REDIS_URL", "redis://localhost:6379/0")

TOP_WALLETS_COUNT   = int(os.getenv("TOP_WALLETS_COUNT", "30"))
POLL_SECONDS        = int(os.getenv("POLL_SECONDS", "60"))
MIN_TRADE_USD       = float(os.getenv("MIN_TRADE_USD", "1000"))
MIN_LIQ_USD         = float(os.getenv("MIN_LIQ_USD", "500000"))
CO_WHALES_24H       = int(os.getenv("STRONG_SIGNAL_COWhales", "2"))

# Chain to explorer
EXPLORERS = {
    1: "https://etherscan.io/tx/",
    56: "https://bscscan.com/tx/",
    137: "https://polygonscan.com/tx/",
    42161: "https://arbiscan.io/tx/",
    10: "https://optimistic.etherscan.io/tx/",
    8453: "https://basescan.org/tx/",
}

# Safety checks
if not BOT_TOKEN or not COVALENT_KEY or not CHAT_ID:
    raise SystemExit("❌ Set BOT_TOKEN, COVALENT_KEY, CHAT_ID in environment")

# Globals
bot = Bot(BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()
rds: redis.Redis = redis.from_url(REDIS_URL, decode_responses=True)
scheduler = AsyncIOScheduler()

# =========================
# DATA MODELS
# =========================
@dataclass
class WalletMeta:
    address: str
    est_roi30: float
    winrate: float
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
    link: str

# =========================
# SCORING ENGINE
# =========================
def calc_score(roi, winrate, volume_usd, liquidity_usd, whales_count, token_age_days) -> Tuple[int, int]:
    if roi < 0: ROI_score = -1
    elif roi < 20: ROI_score = 1
    elif roi < 50: ROI_score = 2
    else: ROI_score = 3

    if winrate < 40: WR_score = 0
    elif winrate < 60: WR_score = 1
    elif winrate < 80: WR_score = 2
    else: WR_score = 3

    if volume_usd < 1000: VOL_score = 0
    elif volume_usd < 10000: VOL_score = 1
    elif volume_usd < 100000: VOL_score = 2
    else: VOL_score = 3

    if liquidity_usd < 500_000: LIQ_score = 0
    elif liquidity_usd < 5_000_000: LIQ_score = 1
    elif liquidity_usd < 50_000_000: LIQ_score = 2
    else: LIQ_score = 3

    if whales_count == 1: WHALE_score = 0
    elif whales_count <= 3: WHALE_score = 2
    elif whales_count <= 5: WHALE_score = 3
    else: WHALE_score = 4

    if token_age_days < 7: AGE_score = -1
    elif token_age_days < 30: AGE_score = 0
    elif token_age_days < 180: AGE_score = 1
    else: AGE_score = 2

    total = ROI_score + WR_score + VOL_score + LIQ_score + WHALE_score + AGE_score

    if total <= 3: stars = 1
    elif total <= 6: stars = 2
    elif total <= 9: stars = 3
    elif total <= 12: stars = 4
    else: stars = 5

    return total, stars

def format_signal(sig: TradeSignal) -> str:
    score, stars = calc_score(sig.roi, sig.winrate, sig.volume_usd,
                              sig.liquidity_usd, sig.whales_count_24h, sig.token_age_days)
    star_str = "⭐" * stars
    link = sig.link or (EXPLORERS.get(sig.chain_id, "") + sig.tx_hash)
    return (
        f"{star_str} Whale Signal\n"
        f"<b>Кошелек:</b> {sig.wallet}\n"
        f"<b>Дія:</b> {sig.action}\n"
        f"<b>Токен:</b> {sig.token_symbol} (<code>{sig.token}</code>)\n"
        f"<b>Сума угоди:</b> ${sig.volume_usd:,.2f}\n"
        f"<b>ROI (30д):</b> {sig.roi:.0f}% | <b>Winrate:</b> {sig.winrate:.0f}%\n"
        f"<b>Ліквідність:</b> ${sig.liquidity_usd:,.0f}\n"
        f"<b>Інші кити за 24h у токені:</b> {sig.whales_count_24h}\n"
        f"<b>Вік токена:</b> {sig.token_age_days} днів\n"
        f"🔗 <a href='{link}'>Переглянути</a>"
    )

# =========================
# API HELPERS
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
            return await r.json()

# Dummy jobs (скорочено, щоб не було тисяч рядків)
async def refresh_top_wallets_job():
    await bot.send_message(CHAT_ID, "🔄 Оновив топ-гаманці (demo).")

async def monitor_job():
    demo = TradeSignal(
        wallet="0x123...abc",
        action="BUY",
        token="0xTOKEN",
        token_symbol="PEPE",
        volume_usd=25000,
        roi=42,
        winrate=70,
        liquidity_usd=12_500_000,
        whales_count_24h=3,
        token_age_days=150,
        tx_hash="0xabc123",
        chain_id=1,
        link="https://dexscreener.com/ethereum/0xPAIR"
    )
    await bot.send_message(CHAT_ID, format_signal(demo))

# =========================
# TELEGRAM HANDLERS
# =========================
@dp.message(Command("start"))
async def start_cmd(m: Message):
    await m.answer("👋 Привіт! Я SmartMoney Bot.")

@dp.message(Command("test"))
async def test_cmd(m: Message):
    await monitor_job()

# =========================
# MAIN
# =========================
async def main():
    scheduler.add_job(monitor_job, "interval", seconds=POLL_SECONDS)
    scheduler.start()
    await bot.send_message(CHAT_ID, "✅ SmartMoney запущено.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
