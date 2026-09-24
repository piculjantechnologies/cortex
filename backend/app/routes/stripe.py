import stripe
from flask import Blueprint, current_app, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from ..decorators import has_data_access
from ..extensions import db
from ..models import StripeEvent, User

bp = Blueprint("stripe", __name__)

# A customer with a subscription in one of these states must not start a second one.
OPEN_SUBSCRIPTION_STATUSES = ("active", "trialing", "past_due")
ALREADY_SUBSCRIBED = "Subscription already active"
# past_due is open but gives no data access: the card is fixed in the billing portal, not by a new checkout.
PAYMENT_FAILED = "Your last payment failed. Update your payment method in the billing portal."

SUBSCRIPTION_EVENTS = (
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
)
INVOICE_EVENTS = ("invoice.payment_succeeded", "invoice.payment_failed")


def _api_key():
    return current_app.config["STRIPE_API_KEY"]


def _open_subscription_status(customer_id):
    """The status of the customer's newest open subscription in Stripe, or None."""
    subscriptions = stripe.Subscription.list(customer=customer_id, status="all", api_key=_api_key())
    return next(
        (sub.status for sub in subscriptions.auto_paging_iter() if sub.status in OPEN_SUBSCRIPTION_STATUSES),
        None,
    )


def _checkout_refused(status):
    return jsonify({"error": PAYMENT_FAILED if status == "past_due" else ALREADY_SUBSCRIBED}), 409


@bp.route("/api/create-checkout-session", methods=["POST"])
@login_required
def create_checkout_session():
    if current_user.subscription_status in OPEN_SUBSCRIPTION_STATUSES:
        return _checkout_refused(current_user.subscription_status)

    config = current_app.config
    try:
        if not current_user.stripe_customer_id:
            customer = stripe.Customer.create(email=current_user.email, api_key=_api_key())
            current_user.stripe_customer_id = customer.id
            db.session.commit()
        else:
            open_status = _open_subscription_status(current_user.stripe_customer_id)
            if open_status is not None:
                # Stripe knows of a subscription the webhook has not recorded yet.
                return _checkout_refused(open_status)

        params = {
            "payment_method_types": ["card"],
            "mode": "subscription",
            "line_items": [{"price": config["STRIPE_PRICE_ID"], "quantity": 1}],
            "customer": current_user.stripe_customer_id,
            "success_url": f"{config['STRIPE_SUCCESS_URL']}?session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": config["STRIPE_CANCEL_URL"],
        }
        if current_user.subscription_status is None:
            # First subscription: a one-day free trial that needs no card up front.
            params["subscription_data"] = {
                "trial_period_days": 1,
                "trial_settings": {"end_behavior": {"missing_payment_method": "cancel"}},
            }
            params["payment_method_collection"] = "if_required"

        session = stripe.checkout.Session.create(**params, api_key=_api_key())
    except stripe.StripeError:
        current_app.logger.exception("Checkout session for user %s failed", current_user.id)
        return jsonify({"error": "Could not start checkout"}), 502

    return jsonify({"url": session.url})


@bp.route("/api/check-subscription-status", methods=["GET"])
@login_required
def check_subscription_status():
    return jsonify({
        # Same rule as the data endpoint, so complimentary accounts are not sent to checkout.
        "subscription_active": has_data_access(current_user),
        # Lets the SPA offer the billing portal to users without access, e.g. to fix a failed payment.
        "subscription_status": current_user.subscription_status,
        "has_billing_account": bool(current_user.stripe_customer_id),
    })


@bp.route("/api/create-portal-session", methods=["POST"])
@login_required
def create_portal_session():
    # The customer always comes from the logged-in user; the request body is ignored.
    customer_id = current_user.stripe_customer_id
    if not customer_id:
        return jsonify({"error": "No billing account found"}), 404

    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=current_app.config["STRIPE_SUCCESS_URL"],  # Redirect after leaving the portal
            api_key=_api_key(),
        )
    except stripe.StripeError:
        current_app.logger.exception("Billing portal session for user %s failed", current_user.id)
        return jsonify({"error": "Could not open the billing portal"}), 502

    return jsonify({"url": session.url})


@bp.route("/api/webhook", methods=["POST"])
def stripe_webhook():
    endpoint_secret = current_app.config.get("STRIPE_WEBHOOK_SECRET")
    if not endpoint_secret:
        current_app.logger.error("STRIPE_WEBHOOK_SECRET is not configured; rejecting the webhook")
        return "Webhook not configured", 500

    payload = request.get_data(as_text=True)
    sig_header = request.headers.get("Stripe-Signature")
    try:
        # Verify the webhook signature
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except stripe.SignatureVerificationError:
        return "Invalid signature", 400
    except (ValueError, AttributeError):  # not JSON, or JSON that is not an object
        return "Invalid payload", 400

    event_id, event_type = event.get("id"), event.get("type")
    data = event.get("data")
    obj = data.get("object") if isinstance(data, dict) else None
    if not event_id or not isinstance(obj, dict):
        return "Invalid payload", 400

    subscription_id = _subscription_id(event_type, obj)
    if subscription_id is None:
        current_app.logger.info("Ignoring Stripe event %s of type %s", event_id, event_type)
        return "Webhook received", 200

    if db.session.get(StripeEvent, event_id) is not None:
        return "Webhook received", 200  # a redelivery of an event that was already processed

    # Events arrive at least once and in any order, so the status is always read from the
    # subscription itself instead of being derived from the event type.
    try:
        subscription = stripe.Subscription.retrieve(subscription_id, api_key=_api_key())
        status = subscription.status
        if status not in OPEN_SUBSCRIPTION_STATUSES:
            # A (late) event about an ended subscription must not hide one the customer still
            # has open, e.g. after resubscribing.
            status = _open_subscription_status(subscription.customer) or status
    except stripe.StripeError:
        current_app.logger.exception("Could not retrieve subscription %s for event %s", subscription_id, event_id)
        return "Could not retrieve the subscription", 500  # Stripe retries

    try:
        db.session.add(StripeEvent(id=event_id, created=event.get("created"), type=event_type))
        user = User.query.filter_by(stripe_customer_id=subscription.customer).first()
        if user:
            user.subscription_status = status
        db.session.commit()
    except IntegrityError:
        db.session.rollback()  # a concurrent delivery of the same event got there first
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception("Could not store Stripe event %s", event_id)
        return "Database error", 500  # Stripe retries

    return "Webhook received", 200


def _subscription_id(event_type, obj):
    """The id of the subscription an event is about, or None for events that change no status."""
    if event_type in SUBSCRIPTION_EVENTS:
        return obj.get("id")
    if event_type in INVOICE_EVENTS:
        return obj.get("subscription")  # None for invoices outside a subscription
    if event_type == "checkout.session.completed" and obj.get("mode") == "subscription":
        return obj.get("subscription")
    return None
