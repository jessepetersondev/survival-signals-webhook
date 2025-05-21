import os
import stripe
import requests
import logging
import json
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
CORS(app, origins=[
    "https://survivalsignals.trade",
    "https://www.survivalsignals.trade"
], methods=["POST", "GET"], supports_credentials=True)

# Configuration
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
WEBHOOK_SECRET   = os.getenv('STRIPE_WEBHOOK_SECRET')
TG_BOT_TOKEN     = os.getenv('TG_BOT_TOKEN')
TG_CHAT_ID       = os.getenv('TG_CHAT_ID')
PRICE_ID         = os.getenv('STRIPE_PRICE_ID')

logger.debug(f"Config loaded: TG_CHAT_ID={TG_CHAT_ID}, PRICE_ID={PRICE_ID}")

# Helper function to safely log objects
def safe_log_object(obj, prefix="Object"):
    """Safely log an object's properties, handling potential serialization issues"""
    if obj is None:
        logger.debug(f"{prefix}: None")
        return
        
    try:
        # Try to convert to dict if it's a Stripe object
        if hasattr(obj, 'to_dict'):
            obj_dict = obj.to_dict()
        else:
            obj_dict = dict(obj)
            
        # Log the object as JSON
        logger.debug(f"{prefix}: {json.dumps(obj_dict, indent=2)}")
    except Exception as e:
        # If conversion fails, log available attributes
        logger.debug(f"{prefix} (conversion failed: {e})")
        try:
            attrs = dir(obj)
            values = {}
            for attr in attrs:
                if not attr.startswith('_') and not callable(getattr(obj, attr)):
                    try:
                        values[attr] = getattr(obj, attr)
                    except Exception:
                        values[attr] = "ERROR: Could not access attribute"
            logger.debug(f"{prefix} attributes: {values}")
        except Exception as e2:
            logger.debug(f"Could not log {prefix} attributes: {e2}")

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

