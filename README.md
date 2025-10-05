# SmartMoney Bot

Telegram-бот для відслідковування китових угод у DeFi з оцінкою сигналів ⭐.

## Запуск локально
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # заповни BOT_TOKEN, CHAT_ID, COVALENT_KEY
python smartmoney_full.py
