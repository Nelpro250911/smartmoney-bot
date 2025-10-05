*** Begin Patch ***
*** Update File: smartmoney_full.py
@@
 from dotenv import load_dotenv
 
 # =========================
 # CONFIG
 # =========================
 load_dotenv()
 
 BOT_TOKEN       = os.getenv("BOT_TOKEN")
 CHAT_ID         = int(os.getenv("CHAT_ID", "0") or "0")
 COVALENT_KEY    = os.getenv("COVALENT_KEY")  # обов'язково для моніторингу
 REDIS_URL       = os.getenv("REDIS_URL", "redis://localhost:6379/0")
+DEBANK_ACCESS_KEY = os.getenv("DEBANK_ACCESS_KEY", "").strip()
 
 TOP_WALLETS_COUNT   = int(os.getenv("TOP_WALLETS_COUNT", "30"))
 POLL_SECONDS        = int(os.getenv("POLL_SECONDS", "60"))
 MIN_TRADE_USD       = float(os.getenv("MIN_TRADE_USD", "1000"))
 MIN_LIQ_USD         = float(os.getenv("MIN_LIQ_USD", "500000"))
 CO_WHALES_24H       = int(os.getenv("STRONG_SIGNAL_COWhales", "2"))
@@
 class Http:
-    def __init__(self):
+    def __init__(self, headers: Optional[Dict[str, str]] = None):
         self.session: Optional[aiohttp.ClientSession] = None
+        self.headers = headers or {}
 
     async def __aenter__(self):
-        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25))
+        self.session = aiohttp.ClientSession(
+            timeout=aiohttp.ClientTimeout(total=25),
+            headers=self.headers
+        )
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
@@
-async def debank_top_wallets(http: Http, count:int=TOP_WALLETS_COUNT) -> List[WalletMeta]:
-    url = "https://openapi.debank.com/v1/ranking/list"
-    params = {"type":"profit_30d", "count": str(count)}
-    js = await http.get_json(url, params=params)
-    out: List[WalletMeta] = []
-    for item in (js.get("data") or []):
-        addr = (item.get("id") or "").lower()
-        prof30 = float(item.get("profit_30d") or 0.0)
-        portv  = float(item.get("portfolio_usd_value") or 0.0)
-        roi_pct = (prof30/portv*100.0) if portv>0 else 0.0
-        out.append(WalletMeta(address=addr, est_roi30=roi_pct, winrate=60.0, chain_id=1))
-    return out
+async def debank_top_wallets(http: Http, count:int=TOP_WALLETS_COUNT) -> List[WalletMeta]:
+    """
+    Спроба отримати топ-гаманці з DeBank pro-openapi.
+    Якщо немає ключа або ендпоінт недоступний у вашому тарифі — повертаємо [] (щоб не падати).
+    """
+    if not DEBANK_ACCESS_KEY:
+        return []
+
+    # ⚠️ У публічній версії 'ranking/list' може бути відсутній.
+    # Тут просто перевіримо, що ключ працює, і повернемо [] як м'який fallback.
+    # Коли матимеш робочий список адрес — заміни логіку нижче на свій ендпоінт.
+    test_url = "https://pro-openapi.debank.com/v1/user/used_chain_list"
+    params = {"id": "0x0000000000000000000000000000000000000000"}
+    js = await http.get_json(test_url, params=params)
+    if not js:
+        return []
+
+    # TODO: підстав реальний ендпоінт для відбору «топ китів» і розпарсь тут адреси.
+    return []
@@
-async def refresh_top_wallets_job():
-    async with Http() as http:
-        wl = await debank_top_wallets(http, TOP_WALLETS_COUNT)
-        if wl:
-            await save_top_wallets(wl)
-            await bot.send_message(CHAT_ID, f"🔄 Оновив топ-гаманці з DeBank: {len(wl)} адрес.")
+async def refresh_top_wallets_job():
+    try:
+        headers = {"AccessKey": DEBANK_ACCESS_KEY} if DEBANK_ACCESS_KEY else {}
+        async with Http(headers=headers) as http:
+            wl = await debank_top_wallets(http, TOP_WALLETS_COUNT)
+            if wl:
+                await save_top_wallets(wl)
+                await bot.send_message(CHAT_ID, f"🔄 Оновив топ-гаманці з DeBank: {len(wl)} адрес.")
+    except Exception as e:
+        # Не валимо планувальник — просто лог/нотифікація
+        try:
+            await bot.send_message(CHAT_ID, f"⚠️ DeBank недоступний: {e}")
+        except:
+            pass
@@
 async def monitor_job():
