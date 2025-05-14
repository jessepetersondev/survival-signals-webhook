import os
import stripe
import requests
from flask import Flask, jsonify, request, abort
from dotenv import load_dotenv
from notify_signals import send_signal
from flask_cors import CORS

# Load environment variables\load_dotenv()
app = Flask(__name__)
# Allow CORS from your front-end
CORS(app, origins=["https://survivalsignals.trade"], supports_credentials=True)

stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')
TG_BOT_TOKEN    = os.getenv('TG_BOT_TOKEN')
TG_CHAT_ID      = os.getenv('TG_CHAT_ID')
PRICE_ID        = os.getenv('STRIPE_PRICE_ID', 'price_1RMYrR2X75x3JSfv5Ad0YdRk')

# In-memory idempotency store (persist in DB for prod)
processed = set()
def already_processed(event_id):
    if event_id in processed:
        return True
    processed.add(event_id)
    return False

# Create single-use invite
def create_one_time_invite():
    print(TG_BOT_TOKEN)
    resp = requests.post(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/createChatInviteLink",
        json={'chat_id': TG_CHAT_ID, 'member_limit': 1}
    )
    print("after created link")
    resp.raise_for_status()
    print(resp.json()['result']['invite_link'])
    return resp.json()['result']['invite_link']

# Direct message helper
def send_dm(tg_id, text):
    resp = requests.post(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
        json={'chat_id': tg_id, 'text': text}
    )
    resp.raise_for_status()

@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    data = request.json or {}
    tg_id = data.get('telegram_user_id')
    if not tg_id:
        return jsonify({'error': 'Missing telegram_user_id'}), 400
    session = stripe.checkout.Session.create(
        payment_method_types=['card'],
        line_items=[{'price': PRICE_ID, 'quantity': 1}],
        mode='subscription',
        success_url='https://survivalsignals.trade/success',
        cancel_url='https://survivalsignals.trade/cancel',
        metadata={'telegram_user_id': str(tg_id)}
    )
    return jsonify({'sessionId': session.id})

@app.route('/webhook/stripe', methods=['POST'])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig = request.headers.get('Stripe-Signature')
    try:
        event = stripe.Webhook.construct_event(payload, sig, WEBHOOK_SECRET)
    except Exception as e:
        send_signal(f"❌ Invalid signature: {e}")
        abort(400)

    if already_processed(event.id):
        return '', 200

    et = event['type']
    send_signal(f"✨ Event: {et}")

    # Handle Checkout success
    if et in ('checkout.session.completed', 'checkout.session.async_payment_succeeded'):
        sess = event['data']['object']
        tg = sess['metadata'].get('telegram_user_id')
        if tg:
            try:
                link = create_one_time_invite()
                print(f"Your one-time invite: {link}")
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

    # Handle payment failures
    elif et == 'invoice.payment_failed':
        inv = event['data']['object']
        tg = inv['metadata'].get('telegram_user_id')
        if tg:
            send_dm(tg, "❗️ Payment failed. Please update your card to retain access.")

    # Handle subscription status changes
    elif et == 'customer.subscription.updated':
        sub = event['data']['object']
        status = sub['status']
        tg = sub['metadata'].get('telegram_user_id')
        if tg and status in ('canceled','unpaid'):
            send_dm(tg, "🔒 Subscription ended. You have been removed from alerts.")

    return '', 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
