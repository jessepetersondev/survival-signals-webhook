### 1) `app.py` (webhook/service)

```python
import os
import stripe
import requests
from flask import Flask, jsonify, request, abort
from dotenv import load_dotenv
from notify_signals import send_signal

# Load environment
load_dotenv()
app = Flask(__name__)

# Stripe & Telegram config
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')
TG_BOT_TOKEN   = os.getenv('TG_BOT_TOKEN')
TG_CHAT_ID     = os.getenv('TG_CHAT_ID')
PRICE_ID       = os.getenv('STRIPE_PRICE_ID')

# In-memory idempotency
processed = set()
def already_processed(eid):
    if eid in processed:
        return True
    processed.add(eid)
    return False

# Generate single-use invite link
def create_one_time_invite():
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/createChatInviteLink"
    resp = requests.post(url, json={'chat_id': TG_CHAT_ID, 'member_limit': 1})
    resp.raise_for_status()
    return resp.json()['result']['invite_link']

# Send a DM to a user
def send_dm(tg_id, text):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={'chat_id': tg_id, 'text': text})
    resp.raise_for_status()

@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    data = request.json or {}
    tg = data.get('telegram_user_id')
    if not tg:
        return jsonify({'error':'Missing telegram_user_id'}), 400
    session = stripe.checkout.Session.create(
        payment_method_types=['card'],
        line_items=[{'price': PRICE_ID, 'quantity': 1}],
        mode='subscription',
        success_url='https://survivalsignals.trade/success',
        cancel_url='https://survivalsignals.trade/cancel',
        metadata={'telegram_user_id': str(tg)}
    )
    return jsonify({'sessionId': session.id})

# Accept both /webhook/stripe and /webhook/stripe/ for POST events
for route in ['/webhook/stripe', '/webhook/stripe/']:
    app.add_url_rule(
        route,
        'stripe_webhook',
        endpoint='stripe_webhook',
        view_func=lambda: None,
        methods=['GET','POST','OPTIONS','HEAD']
    )

@app.route('/webhook/stripe', methods=['GET','POST','OPTIONS','HEAD'])
@app.route('/webhook/stripe/', methods=['GET','POST','OPTIONS','HEAD'])
def stripe_webhook():
    # Health and preflight
    if request.method in ('GET','OPTIONS','HEAD'):
        return jsonify({'status':'ok'}), 200

    payload = request.get_data(as_text=True)
    sig = request.headers.get('Stripe-Signature')
    try:
        event = stripe.Webhook.construct_event(payload, sig, WEBHOOK_SECRET)
    except Exception as e:
        send_signal(f"❌ Signature verification failed: {e}")
        abort(400)

    if already_processed(event.id):
        return '', 200

    et = event['type']
    send_signal(f"✨ Event: {et}")

    # Handle Checkout success
    if et in ('checkout.session.completed','checkout.session.async_payment_succeeded'):
        sess = event['data']['object']
        tg = sess['metadata'].get('telegram_user_id')
        if tg:
            try:
                link = create_one_time_invite()
                send_dm(tg, f"🎉 Your one-time invite: {link}")
            except Exception as e:
                send_signal(f"❌ Invite error: {e}")

    # Handle recurring invoice paid
    elif et == 'invoice.paid':
        inv = event['data']['object']
        tg = inv['metadata'].get('telegram_user_id')
        if tg:
            try:
                link = create_one_time_invite()
                send_dm(tg, f"🔄 Renewal invite: {link}")
            except Exception as e:
                send_signal(f"❌ Renewal invite error: {e}")

    # Handle payment failure
    elif et == 'invoice.payment_failed':
        inv = event['data']['object']
        tg = inv['metadata'].get('telegram_user_id')
        if tg:
            send_dm(tg, "❗️ Payment failed. Please update your payment method.")

    # Subscription updates (cancels)
    elif et == 'customer.subscription.updated':
        sub = event['data']['object']
        status = sub.get('status')
        tg = sub['metadata'].get('telegram_user_id')
        if tg and status in ('canceled','unpaid'):
            send_dm(tg, "🔒 Your subscription ended, you have been removed.")

    return '', 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT',5000)))
