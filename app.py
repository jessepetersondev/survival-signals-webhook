import os
import stripe
import requests
import logging
from flask import Flask, jsonify, request, abort
from dotenv import load_dotenv
from notify_signals import send_signal
from flask_cors import CORS

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load env
load_dotenv()
app = Flask(__name__)
CORS(app, origins=["https://survivalsignals.trade"], supports_credentials=True)

# Config
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
WEBHOOK_SECRET   = os.getenv('STRIPE_WEBHOOK_SECRET')
TG_BOT_TOKEN     = os.getenv('TG_BOT_TOKEN')
TG_CHAT_ID       = os.getenv('TG_CHAT_ID')
PRICE_ID         = os.getenv('STRIPE_PRICE_ID', 'price_1RMYrR2X75x3JSfv5Ad0YdRk')

# In-memory idempotency
processed_events = set()
def already_processed(event_id):
    if event_id in processed_events:
        return True
    processed_events.add(event_id)
    return False

# Helper to extract token
def get_bot_token():
    token = TG_BOT_TOKEN or ''
    if token.startswith('http'):
        try:
            from urllib.parse import urlparse
            path = urlparse(token).path
            if path.lower().startswith('/bot'):
                token = path[4:]
        except:
            pass
    if token.lower().startswith('bot'):
        token = token[3:]
    return token

# Create invite
def create_one_time_invite():
    token = get_bot_token()
    url = f"https://api.telegram.org/bot{token}/createChatInviteLink"
    payload = {'chat_id': TG_CHAT_ID, 'member_limit': 1}
    logger.info(f"[invite] POST {url} payload={payload}")
    resp = requests.post(url, json=payload)
    logger.info(f"[invite] HTTP {resp.status_code}: {resp.text}")
    if resp.status_code != 200:
        logger.error(f"Invite HTTP error {resp.status_code}")
        resp.raise_for_status()
    data = resp.json()
    if not data.get('ok'):
        logger.error(f"Invite API error: {data}")
        raise Exception(data.get('description'))
    return data['result']['invite_link']

# Send DM
def send_dm(telegram_id, text):
    token = get_bot_token()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {'chat_id': telegram_id, 'text': text}
    logger.info(f"[send_dm] POST {url} payload={payload}")
    resp = requests.post(url, json=payload)
    logger.info(f"[send_dm] HTTP {resp.status_code}: {resp.text}")
    if resp.status_code != 200:
        logger.error(f"DM HTTP error {resp.status_code}")
        resp.raise_for_status()
    data = resp.json()
    if not data.get('ok'):
        logger.error(f"DM API error: {data}")
        raise Exception(data.get('description'))

# Create Checkout Session
def create_session():
    data = request.json or {}
    tg = data.get('telegram_user_id')
    if not tg:
        return jsonify({'error':'Missing telegram_user_id'}),400
    session = stripe.checkout.Session.create(
        payment_method_types=['card'],
        line_items=[{'price':PRICE_ID,'quantity':1}],
        mode='subscription',
        success_url='https://survivalsignals.trade/success',
        cancel_url='https://survivalsignals.trade/cancel',
        metadata={'telegram_user_id':tg},
        subscription_data={'metadata':{'telegram_user_id':tg}}
    )
    logger.info(f"Created session {session.id} for TG {tg}")
    return jsonify({'sessionId':session.id})

@app.route('/create-checkout-session',methods=['POST'])
def route_create_session():
    return create_session()

# Webhook endpoint
@app.route('/webhook/stripe',methods=['GET','OPTIONS','POST'])
@app.route('/webhook/stripe/',methods=['GET','OPTIONS','POST'])
def stripe_webhook():
    if request.method in ('GET','OPTIONS'):
        return jsonify({'status':'ok'}),200
    payload = request.get_data(as_text=True)
    sig = request.headers.get('Stripe-Signature')
    try:
        event = stripe.Webhook.construct_event(payload,sig,WEBHOOK_SECRET)
    except Exception as e:
        logger.error(f"Signature failed: {e}")
        abort(400)
    if already_processed(event.id):
        return '',200
    et=event['type']
    logger.info(f"Event {et}")
    # Handle checkout
    if et in ('checkout.session.completed','checkout.session.async_payment_succeeded'):
        sess=event['data']['object']
        tg=sess['metadata'].get('telegram_user_id')
        if tg:
            try:
                link=create_one_time_invite()
                send_dm(tg,f"🎉 Invite: {link}")
            except Exception as e:
                logger.error(f"Invite error: {e}")
    # Handle invoice.paid
    elif et=='invoice.paid':
        inv=event['data']['object']
        tg=inv['metadata'].get('telegram_user_id')
        if not tg and inv.get('subscription'):
            # fetch subscription metadata
            sub=stripe.Subscription.retrieve(inv['subscription'])
            tg=sub.metadata.get('telegram_user_id')
        logger.info(f"invoice.paid TG: {tg}")
        if tg:
            try:
                link=create_one_time_invite()
                send_dm(tg,f"🔄 Renewal invite: {link}")
            except Exception as e:
                logger.error(f"Renewal error: {e}")
    # invoice.payment_failed
    elif et=='invoice.payment_failed':
        inv=event['data']['object']
        tg=inv['metadata'].get('telegram_user_id')
        if tg:
            send_dm(tg,"❗️ Payment failed, update method.")
    # subscription updated
    elif et=='customer.subscription.updated':
        sub=event['data']['object']
        status=sub.get('status')
        tg=sub['metadata'].get('telegram_user_id')
        if tg and status in('canceled','unpaid'):
            send_dm(tg,"🔒 Subscription ended.")
    return '',200

if __name__=='__main__':
    app.run(host='0.0.0.0',port=int(os.getenv('PORT',5000)))
