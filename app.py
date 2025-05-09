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
# Enable CORS for health-checks and webhooks
CORS(app, origins=["https://survivalsignals.trade"], supports_credentials=True)

# Stripe and Telegram configuration
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET') or os.getenv('STRIPE_WEBHOOK_SECRET')
TG_BOT_TOKEN    = os.getenv('TG_BOT_TOKEN')
TG_CHAT_ID      = os.getenv('TG_CHAT_ID')
PRICE_ID        = os.getenv('STRIPE_PRICE_ID', 'price_1RMYrR2X75x3JSfv5Ad0YdRk')

# Utility: Create a single-use Telegram invite link
def create_one_time_invite():
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/createChatInviteLink"
    payload = {'chat_id': TG_CHAT_ID, 'member_limit': 1}
    resp = requests.post(url, json=payload)
    resp.raise_for_status()
    return resp.json().get('result', {}).get('invite_link')

# Utility: Send a direct message to a user
def send_dm(telegram_id, text):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {'chat_id': telegram_id, 'text': text}
    resp = requests.post(url, json=payload)
    resp.raise_for_status()

# Endpoint: Create Checkout Session\ n@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    data = request.json or {}
    telegram_id = data.get('telegram_user_id')
    if not telegram_id:
        return jsonify({'error': 'Missing telegram_user_id'}), 400
    session = stripe.checkout.Session.create(
        payment_method_types=['card'],
        line_items=[{'price': PRICE_ID, 'quantity': 1}],
        mode='subscription',
        success_url='https://survivalsignals.trade/success',
        cancel_url='https://survivalsignals.trade/cancel',
        metadata={'telegram_user_id': str(telegram_id)}
    )
    return jsonify({'sessionId': session.id})

# Endpoint: Stripe Webhook Receiver (support GET, POST, OPTIONS)
@app.route('/webhook/stripe', methods=['OPTIONS', 'GET', 'POST'])
def stripe_webhook():
    # Handle CORS preflight
    if request.method == 'OPTIONS':
        return '', 200
    # GET for healthcheck
    if request.method == 'GET':
        return jsonify({'status': 'ok'}), 200
    # POST: process Stripe event
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    send_signal(f"🔔 Webhook received: {payload[:200]}...")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
    except Exception as e:
        send_signal(f"❌ Webhook verification failed: {e}")
        abort(400)
    send_signal(f"✅ Event type: {event['type']}")
    if event['type'] == 'invoice.paid':
        invoice = event['data']['object']
        telegram_id = invoice.get('metadata', {}).get('telegram_user_id')
        send_signal(f"🔍 Metadata TG ID: {telegram_id}")
        if telegram_id:
            customer = invoice.get('customer')
            send_signal(f"✅ Subscription for cus {customer} (TG {telegram_id})")
            try:
                invite_link = create_one_time_invite()
                send_signal(f"🔗 Link: {invite_link}")
                send_dm(telegram_id, f"🎉 Use this one-time link to join: {invite_link}")
                send_signal(f"✉️ DM sent to {telegram_id}")
            except Exception as e:
                send_signal(f"❌ Invite error for {telegram_id}: {e}")
    return '', 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
