import os
import stripe
import requests
import logging
from flask import Flask, jsonify, request, abort
from dotenv import load_dotenv
from notify_signals import send_signal
from flask_cors import CORS

# Configure detailed logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger('app')

# Load environment variables
load_dotenv()
app = Flask(__name__)
CORS(app, origins=["https://survivalsignals.trade"], supports_credentials=True)

# Configuration
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
WEBHOOK_SECRET   = os.getenv('STRIPE_WEBHOOK_SECRET')
TG_BOT_TOKEN     = os.getenv('TG_BOT_TOKEN')
TG_CHAT_ID       = os.getenv('TG_CHAT_ID')
PRICE_ID         = os.getenv('STRIPE_PRICE_ID', 'price_1RMYrR2X75x3JSfv5Ad0YdRk')

logger.debug(f"Config loaded: TG_CHAT_ID={TG_CHAT_ID}, PRICE_ID={PRICE_ID}")

# Idempotency store
processed_events = set()
def already_processed(event_id):
    logger.debug(f"Checking idempotency for event {event_id}")
    if event_id in processed_events:
        logger.info(f"Skipping duplicate event {event_id}")
        return True
    processed_events.add(event_id)
    logger.debug(f"Marked event {event_id} as processed")
    return False

# Extract raw bot token
def get_bot_token():
    token = TG_BOT_TOKEN or ''
    logger.debug(f"Raw TG_BOT_TOKEN: {token}")
    if token.startswith('http'):
        try:
            from urllib.parse import urlparse
            path = urlparse(token).path
            if path.lower().startswith('/bot'):
                token = path[4:]
                logger.debug(f"Extracted token from URL: {token}")
        except Exception as e:
            logger.error(f"Error parsing bot token URL: {e}")
    if token.lower().startswith('bot'):
        token = token[3:]
        logger.debug(f"Stripped 'bot' prefix, token now: {token}")
    return token

# Create a one-time invite link
def create_one_time_invite():
    logger.debug("Entering create_one_time_invite")
    token = get_bot_token()
    url = f"https://api.telegram.org/bot{token}/createChatInviteLink"
    payload = {'chat_id': TG_CHAT_ID, 'member_limit': 1}
    logger.debug(f"Calling Telegram API: POST {url} payload={payload}")
    resp = requests.post(url, json=payload)
    logger.debug(f"Telegram response: HTTP {resp.status_code} {resp.text}")
    if resp.status_code != 200:
        logger.error(f"Invite HTTP error {resp.status_code}: {resp.text}")
        resp.raise_for_status()
    data = resp.json()
    if not data.get('ok'):
        logger.error(f"Telegram API createChatInviteLink error: {data}")
        raise Exception(data.get('description'))
    link = data['result']['invite_link']
    logger.info(f"Generated invite link: {link}")
    return link

# Send a direct message via Telegram
def send_dm(telegram_id, text):
    logger.debug(f"Entering send_dm for TG {telegram_id}")
    token = get_bot_token()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {'chat_id': telegram_id, 'text': text}
    logger.debug(f"Calling Telegram API: POST {url} payload={payload}")
    resp = requests.post(url, json=payload)
    logger.debug(f"Telegram sendMessage response: HTTP {resp.status_code} {resp.text}")
    if resp.status_code != 200:
        logger.error(f"DM HTTP error {resp.status_code}: {resp.text}")
        resp.raise_for_status()
    data = resp.json()
    if not data.get('ok'):
        logger.error(f"Telegram API sendMessage error: {data}")
        raise Exception(data.get('description'))
    logger.info(f"DM successfully sent to {telegram_id}")

# Create Stripe Checkout Session
@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    logger.debug("create_checkout_session invoked")
    data = request.json or {}
    tg_id = data.get('telegram_user_id')
    logger.debug(f"Payload data: {data}")
    if not tg_id:
        logger.warning("Missing telegram_user_id in request")
        return jsonify({'error': 'Missing telegram_user_id'}), 400

    # Create the Stripe Checkout Session
    session = stripe.checkout.Session.create(
        payment_method_types=['card'],
        line_items=[{'price': PRICE_ID, 'quantity': 1}],
        mode='subscription',
        success_url='https://survivalsignals.trade/success',
        cancel_url='https://survivalsignals.trade/cancel',
        metadata={'telegram_user_id': tg_id},
        subscription_data={'metadata': {'telegram_user_id': tg_id}}
    )
    # Important log: show session ID and associated Telegram ID
    logger.info(f"Created session {session.id} for TG {tg_id}")

    return jsonify({'sessionId': session.id})