# Remove user from Telegram group
def remove_from_telegram_group(telegram_id):
    logger.info(f"Removing user {telegram_id} from Telegram group {TG_CHAT_ID}")
    token = get_bot_token()
    url = f"https://api.telegram.org/bot{token}/banChatMember"
    
    payload = {
        'chat_id': TG_CHAT_ID,
        'user_id': telegram_id,
        'revoke_messages': False  # Don't delete their messages
    }
    
    logger.debug(f"Calling Telegram API: POST {url} payload={payload}")
    
    try:
        resp = requests.post(url, json=payload)
        logger.debug(f"Telegram banChatMember response: HTTP {resp.status_code} {resp.text}")
        
        if resp.status_code != 200:
            logger.error(f"Remove user HTTP error {resp.status_code}: {resp.text}")
            return False
            
        data = resp.json()
        if not data.get('ok'):
            logger.error(f"Telegram API banChatMember error: {data}")
            return False
            
        # Immediately unban to allow them to rejoin if they resubscribe
        unban_url = f"https://api.telegram.org/bot{token}/unbanChatMember"
        unban_payload = {
            'chat_id': TG_CHAT_ID,
            'user_id': telegram_id,
            'only_if_banned': True
        }
        
        unban_resp = requests.post(unban_url, json=unban_payload)
        logger.debug(f"Telegram unbanChatMember response: HTTP {unban_resp.status_code} {unban_resp.text}")
        
        logger.info(f"Successfully removed user {telegram_id} from group {TG_CHAT_ID}")
        return True
        
    except Exception as e:
        logger.error(f"Error removing user from Telegram group: {e}", exc_info=True)
        return False

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
    logger.info(f"subscription_details called with telegram_user_id: {tg_id}")
    
    if not tg_id:
        logger.warning("Missing telegram_user_id in request")
        return jsonify({'error': 'Missing telegram_user_id'}), 400

    try:
        # Find their subscription via metadata
        logger.info(f"Searching for subscription with metadata['telegram_user_id']: {tg_id}")
        res = stripe.Subscription.search(
            query=f"metadata['telegram_user_id']:'{tg_id}'",
            limit=1
        )
        
        logger.info(f"Search returned {len(res.data)} results")
        
        if not res.data:
            logger.info(f"No subscription found for telegram_user_id: {tg_id}")
            return jsonify({'subscribed': False}), 200

        sub = res.data[0]
        logger.info(f"Found subscription: {sub.id}")
        
        # Log the entire subscription object for debugging
        logger.info(f"SUBSCRIPTION DETAILS FOR {sub.id}:")
        safe_log_object(sub, f"Subscription {sub.id}")
        
        # Log available top-level attributes
        logger.info(f"Available top-level attributes for subscription {sub.id}:")
        for attr in dir(sub):
            if not attr.startswith('_') and not callable(getattr(sub, attr, None)):
                try:
                    value = getattr(sub, attr)
                    logger.info(f"  - {attr}: {value}")
                except Exception as e:
                    logger.info(f"  - {attr}: ERROR accessing ({e})")
        
        # Safely get subscription attributes with fallbacks
        try:
            current_period_end = getattr(sub, 'current_period_end', None)
            logger.info(f"current_period_end: {current_period_end}")
        except (AttributeError, KeyError) as e:
            logger.warning(f"current_period_end not found in subscription {sub.id}: {e}")
            current_period_end = None
            
        try:
            status = getattr(sub, 'status', 'unknown')
            logger.info(f"status: {status}")
        except (AttributeError, KeyError) as e:
            logger.warning(f"status not found in subscription {sub.id}: {e}")
            status = 'unknown'
        
        # Log items data if available
        if hasattr(sub, 'items') and hasattr(sub.items, 'data'):
            logger.info(f"Subscription has {len(sub.items.data)} items")
            for i, item in enumerate(sub.items.data):
                safe_log_object(item, f"Subscription item {i}")
                if hasattr(item, 'price'):
                    safe_log_object(item.price, f"Price for item {i}")
        else:
            logger.warning(f"No items data found in subscription {sub.id}")
            
        try:
            if hasattr(sub, 'items') and hasattr(sub.items, 'data') and len(sub.items.data) > 0:
                price = sub.items.data[0].price.unit_amount_decimal
                logger.info(f"price: {price}")
            else:
                logger.warning(f"No items data available to extract price")
                price = None
        except (AttributeError, KeyError, IndexError) as e:
            logger.warning(f"price not found in subscription {sub.id}: {e}")
            price = None
            
        try:
            if hasattr(sub, 'items') and hasattr(sub.items, 'data') and len(sub.items.data) > 0:
                currency = sub.items.data[0].price.currency
                logger.info(f"currency: {currency}")
            else:
                logger.warning(f"No items data available to extract currency")
                currency = 'usd'
        except (AttributeError, KeyError, IndexError) as e:
            logger.warning(f"currency not found in subscription {sub.id}: {e}")
            currency = 'usd'
            
        # Return subscription details with safe values
        response_data = {
            'subscribed': True,
            'status': status,
            'current_period_end': current_period_end,
            'price': price,
            'currency': currency,
            'subscription_id': sub.id
        }
        
        logger.info(f"Returning subscription details: {response_data}")
        return jsonify(response_data)
        
    except Exception as e:
        logger.error(f"Error fetching subscription details: {e}", exc_info=True)
        return jsonify({'error': f'Server error: {str(e)}'}), 500

