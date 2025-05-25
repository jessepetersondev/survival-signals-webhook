import os
import requests
import logging
import time
from urllib.parse import urlparse

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger(__name__)

# Load Telegram configuration from environment
RAW_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
CHAT_ID = os.getenv("TG_CHAT_ID")

# Validate required environment variables
if not RAW_BOT_TOKEN:
    logger.critical("TG_BOT_TOKEN environment variable is not set")
    raise ValueError("TG_BOT_TOKEN environment variable is required")
    
if not CHAT_ID:
    logger.critical("TG_CHAT_ID environment variable is not set")
    raise ValueError("TG_CHAT_ID environment variable is required")

def get_bot_token():
    token = RAW_BOT_TOKEN or ""
    # Mask token in logs for security
    masked_token = token[:4] + "..." + token[-4:] if len(token) > 8 else "***"
    logger.debug(f"Processing BOT_TOKEN: {masked_token}")
    
    # If someone stored the full API URL, extract the path
    if token.startswith("http"):
        try:
            path = urlparse(token).path  # e.g. '/bot<token>'
            if path.lower().startswith('/bot'):
                token = path[4:]
                logger.debug("Extracted token from URL")
        except Exception as e:
            logger.error(f"Error parsing BOT_TOKEN URL: {e}")
            
    # Remove 'bot' prefix if present
    if token.lower().startswith('bot'):
        token = token[3:]
        logger.debug("Stripped 'bot' prefix from token")
        
    return token

def send_signal(message: str, max_retries=3, retry_delay=2):
    """
    Send a message to the Telegram channel with retry logic
    
    Args:
        message: The message to send
        max_retries: Maximum number of retry attempts
        retry_delay: Delay between retries in seconds
    
    Returns:
        bool: True if successful, False otherwise
    """
    if not message:
        logger.warning("Empty message provided to send_signal, skipping")
        return False
        
    bot_token = get_bot_token()
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {'chat_id': CHAT_ID, 'text': message}
    
    # Truncate message if too long
    if len(message) > 4000:
        logger.warning(f"Message too long ({len(message)} chars), truncating to 4000 chars")
        payload['text'] = message[:3997] + "..."
    
    logger.info(f"Sending admin signal: {message[:50]}..." if len(message) > 50 else message)
    
    for attempt in range(max_retries):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            
            if resp.ok:
                logger.info("Signal sent successfully")
                return True
                
            logger.error(f"Failed to send signal: HTTP {resp.status_code} - {resp.text}")
            
            # Don't retry on client errors (except 429 Too Many Requests)
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                return False
                
        except requests.exceptions.Timeout:
            logger.error(f"Timeout sending signal (attempt {attempt+1}/{max_retries})")
        except requests.exceptions.RequestException as e:
            logger.error(f"Error sending signal (attempt {attempt+1}/{max_retries}): {e}")
            
        # Don't sleep on the last attempt
        if attempt < max_retries - 1:
            time.sleep(retry_delay)
            
    return False
