# notify_signals.py
import os
import requests

BOT_TOKEN = os.getenv("7292085508:AAHlCSijLTmfdJilxq0ykZWh0bO6BwaQjD0")
CHAT_ID   = os.getenv("-1002263307038")
TELEGRAM_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

def send_signal(message: str):
    payload = {"chat_id": CHAT_ID, "text": message}
    resp = requests.post(TELEGRAM_URL, json=payload)
    if not resp.ok:
        print("Failed to send:", resp.text)

if __name__ == "__main__":
    # Example usage; you’ll hook this into Freqtrade events later
    send_signal("🚨 TEST SIGNAL: BTC/USDT BUY @ 50000 USDT")
