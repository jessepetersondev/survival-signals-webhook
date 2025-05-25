import sys
import json
from notify_signals import send_signal

# Freqtrade passes the trade event JSON on STDIN
trade_data = json.load(sys.stdin)

# Example trade_data fields: pair, side, price, amount, timestamp
pair      = trade_data.get('pair')
side      = trade_data.get('side').upper()
price     = trade_data.get('price')
timestamp = trade_data.get('open_date')

message = (
    f"🚨 SURVIVAL SIGNAL 🚨\n"
    f"Pair: {pair}\n"
    f"Side: {side}\n"
    f"Price: {price}\n"
    f"Time: {timestamp} UTC"
)

# Send it
send_signal(message)