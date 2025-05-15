import os
import requests
import logging
from urllib.parse import urlparse

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load Telegram configuration from environment
RAW_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
CHAT_ID       = os.getenv("TG_CHAT_ID")

def get_bot_token():
    token = RAW_BOT_TOKEN or ""
    logger.debug(f"Raw BOT_TOKEN: {token}")
    # If someone stored the full API URL, extract the path
    if token.startswith("http"):
        try:
            path = urlparse(token).path  # e.g. '/bot<token>'
            if path.lower().startswith('/bot'):
                token = path[4:]
                logger.debug(f"Extracted token from URL: {token}")
        except Exception as e:
            logger.error(f"Error parsing BOT_TOKEN URL: {e}")
    # Remove 'bot' prefix if present
    if token.lower().startswith('bot'):
        token = token[3:]
        logger.debug(f"Stripped 'bot' prefix, token now: {token}")
    return token

def send_signal(message: str):
    bot_token = get_bot_token()
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = { 'chat_id': CHAT_ID, 'text': message }
    logger.info(f"Sending admin signal: {message}")
    resp = requests.post(url, json=payload)
    if not resp.ok:
        logger.error(f"Failed to send admin signal: HTTP {resp.status_code} - {resp.text}")
