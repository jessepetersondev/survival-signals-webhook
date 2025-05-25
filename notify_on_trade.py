import sys
import json
import logging
from notify_signals import send_signal

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger('notify_on_trade')

try:
    # Freqtrade passes the trade event JSON on STDIN
    raw_data = sys.stdin.read()
    logger.info(f"Received trade data: {raw_data[:100]}..." if len(raw_data) > 100 else raw_data)
    
    try:
        trade_data = json.loads(raw_data)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON: {e}")
        sys.exit(1)
    
    # Validate required fields with defaults
    pair = trade_data.get('pair', 'UNKNOWN')
    side = trade_data.get('side', 'UNKNOWN').upper()
    price = trade_data.get('price', 'UNKNOWN')
    timestamp = trade_data.get('open_date', 'UNKNOWN')
    
    # Log the processed data
    logger.info(f"Processing trade: {pair} {side} at {price}")
    
    message = (
        f"🚨 SURVIVAL SIGNAL 🚨\n"
        f"Pair: {pair}\n"
        f"Side: {side}\n"
        f"Price: {price}\n"
        f"Time: {timestamp} UTC"
    )
    
    # Send it with error handling
    try:
        send_signal(message)
        logger.info("Signal sent successfully")
    except Exception as e:
        logger.error(f"Failed to send signal: {e}")
        sys.exit(2)
        
except Exception as e:
    logger.error(f"Unexpected error: {e}", exc_info=True)
    sys.exit(3)