@app.route('/subscription-details', methods=['POST'])
def subscription_details():
    data = request.json or {}
    tg_id = data.get('telegram_user_id')
    if not tg_id:
        return jsonify({'error': 'Missing telegram_user_id'}), 400

    try:
        # Find their subscription via metadata
        res = stripe.Subscription.search(
            query=f"metadata['telegram_user_id']:'{tg_id}'",
            limit=1
        )
        
        if not res.data:
            return jsonify({'subscribed': False}), 200

        sub = res.data[0]
        
        # Safely get subscription attributes with fallbacks
        try:
            current_period_end = getattr(sub, 'current_period_end', None)
        except (AttributeError, KeyError):
            logger.warning(f"current_period_end not found in subscription {sub.id}")
            current_period_end = None
            
        try:
            status = getattr(sub, 'status', 'unknown')
        except (AttributeError, KeyError):
            logger.warning(f"status not found in subscription {sub.id}")
            status = 'unknown'
            
        try:
            price = sub.items.data[0].price.unit_amount_decimal
        except (AttributeError, KeyError, IndexError):
            logger.warning(f"price not found in subscription {sub.id}")
            price = None
            
        try:
            currency = sub.items.data[0].price.currency
        except (AttributeError, KeyError, IndexError):
            logger.warning(f"currency not found in subscription {sub.id}")
            currency = 'usd'
            
        # Return subscription details with safe values
        return jsonify({
            'subscribed': True,
            'status': status,
            'current_period_end': current_period_end,
            'price': price,
            'currency': currency,
            'subscription_id': sub.id
        })
        
    except Exception as e:
        logger.error(f"Error fetching subscription details: {e}")
        return jsonify({'error': f'Server error: {str(e)}'}), 500

# Create Stripe Portal Session
@app.route('/create-portal-session', methods=['POST'])
def create_portal_session():
    data = request.json or {}
    tg_id = data.get("telegram_user_id")
    if not tg_id:
        return jsonify({"error": "Missing telegram_user_id"}), 400

    try:
        # Search for the subscription whose metadata.telegram_user_id matches
        result = stripe.Subscription.search(
            query=f"metadata['telegram_user_id']:'{tg_id}'",
            limit=1
        )
        
        if not result.data:
            return jsonify({"error": "Subscription not found"}), 404

        subscription = result.data[0]
        customer_id = subscription.customer

        try:
            # Create a billing portal session for that customer
            # Add configuration_id if available in environment
            portal_config = os.getenv('STRIPE_PORTAL_CONFIG_ID')
            portal_args = {
                'customer': customer_id,
                'return_url': "https://survivalsignals.trade/account"
            }
            
            # Only add configuration if it's set
            if portal_config:
                portal_args['configuration'] = portal_config
                
            portal = stripe.billing_portal.Session.create(**portal_args)
            return jsonify({"url": portal.url})
            
        except stripe.error.InvalidRequestError as e:
            # Handle specific Stripe errors
            if "No configuration provided" in str(e):
                logger.error(f"Stripe portal configuration error: {e}")
                return jsonify({
                    "error": "Stripe customer portal is not configured. Please contact support.",
                    "details": "The site administrator needs to configure the Stripe Customer Portal in the Stripe Dashboard.",
                    "admin_action_required": True
                }), 503
            else:
                # Re-raise for general handling
                raise
                
    except stripe.error.StripeError as e:
        logger.error(f"Stripe error creating portal session: {e}")
        return jsonify({"error": f"Payment service error: {str(e)}"}), 500
    except Exception as e:
        logger.error(f"Unexpected error creating portal session: {e}")
        return jsonify({"error": "An unexpected error occurred. Please try again later."}), 500

