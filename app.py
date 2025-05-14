import os
import stripe
import requests
from flask import Flask, jsonify, request, abort
from dotenv import load_dotenv
from notify_signals import send_signal
from flask_cors import CORS

# Load environment variables
load_dotenv()
app = Flask(__name__)
CORS(app, origins=["https://survivalsignals.trade"], supports_credentials=True)

# Stripe & Telegram configuration
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')
TG_BOT_TOKEN   = os.getenv('TG_BOT_TOKEN')
TG_CHAT_ID     = os.getenv('TG_CHAT_ID')
PRICE_ID       = os.getenv('STRIPE_PRICE_ID', 'price_1RMYrR2X75x3JSfv5Ad0YdRk')

# In-memory idempotency store (fade to DB in prod)
processed_events = set()
def already_processed(event_id):
    if event_id in processed_events:
        return True
    processed_events.add(event_id)
    return False

# Helper: Sanitize and extract raw bot token
def get_bot_token():
    token = TG_BOT_TOKEN or ''
    # If someone provided full URL, extract only the token
    if token.startswith('http'):
        try:
            from urllib.parse import urlparse
            parsed = urlparse(token)
            path = parsed.path  # expected '/bot<token>'
            if path.lower().startswith('/bot'):
                token = path[4:]
        except Exception:
            pass
    # Remove leading 'bot' prefix if present
    if token.lower().startswith('bot'):
        token = token[3:]
    return token

# Utility: Create a single-use Telegram invite link
def create_one_time_invite():
    token = get_bot_token()
    url = f"https://api.telegram.org/bot{token}/createChatInviteLink"
    payload = { 'chat_id': TG_CHAT_ID, 'member_limit': 1 }
    send_signal(f"📨 [create_one_time_invite] URL: {url}, Payload: {payload}")
    resp = requests.post(url, json=payload)
    send_signal(f"📤 [create_one_time_invite] Response: HTTP {resp.status_code}, Body: {resp.text}")
    # In case of API-level error, log JSON
    if resp.status_code != 200:
        send_signal(f"❌ [create_one_time_invite] HTTP Error {resp.status_code}: {resp.text}")
        resp.raise_for_status()
    data = resp.json()
    if not data.get('ok'):
        send_signal(f"❌ [create_one_time_invite] API Error: {data}")
        raise Exception(f"Telegram API error: {data.get('description')}")
    send_signal(f"✅ [create_one_time_invite] Success, link: {data['result']['invite_link']}")
    return data['result']['invite_link']

# Utility: Send a DM to a Telegram user
def send_dm(telegram_id, text):
    token = get_bot_token()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = { 'chat_id': telegram_id, 'text': text }
    send_signal(f"📨 [send_dm] URL: {url}, Payload: {payload}")
    resp = requests.post(url, json=payload)
    send_signal(f"📤 [send_dm] Response: HTTP {resp.status_code}, Body: {resp.text}")
    # Log on non-200 HTTP
    if resp.status_code != 200:
        send_signal(f"❌ [send_dm] HTTP Error {resp.status_code}: {resp.text}")
        resp.raise_for_status()
    data = resp.json()
    if not data.get('ok'):
        send_signal(f"❌ [send_dm] API Error: {data}")
        raise Exception(f"Telegram API error: {data.get('description')}")
    send_signal(f"✅ [send_dm] Message sent to {telegram_id}")

# Endpoint: Create Checkout Session
def send_dm(telegram_id, text):
    token = get_bot_token()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json={ 'chat_id': telegram_id, 'text': text })
    # Log on non-200 HTTP
    if resp.status_code != 200:
        send_signal(f"Failed to send DM: HTTP {resp.status_code} - {resp.text}")
        resp.raise_for_status()
    data = resp.json()
    if not data.get('ok'):
        send_signal(f"Telegram API error (sendMessage): {data}")
        raise Exception(f"Telegram API error: {data.get('description')}")

# Endpoint: Create Checkout Session
@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    data = request.json or {}
    telegram_id = data.get('telegram_user_id')
    if not telegram_id:
        return jsonify({'error': 'Missing telegram_user_id'}), 400

    session = stripe.checkout.Session.create(
        payment_method_types=['card'],
        line_items=[{ 'price': PRICE_ID, 'quantity': 1 }],
        mode='subscription',
        success_url='https://survivalsignals.trade/success',
        cancel_url='https://survivalsignals.trade/cancel',
        metadata={ 'telegram_user_id': str(telegram_id) }
    )
    return jsonify({ 'sessionId': session.id })

# Endpoint: Stripe Webhook Receiver
@app.route('/webhook/stripe', methods=['GET', 'POST', 'OPTIONS'])
@app.route('/webhook/stripe/', methods=['GET', 'POST', 'OPTIONS'])
def stripe_webhook():
    # Handle preflight and health checks
    if request.method in ('GET', 'OPTIONS'):
        return jsonify({'status': 'ok'}), 200

    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
    except Exception as e:
        send_signal(f"❌ Signature verification failed: {e}")
        abort(400)

    # Idempotency: skip duplicates
    if already_processed(event.id):
        return '', 200

    etype = event['type']
    send_signal(f"✨ Event received: {etype}")

    # 1) Initial Checkout completion
    if etype in ('checkout.session.completed', 'checkout.session.async_payment_succeeded'):
        sess = event['data']['object']
        tg = sess['metadata'].get('telegram_user_id')
        if tg:
            try:
                link = create_one_time_invite()
                send_dm(tg, f"🎉 Your one-time invite link: {link}")
            except Exception as e:
                send_signal(f"❌ Invite creation error: {e}")

    # 2) Recurring invoice paid
    elif etype == 'invoice.paid':
        inv = event['data']['object']
        tg = inv['metadata'].get('telegram_user_id')
        if tg:
            try:
                link = create_one_time_invite()
                send_dm(tg, f"🔄 Renewal invite link: {link}")
            except Exception as e:
                send_signal(f"❌ Renewal invite error: {e}")

    # 3) Payment failure
    elif etype == 'invoice.payment_failed':
        inv = event['data']['object']
        tg = inv['metadata'].get('telegram_user_id')
        if tg:
            send_dm(tg, "❗️ Your payment failed; please update your payment method.")

    # 4) Subscription status changes
    elif etype == 'customer.subscription.updated':
        sub = event['data']['object']
        status = sub.get('status')
        tg = sub['metadata'].get('telegram_user_id')
        if tg and status in ('canceled', 'unpaid'):
            send_dm(tg, "🔒 Your subscription has ended; alerts paused.")

    return '', 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
