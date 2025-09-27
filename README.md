# SmartMoney Bot

Telegram-бот для відслідковування китових угод у DeFi.

## 🚀 Деплой на Render
1. Склонуй цей репозиторій
2. Налаштуй `.env` (дивись `.env.example`)
3. Render автоматично підтягне `render.yaml` і створить Web Service + Redis

## ⚙️ Налаштування
- BOT_TOKEN: токен твого Telegram-бота (BotFather)
- CHAT_ID: твій Telegram ID (наприклад, через @userinfobot)
- COVALENT_KEY: API ключ Covalent
- REDIS_URL: підставиться автоматично від Render Redis

## 📜 Команди
- `/start` — інформація
- `/test` — приклад сигналу
- `/refresh` — оновити топ-гаманці
- `/stats` — коротка статистика останніх сигналів