# Create Stripe Portal Session
@app.route('/create-portal-session', methods=['POST'])
def create_portal_session():
    data = request.json or {}
    tg_id = data.get("telegram_user_id")
    logger.info(f"create_portal_session called with telegram_user_id: {tg_id}")
    
    if not tg_id:
        logger.warning("Missing telegram_user_id in request")
        return jsonify({"error": "Missing telegram_user_id"}), 400

    try:
        # Search for the subscription whose metadata.telegram_user_id matches
        logger.info(f"Searching for subscription with metadata['telegram_user_id']: {tg_id}")
        result = stripe.Subscription.search(
            query=f"metadata['telegram_user_id']:'{tg_id}'",
            limit=1
        )
        
        logger.info(f"Search returned {len(result.data)} results")
        
        if not result.data:
            logger.warning(f"No subscription found for telegram_user_id: {tg_id}")
            return jsonify({"error": "Subscription not found"}), 404

        subscription = result.data[0]
        logger.info(f"Found subscription: {subscription.id}")
        
        # Log the subscription object
        safe_log_object(subscription, f"Portal subscription {subscription.id}")
        
        # Get customer ID
        try:
            customer_id = subscription.customer
            logger.info(f"Customer ID: {customer_id}")
        except Exception as e:
            logger.error(f"Error getting customer ID: {e}")
            return jsonify({"error": "Could not determine customer ID"}), 500

        try:
            # Create a billing portal session for that customer
            # Add configuration_id if available in environment
            portal_config = os.getenv('STRIPE_PORTAL_CONFIG_ID')
            logger.info(f"Portal configuration ID from env: {portal_config}")
            
            portal_args = {
                'customer': customer_id,
                'return_url': "https://survivalsignals.trade/account"
            }
            
            # Only add configuration if it's set
            if portal_config:
                portal_args['configuration'] = portal_config
                
            logger.info(f"Creating portal session with args: {portal_args}")
            portal = stripe.billing_portal.Session.create(**portal_args)
            
            logger.info(f"Portal session created: {portal.id}, URL: {portal.url}")
            return jsonify({"url": portal.url})
            
        except stripe.error.InvalidRequestError as e:
            # Handle specific Stripe errors
            error_message = str(e)
            logger.error(f"Stripe portal InvalidRequestError: {error_message}")
            
            if "No configuration provided" in error_message:
                logger.error(f"Stripe portal configuration error: {e}")
                return jsonify({
                    "error": "Stripe customer portal is not configured. Please contact support.",
                    "details": "The site administrator needs to configure the Stripe Customer Portal in the Stripe Dashboard.",
                    "admin_action_required": True,
                    "stripe_error": error_message
                }), 503
            else:
                # Return detailed error for other invalid request errors
                return jsonify({
                    "error": "Payment service configuration error",
                    "details": error_message,
                    "admin_action_required": True
                }), 500
                
    except stripe.error.StripeError as e:
        error_message = str(e)
        logger.error(f"Stripe error creating portal session: {error_message}")
        return jsonify({
            "error": "Payment service error",
            "details": error_message
        }), 500
    except Exception as e:
        logger.error(f"Unexpected error creating portal session: {e}", exc_info=True)
        return jsonify({
            "error": "An unexpected error occurred. Please try again later.",
            "details": str(e)
        }), 500

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
            
            # Remove user from group on payment failure
            try:
                logger.info(f"Removing user {tg} from Telegram group due to payment failure")
                if remove_from_telegram_group(tg):
                    send_dm(tg, "🔒 You've been removed from the group due to payment failure. Please update your payment method to regain access.")
                    send_signal(f"👋 User {tg} removed from group due to payment failure")
            except Exception as e:
                logger.error(f"Error removing user from Telegram group: {e}")
                send_signal(f"❌ Error removing user {tg} from group: {e}")

    # Handle subscription updates
    elif etype == 'customer.subscription.updated':
        sub = event['data']['object']
        status = sub.get('status')
        tg = sub.get('metadata', {}).get('telegram_user_id')
        logger.info(f"customer.subscription.updated, status={status}, telegram_user_id={tg}")
        
        # Check for cancellation or unpaid status
        if tg and status in ('canceled', 'unpaid'):
            # Send notification to user
            send_dm(tg, "🔒 Your subscription has ended; alerts paused.")
            
            # Remove user from Telegram group
            try:
                logger.info(f"Removing user {tg} from Telegram group due to subscription {status}")
                if remove_from_telegram_group(tg):
                    send_dm(tg, "👋 You've been removed from the Signals group. Resubscribe anytime to regain access.")
                    send_signal(f"👋 User {tg} removed from group due to subscription {status}")
            except Exception as e:
                logger.error(f"Error removing user from Telegram group: {e}")
                send_signal(f"❌ Error removing user {tg} from group: {e}")
    
    # Handle subscription deletion
    elif etype == 'customer.subscription.deleted':
        sub = event['data']['object']
        tg = sub.get('metadata', {}).get('telegram_user_id')
        logger.info(f"customer.subscription.deleted, telegram_user_id={tg}")
        
        if tg:
            # Send notification to user
            send_dm(tg, "🔒 Your subscription has been deleted; service access revoked.")
            
            # Remove user from Telegram group
            try:
                logger.info(f"Removing user {tg} from Telegram group due to subscription deletion")
                if remove_from_telegram_group(tg):
                    send_dm(tg, "👋 You've been removed from the Signals group. Resubscribe anytime to regain access.")
                    send_signal(f"👋 User {tg} removed from group due to subscription deletion")
            except Exception as e:
                logger.error(f"Error removing user from Telegram group: {e}")
                send_signal(f"❌ Error removing user {tg} from group: {e}")

    return '', 200

if __name__ == '__main__':
    logger.info("Starting Flask app")
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
