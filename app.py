import os
import stripe
import requests
import logging
from flask import Flask, jsonify, request, abort
from dotenv import load_dotenv
from notify_signals import send_signal
from flask_cors import CORS

# Initialize logging to stdout for Railway
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
app = Flask(__name__)
CORS(app, origins=["https://survivalsignals.trade"], supports_credentials=True)

# Configuration
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')
TG_BOT_TOKEN   = os.getenv('TG_BOT_TOKEN')
TG_CHAT_ID     = os.getenv('TG_CHAT_ID')
PRICE_ID       = os.getenv('STRIPE_PRICE_ID', 'price_1RMYrR2X75x3JSfv5Ad0YdRk')

# In-memory idempotency store (persist in DB for production)
processed_events = set()
def already_processed(event_id):
    if event_id in processed_events:
        return True
    processed_events.add(event_id)
    return False

# Helper: Extract raw bot token from env
def get_bot_token():
    token = TG_BOT_TOKEN or ''
    if token.startswith('http'):
        try:
            from urllib.parse import urlparse
            path = urlparse(token).path
            if path.lower().startswith('/bot'):
                token = path[4:]
        except Exception:
            pass
    if token.lower().startswith('bot'):
        token = token[3:]
    return token

# Create a single-use Telegram invite link
def create_one_time_invite():
    token = get_bot_token()
    url = f"https://api.telegram.org/bot{token}/createChatInviteLink"
    payload = {'chat_id': TG_CHAT_ID, 'member_limit': 1}
    logger.info(f"[create_one_time_invite] URL: {url}, Payload: {payload}")
    resp = requests.post(url, json=payload)
    logger.info(f"[create_one_time_invite] HTTP {resp.status_code}: {resp.text}")
    if resp.status_code != 200:
        logger.error(f"Invite link HTTP error {resp.status_code}: {resp.text}")
        resp.raise_for_status()
    data = resp.json()
    if not data.get('ok'):
        logger.error(f"Telegram API error: {data}")
        raise Exception(f"Telegram API error: {data.get('description')}")
    link = data['result']['invite_link']
    logger.info(f"[create_one_time_invite] Success, link: {link}")
    return link

# Send a direct message to a Telegram user
def send_dm(telegram_id, text):
    token = get_bot_token()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {'chat_id': telegram_id, 'text': text}
    logger.info(f"[send_dm] URL: {url}, Payload: {payload}")
    resp = requests.post(url, json=payload)
    logger.info(f"[send_dm] HTTP {resp.status_code}: {resp.text}")
    if resp.status_code != 200:
        logger.error(f"send_dm HTTP error {resp.status_code}: {resp.text}")
        resp.raise_for_status()
    data = resp.json()
    if not data.get('ok'):
        logger.error(f"Telegram API error on send_dm: {data}")
        raise Exception(f"Telegram API error: {data.get('description')}")
    logger.info(f"[send_dm] Message successfully sent to {telegram_id}")

# Endpoint: Create a Stripe Checkout Session
@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    data = request.json or {}
    telegram_id = data.get('telegram_user_id')
    if not telegram_id:
        logger.warning("Missing telegram_user_id in request")
        return jsonify({'error': 'Missing telegram_user_id'}), 400

    session = stripe.checkout.Session.create(
        payment_method_types=['card'],
        line_items=[{'price': PRICE_ID, 'quantity': 1}],
        mode='subscription',
        success_url='https://survivalsignals.trade/success',
        cancel_url='https://survivalsignals.trade/cancel',
        metadata={'telegram_user_id': str(telegram_id)}
    )
    logger.info(f"Created Checkout Session {session.id} for TG: {telegram_id}")
    return jsonify({'sessionId': session.id})

# Endpoint: Stripe Webhook Receiver
@app.route('/webhook/stripe', methods=['GET', 'POST', 'OPTIONS'])
@app.route('/webhook/stripe/', methods=['GET', 'POST', 'OPTIONS'])
def stripe_webhook():
    # Health-check and preflight
    if request.method in ('GET', 'OPTIONS'):
        return jsonify({'status': 'ok'}), 200

    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
    except Exception as e:
        logger.error(f"Signature verification failed: {e}")
        send_signal(f"❌ Signature verification failed: {e}")
        abort(400)

    if already_processed(event.id):
        logger.info(f"Skipping duplicate event {event.id}")
        return '', 200

    etype = event['type']
    logger.info(f"Handling event: {etype}")
    send_signal(f"✨ Event: {etype}")

    # Handle checkout.session.completed
    if etype in ('checkout.session.completed', 'checkout.session.async_payment_succeeded'):
        sess = event['data']['object']
        tg = sess['metadata'].get('telegram_user_id')
        logger.info(f"checkout completed for TG: {tg}")
        if tg:
            try:
                link = create_one_time_invite()
                send_dm(tg, f"🎉 Your one-time invite link: {link}")
            except Exception as e:
                logger.error(f"Invite creation error: {e}")
                send_signal(f"❌ Invite creation error: {e}")

    # Handle recurring invoice.paid
    elif etype == 'invoice.paid':
        inv = event['data']['object']
        tg = inv['metadata'].get('telegram_user_id')
        logger.info(f"invoice.paid for TG: {tg}")
        if tg:
            try:
                link = create_one_time_invite()
                send_dm(tg, f"🔄 Renewal invite link: {link}")
            except Exception as e:
                logger.error(f"Renewal invite error: {e}")
                send_signal(f"❌ Renewal invite error: {e}")

    # Handle payment failures
    elif etype == 'invoice.payment_failed':
        inv = event['data']['object']
        tg = inv['metadata'].get('telegram_user_id')
        logger.info(f"invoice.payment_failed for TG: {tg}")
        if tg:
            send_dm(tg, "❗️ Your payment failed; please update your payment method.")

    # Handle subscription updates
    elif etype == 'customer.subscription.updated':
        sub = event['data']['object']
        status = sub.get('status')
        tg = sub['metadata'].get('telegram_user_id')
        logger.info(f"subscription.updated status={status} for TG: {tg}")
        if tg and status in ('canceled', 'unpaid'):
            send_dm(tg, "🔒 Your subscription has ended; alerts paused.")

    return '', 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