# Stripe Webhook Endpoint
@app.route('/webhook/stripe', methods=['GET', 'OPTIONS', 'POST'])
@app.route('/webhook/stripe/', methods=['GET', 'OPTIONS', 'POST'])
def stripe_webhook():
    logger.debug(f"stripe_webhook invoked, method={request.method}")
    if request.method in ('GET', 'OPTIONS'):
        logger.debug("Health-check or CORS preflight request")
        return jsonify({'status': 'ok'}), 200

    payload = request.get_data(as_text=True)
    logger.debug(f"Raw payload: {payload}")
    sig_header = request.headers.get('Stripe-Signature')
    logger.debug(f"Stripe-Signature header: {sig_header}")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
        logger.info(f"Constructed Stripe event id={event.id} type={event['type']}")
    except Exception as e:
        logger.error(f"Webhook signature verification failed: {e}")
        abort(400)

    if already_processed(event.id):
        return '', 200

    etype = event['type']
    logger.info(f"Handling event: {etype}")
    send_signal(f"✨ Event: {etype}")

        # Handle checkout session completed
    if etype in ('checkout.session.completed', 'checkout.session.async_payment_succeeded'):
        sess = event['data']['object']
        tg = sess['metadata'].get('telegram_user_id')
        logger.info(f"checkout.session event, telegram_user_id={tg}")
        if tg:
            try:
                # Send initial invite
                link = create_one_time_invite()
                send_dm(tg, f"🎉 Your invite link: {link}")
                # Patch initial invoice metadata so invoice.paid carries TG ID
                sub_id = sess.get('subscription')
                if sub_id:
                    invoices = stripe.Invoice.list(subscription=sub_id, limit=1)
                    if invoices.data:
                        inv_id = invoices.data[0].id
                        stripe.Invoice.modify(inv_id, metadata={'telegram_user_id': tg})
                        logger.info(f"Patched invoice {inv_id} with telegram_user_id={tg}")
            except Exception as e:
                logger.error(f"Error creating or sending invite: {e}")

    # Handle invoice.paid
    elif etype == 'invoice.paid':
        inv = event['data']['object']
        logger.info(f"Invoice paid event: id={inv.get('id')}, billing_reason={inv.get('billing_reason')}")
        # 1) Try metadata on invoice directly
        tg = inv.get('metadata', {}).get('telegram_user_id')
        logger.debug(f"Primary metadata telegram_user_id on invoice: {tg}")
        # 2) Fallback: Customer metadata
        customer_id = inv.get('customer')
        if not tg and customer_id:
            logger.debug(f"Fetching Customer metadata for {customer_id}")
            try:
                cust = stripe.Customer.retrieve(customer_id)
                tg = cust.metadata.get('telegram_user_id')
                logger.info(f"Retrieved telegram_user_id from Customer metadata: {tg}")
            except Exception as e:
                logger.error(f"Failed to retrieve Customer: {e}")
        # 3) Fallback: subscription metadata
        subscription_id = inv.get('parent', {}).get('subscription_details', {}).get('subscription')
        if not tg and subscription_id:
            logger.debug(f"Fetching Subscription metadata for {subscription_id}")
            try:
                sub = stripe.Subscription.retrieve(subscription_id)
                tg = sub.metadata.get('telegram_user_id')
                logger.info(f"Retrieved telegram_user_id from Subscription metadata: {tg}")
            except Exception as e:
                logger.error(f"Failed to retrieve Subscription: {e}")
        logger.info(f"Final telegram_user_id determined: {tg}")
        if tg:
            try:
                link = create_one_time_invite()
                send_dm(tg, f"🔄 Renewal invite link: {link}")
            except Exception as e:
                logger.error(f"Error sending renewal invite: {e}")
                send_signal(f"❌ Renewal invite error: {e}")

    # Handle payment failures
    elif etype == 'invoice.payment_failed':
        inv = event['data']['object']
        tg = inv.get('metadata', {}).get('telegram_user_id')
        logger.info(f"invoice.payment_failed, telegram_user_id={tg}")
        if tg:
            send_dm(tg, "❗️ Your payment failed; please update your payment method.")

    # Handle subscription updates
    elif etype == 'customer.subscription.updated':
        sub = event['data']['object']
        status = sub.get('status')
        tg = sub.get('metadata', {}).get('telegram_user_id')
        logger.info(f"customer.subscription.updated, status={status}, telegram_user_id={tg}")
        if tg and status in ('canceled', 'unpaid'):
            send_dm(tg, "🔒 Your subscription has ended; alerts paused.")

    return '', 200

if __name__ == '__main__':
    logger.info("Starting Flask app")
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
