import os
import stripe
import requests
from flask import Flask, jsonify, request, abort
from dotenv import load_dotenv
from notify_signals import send_signal

# Load environment variables
load_dotenv()

app = Flask(__name__)

# Stripe and Telegram configuration
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')
TG_BOT_TOKEN = os.getenv('TG_BOT_TOKEN')
TG_CHAT_ID  = os.getenv('TG_CHAT_ID')

# Endpoint: Create Checkout Session
@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    data = request.json or {}
    telegram_id = data.get('telegram_user_id')
    session = stripe.checkout.Session.create(
        payment_method_types=['card'],
        line_items=[{
            'price': 'price_1RMYrR2X75x3JSfv5Ad0YdRk',
            'quantity': 1
        }],
        mode='subscription',
        success_url='https://survivalsignals.trade/success',
        cancel_url='https://survivalsignals.trade/cancel',
        metadata={'telegram_user_id': telegram_id}
    )
    return jsonify({'sessionId': session.id})

# Endpoint: Stripe Webhook Receiver
@app.route('/webhook/stripe', methods=['POST'])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, WEBHOOK_SECRET
        )
    except (ValueError, stripe.error.SignatureVerificationError):
        abort(400)

    # Handle successful invoice payment
    if event['type'] == 'invoice.paid':
        invoice = event['data']['object']
        customer = invoice['customer']
        telegram_id = invoice['metadata'].get('telegram_user_id')
        if telegram_id:
            # Notify admin channel
            message = f"✅ New subscription: customer {customer} granted access."
            send_signal(message)
            # Invite subscriber to Telegram group
            invite_url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/inviteChatMember"
            requests.post(invite_url, json={
                'chat_id': TG_CHAT_ID,
                'user_id': telegram_id
            })
    return '', 200

if __name__ == '__main__':
    app.run(port=5000)