-    wallets = await load_top_wallets()
-    if not wallets:
-        await refresh_top_wallets_job()
-        wallets = await load_top_wallets()
-        if not wallets:
-            return
+    try:
+        wallets = await load_top_wallets()
+        if not wallets:
+            await refresh_top_wallets_job()
+            wallets = await load_top_wallets()
+
+        # Якщо досі немає — використай тимчасовий seed (встав свої адреси)
+        if not wallets:
+            seed = [
+                # "0x....",  # додай кілька відомих китів сюди
+            ]
+            for a in seed:
+                await save_top_wallets([WalletMeta(address=a.lower(), est_roi30=30.0, winrate=60.0, chain_id=1)])
+            wallets = await load_top_wallets()
+
+        if not wallets:
+            return
 
-    async with Http() as http:
-        for w in wallets:
-            txs = await covalent_wallet_txs(http, w.address, w.chain_id, page_size=20)
-            for tx in txs[:5]:
-                txh = tx.get("tx_hash") or ""
-                if not txh or await is_seen(w.address, txh):
-                    continue
-
-                buys = parse_erc20_buys(w.address, tx)
-                for token_addr, token_symbol, approx_usd in buys:
-                    if approx_usd < MIN_TRADE_USD:
-                        continue
-
-                    liq, price, age_days, pair_url, chain_slug = await dexs_token_info(http, token_addr)
-                    if liq < MIN_LIQ_USD:
-                        continue
-
-                    await bump_token_whale(token_addr, w.address)
-                    co_whales = await count_token_whales_24h(token_addr)
-
-                    sig = TradeSignal(
-                        wallet=w.address,
-                        action="BUY",
-                        token=token_addr,
-                        token_symbol=token_symbol or "TOKEN",
-                        volume_usd=approx_usd,
-                        roi=w.est_roi30,
-                        winrate=w.winrate,
-                        liquidity_usd=liq,
-                        whales_count_24h=co_whales,
-                        token_age_days=age_days if age_days < 10**6 else 365,
-                        tx_hash=txh,
-                        chain_id=w.chain_id,
-                        link=pair_url  # головне: даємо готовий url пари, якщо він є
-                    )
-
-                    # фільтр: показувати лише 3⭐+
-                    _, stars = calc_score(sig.roi, sig.winrate, sig.volume_usd,
-                                          sig.liquidity_usд, sig.whales_count_24h, sig.token_age_days)
-                    if stars < 3:
-                        continue
-
-                    await bot.send_message(CHAT_ID, format_signal(sig), disable_web_page_preview=False)
-
-                await mark_seen(w.address, txh)
+        async with Http() as http:
+            for w in wallets:
+                txs = await covalent_wallet_txs(http, w.address, w.chain_id, page_size=20)
+                for tx in txs[:5]:
+                    txh = tx.get("tx_hash") or ""
+                    if not txh or await is_seen(w.address, txh):
+                        continue
+
+                    buys = parse_erc20_buys(w.address, tx)
+                    for token_addr, token_symbol, approx_usd in buys:
+                        if approx_usд < MIN_TRADE_USD:
+                            continue
+
+                        liq, price, age_days, pair_url, chain_slug = await dexs_token_info(http, token_addr)
+                        if liq < MIN_LIQ_USD:
+                            continue
+
+                        await bump_token_whale(token_addr, w.address)
+                        co_whales = await count_token_whales_24h(token_addr)
+
+                        sig = TradeSignal(
+                            wallet=w.address,
+                            action="BUY",
+                            token=token_addr,
+                            token_symbol=token_symbol or "TOKEN",
+                            volume_usd=approx_usd,
+                            roi=w.est_roi30,
+                            winrate=w.winrate,
+                            liquidity_usd=liq,
+                            whales_count_24h=co_whales,
+                            token_age_days=age_days if age_days < 10**6 else 365,
+                            tx_hash=txh,
+                            chain_id=w.chain_id,
+                            link=pair_url
+                        )
+
+                        _, stars = calc_score(sig.roi, sig.winrate, sig.volume_usd,
+                                              sig.liquidity_usd, sig.whales_count_24h, sig.token_age_days)
+                        if stars < 3:
+                            continue
+
+                        await bot.send_message(CHAT_ID, format_signal(sig), disable_web_page_preview=False)
+
+                    await mark_seen(w.address, txh)
+    except Exception as e:
+        try:
+            await bot.send_message(CHAT_ID, f"⚠️ Помилка моніторингу: {e}")
+        except:
+            pass
*** End Patch ***
