import json

import pytest
import stripe
from sqlalchemy.exc import IntegrityError, OperationalError

from app.config import TestConfig
from app.extensions import db
from app.models import StripeEvent, User

CHECKOUT = "/api/create-checkout-session"
PORTAL = "/api/create-portal-session"


def get_user(app, user_id):
    with app.app_context():
        user = db.session.get(User, user_id)
        db.session.expunge(user)
        return user


def stripe_calls(mock_stripe):
    return sum(getattr(mock_stripe, name).call_count for name in (
        "customer_create", "customer_delete", "checkout_session_create", "portal_session_create",
        "subscription_list", "subscription_retrieve", "subscription_cancel",
    ))


# --- billing portal ------------------------------------------------------------------------------


def test_portal_requires_login(client, mock_stripe):
    response = client.post(PORTAL, json={"customer_id": "cus_victim"})
    assert response.status_code == 401
    assert stripe_calls(mock_stripe) == 0


def test_portal_without_a_customer_is_404(logged_in_client, mock_stripe):
    assert logged_in_client.post(PORTAL).status_code == 404
    assert stripe_calls(mock_stripe) == 0


@pytest.mark.parametrize("body", [None, {"customer_id": "cus_victim"}])
def test_portal_uses_the_logged_in_customer_only(login_as, subscriber, mock_stripe, body):
    response = login_as(subscriber).post(PORTAL, json=body)

    assert response.status_code == 200
    assert response.get_json() == {"url": "https://billing.stripe.com/p/session/bps_test"}
    call = mock_stripe.portal_session_create.call_args
    assert call.kwargs["customer"] == "cus_test_subscriber"
    assert call.kwargs["return_url"] == TestConfig.STRIPE_SUCCESS_URL


def test_portal_stripe_error_is_a_generic_502(login_as, subscriber, mock_stripe):
    mock_stripe.portal_session_create.side_effect = stripe.InvalidRequestError("No such customer: cus_x", None)

    response = login_as(subscriber).post(PORTAL)

    assert response.status_code == 502
    assert "cus_x" not in response.get_data(as_text=True)


def test_get_customer_id_is_gone(login_as, subscriber):
    assert login_as(subscriber).get("/api/get-customer-id").status_code == 404


# --- checkout ------------------------------------------------------------------------------------


def test_checkout_requires_login(client, mock_stripe):
    assert client.post(CHECKOUT).status_code == 401
    assert stripe_calls(mock_stripe) == 0


def test_first_checkout_creates_and_stores_the_customer_and_offers_a_trial(app, logged_in_client, user, mock_stripe):
    response = logged_in_client.post(CHECKOUT)

    assert response.status_code == 200
    assert response.get_json() == {"url": "https://checkout.stripe.com/c/pay/cs_test"}
    assert mock_stripe.customer_create.call_args.kwargs["email"] == user.email
    assert get_user(app, user.id).stripe_customer_id == "cus_test_new"

    params = mock_stripe.checkout_session_create.call_args.kwargs
    assert params["mode"] == "subscription"
    assert params["customer"] == "cus_test_new"
    assert params["line_items"] == [{"price": TestConfig.STRIPE_PRICE_ID, "quantity": 1}]
    assert params["success_url"] == TestConfig.STRIPE_SUCCESS_URL + "?session_id={CHECKOUT_SESSION_ID}"
    assert params["cancel_url"] == TestConfig.STRIPE_CANCEL_URL
    assert params["subscription_data"]["trial_period_days"] == 1
    assert params["subscription_data"]["trial_settings"] == {"end_behavior": {"missing_payment_method": "cancel"}}
    assert params["payment_method_collection"] == "if_required"


def test_returning_customer_gets_no_trial(login_as, make_user, mock_stripe):
    former = make_user(stripe_customer_id="cus_former", subscription_status="canceled")

    response = login_as(former).post(CHECKOUT)

    assert response.status_code == 200
    mock_stripe.customer_create.assert_not_called()
    assert mock_stripe.subscription_list.call_args.kwargs["customer"] == "cus_former"
    params = mock_stripe.checkout_session_create.call_args.kwargs
    assert params["customer"] == "cus_former"
    assert "subscription_data" not in params
    assert "payment_method_collection" not in params


ALREADY_ACTIVE = {"error": "Subscription already active"}
PAYMENT_FAILED = {"error": "Your last payment failed. Update your payment method in the billing portal."}


@pytest.mark.parametrize(
    ("status", "answer"),
    [("active", ALREADY_ACTIVE), ("trialing", ALREADY_ACTIVE), ("past_due", PAYMENT_FAILED)],
)
def test_checkout_refused_while_a_subscription_is_open(login_as, make_user, mock_stripe, status, answer):
    member = make_user(stripe_customer_id="cus_member", subscription_status=status)

    response = login_as(member).post(CHECKOUT)

    assert response.status_code == 409
    assert response.get_json() == answer
    assert stripe_calls(mock_stripe) == 0


@pytest.mark.parametrize(("status", "answer"), [("active", ALREADY_ACTIVE), ("past_due", PAYMENT_FAILED)])
def test_checkout_refused_when_stripe_knows_an_open_subscription(login_as, make_user, mock_stripe, status, answer):
    # The local status is stale (a webhook has not arrived yet).
    member = make_user(stripe_customer_id="cus_member", subscription_status="canceled")
    mock_stripe.subscription_list.return_value = mock_stripe.list_of(
        mock_stripe.obj(stripe.Subscription, id="sub_new", status=status),
        mock_stripe.obj(stripe.Subscription, id="sub_old", status="canceled"),
    )

    response = login_as(member).post(CHECKOUT)

    assert response.status_code == 409
    assert response.get_json() == answer
    mock_stripe.checkout_session_create.assert_not_called()


def test_checkout_stripe_error_is_a_generic_502(logged_in_client, mock_stripe, caplog):
    mock_stripe.checkout_session_create.side_effect = stripe.APIConnectionError("internal detail SECRET-DETAIL-123")

    response = logged_in_client.post(CHECKOUT)

    assert response.status_code == 502
    assert response.get_json() == {"error": "Could not start checkout"}
    assert "SECRET-DETAIL-123" not in response.get_data(as_text=True)
    assert "Checkout session for user" in caplog.text


# --- subscription status -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("columns", "active"),
    [
        ({}, False),
        ({"subscription_status": "canceled", "stripe_customer_id": "cus_1"}, False),
        ({"subscription_status": "past_due", "stripe_customer_id": "cus_1"}, False),
        ({"subscription_status": "active", "stripe_customer_id": "cus_1"}, True),
        ({"subscription_status": "trialing", "stripe_customer_id": "cus_1"}, True),
        ({"superuser": True}, True),
    ],
)
def test_check_subscription_status(login_as, make_user, columns, active):
    response = login_as(make_user(**columns)).get("/api/check-subscription-status")
    assert response.get_json() == {
        "subscription_active": active,
        "subscription_status": columns.get("subscription_status"),
        "has_billing_account": "stripe_customer_id" in columns,
    }


def test_past_due_subscriber_is_pointed_to_the_billing_portal(login_as, make_user, mock_stripe):
    # No data access and no second checkout, but the portal (where the card is fixed) opens.
    late = login_as(make_user(stripe_customer_id="cus_late", subscription_status="past_due"))

    status = late.get("/api/check-subscription-status").get_json()
    assert (status["subscription_active"], status["has_billing_account"]) == (False, True)
    assert late.post(CHECKOUT).get_json() == PAYMENT_FAILED

    response = late.post(PORTAL)

    assert response.status_code == 200
    assert mock_stripe.portal_session_create.call_args.kwargs["customer"] == "cus_late"


# --- webhook -------------------------------------------------------------------------------------


def event_payload(event_type, obj, event_id="evt_1"):
    return json.dumps({
        "id": event_id,
        "object": "event",
        "type": event_type,
        "created": 1_700_000_000,
        "data": {"object": obj},
    })


def post_webhook(client, payload, signature):
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["Stripe-Signature"] = signature
    return client.post("/api/webhook", data=payload, headers=headers)


@pytest.fixture
def send_event(client, stripe_sig):
    """send_event(type, object, event_id='evt_1') -> the response to a correctly signed webhook."""

    def _send(event_type, obj, event_id="evt_1"):
        payload = event_payload(event_type, obj, event_id)
        return post_webhook(client, payload, stripe_sig(payload, TestConfig.STRIPE_WEBHOOK_SECRET))

    return _send


def live_subscription(mock_stripe, status, customer="cus_test_subscriber", sub_id="sub_test"):
    mock_stripe.subscription_retrieve.return_value = mock_stripe.obj(
        stripe.Subscription, id=sub_id, object="subscription", status=status, customer=customer,
    )


def status_of(app, user):
    return get_user(app, user.id).subscription_status


@pytest.mark.parametrize(
    "signature",
    [None, "garbage", "t=1,v1=0000", "stripe-sig-with-wrong-secret"],
)
def test_webhook_rejects_bad_signatures(client, subscriber, mock_stripe, stripe_sig, signature):
    payload = event_payload("customer.subscription.deleted", {"id": "sub_test", "customer": "cus_test_subscriber"})
    if signature == "stripe-sig-with-wrong-secret":
        signature = stripe_sig(payload, "another-secret")

    response = post_webhook(client, payload, signature)

    assert response.status_code == 400
    assert stripe_calls(mock_stripe) == 0


@pytest.mark.parametrize("payload", ["{not json", "[]", '"text"', "{}", '{"id": "evt_1", "type": "x"}'])
def test_webhook_rejects_malformed_payloads(client, mock_stripe, stripe_sig, payload):
    response = post_webhook(client, payload, stripe_sig(payload, TestConfig.STRIPE_WEBHOOK_SECRET))
    assert response.status_code == 400


def test_webhook_without_a_configured_secret_is_500(app, client, stripe_sig, caplog):
    app.config["STRIPE_WEBHOOK_SECRET"] = ""
    payload = event_payload("customer.subscription.deleted", {"id": "sub_test"})

    response = post_webhook(client, payload, stripe_sig(payload, "anything"))

    assert response.status_code == 500
    assert "STRIPE_WEBHOOK_SECRET is not configured" in caplog.text


@pytest.mark.parametrize(
    ("event_type", "obj"),
    [
        ("customer.subscription.created", {"id": "sub_test", "customer": "cus_test_subscriber", "status": "x"}),
        ("customer.subscription.updated", {"id": "sub_test", "customer": "cus_test_subscriber", "status": "x"}),
        ("customer.subscription.deleted", {"id": "sub_test", "customer": "cus_test_subscriber", "status": "x"}),
        ("invoice.payment_succeeded", {"id": "in_1", "customer": "cus_test_subscriber", "subscription": "sub_test"}),
        ("invoice.payment_failed", {"id": "in_1", "customer": "cus_test_subscriber", "subscription": "sub_test"}),
        ("checkout.session.completed",
         {"id": "cs_1", "customer": "cus_test_subscriber", "mode": "subscription", "subscription": "sub_test"}),
    ],
)
@pytest.mark.parametrize("live_status", ["trialing", "past_due", "canceled", "active"])
def test_webhook_stores_the_live_subscription_status(app, subscriber, mock_stripe, send_event, event_type, obj,
                                                     live_status):
    live_subscription(mock_stripe, live_status)

    response = send_event(event_type, obj)

    assert response.status_code == 200
    assert mock_stripe.subscription_retrieve.call_args.args == ("sub_test",)
    assert status_of(app, subscriber) == live_status
    with app.app_context():
        event = db.session.get(StripeEvent, "evt_1")
        assert (event.type, event.created) == (event_type, 1_700_000_000)


def test_redelivered_event_is_processed_once(app, subscriber, mock_stripe, send_event):
    live_subscription(mock_stripe, "past_due")
    obj = {"id": "in_1", "customer": "cus_test_subscriber", "subscription": "sub_test"}
    assert send_event("invoice.payment_failed", obj).status_code == 200
    with app.app_context():
        db.session.get(User, subscriber.id).subscription_status = "active"
        db.session.commit()

    assert send_event("invoice.payment_failed", obj).status_code == 200

    assert mock_stripe.subscription_retrieve.call_count == 1
    assert status_of(app, subscriber) == "active"
    with app.app_context():
        assert StripeEvent.query.count() == 1


def test_concurrent_delivery_of_the_same_event_is_not_an_error(app, subscriber, mock_stripe, send_event,
                                                               monkeypatch):
    live_subscription(mock_stripe, "canceled")

    def racing_commit():
        # The other delivery stored the event between our lookup and our commit.
        raise IntegrityError("INSERT INTO stripe_event", {}, Exception("UNIQUE constraint failed"))

    with monkeypatch.context() as patch:
        patch.setattr(db.session, "commit", racing_commit)
        response = send_event("customer.subscription.deleted", {"id": "sub_test", "customer": "cus_test_subscriber"})

    assert response.status_code == 200
    assert status_of(app, subscriber) == "active"  # this delivery changed nothing


@pytest.mark.parametrize("current_status", ["active", "trialing", "past_due"])
def test_late_event_about_an_ended_subscription_keeps_the_open_one(app, subscriber, mock_stripe, send_event,
                                                                  current_status):
    # The customer resubscribed (sub_B); an event about the old, canceled sub_A arrives late.
    live_subscription(mock_stripe, "canceled", sub_id="sub_A")
    mock_stripe.subscription_list.return_value = mock_stripe.list_of(
        mock_stripe.obj(stripe.Subscription, id="sub_B", status=current_status),
        mock_stripe.obj(stripe.Subscription, id="sub_A", status="canceled"),
    )

    response = send_event("customer.subscription.deleted", {"id": "sub_A", "customer": "cus_test_subscriber"})

    assert response.status_code == 200
    assert mock_stripe.subscription_list.call_args.kwargs["customer"] == "cus_test_subscriber"
    assert status_of(app, subscriber) == current_status


def test_late_event_leaves_an_active_subscriber_with_access(login_as, subscriber, mock_stripe, send_event, fake_mongo):
    live_subscription(mock_stripe, "canceled", sub_id="sub_A")
    mock_stripe.subscription_list.return_value = mock_stripe.list_of(
        mock_stripe.obj(stripe.Subscription, id="sub_B", status="active"),
    )

    send_event("invoice.payment_failed", {"id": "in_1", "customer": "cus_test_subscriber", "subscription": "sub_A"})

    assert login_as(subscriber).post("/api/get-labeled-data", json={}).status_code == 200


def test_event_about_an_open_subscription_needs_no_list(app, subscriber, mock_stripe, send_event):
    live_subscription(mock_stripe, "past_due")

    send_event("invoice.payment_failed", {"id": "in_1", "customer": "cus_test_subscriber", "subscription": "sub_test"})

    mock_stripe.subscription_list.assert_not_called()
    assert status_of(app, subscriber) == "past_due"


def test_late_payment_success_after_cancellation_keeps_canceled(app, subscriber, mock_stripe, send_event):
    live_subscription(mock_stripe, "canceled")
    send_event("customer.subscription.deleted", {"id": "sub_test", "customer": "cus_test_subscriber"}, "evt_2")

    # A delayed retry of an older invoice event arrives after the cancellation.
    send_event("invoice.payment_succeeded",
               {"id": "in_1", "customer": "cus_test_subscriber", "subscription": "sub_test"}, "evt_1")

    assert status_of(app, subscriber) == "canceled"


@pytest.mark.parametrize(
    ("event_type", "obj"),
    [
        ("invoice.payment_succeeded", {"id": "in_1", "customer": "cus_test_subscriber", "subscription": None}),
        ("invoice.payment_failed", {"id": "in_1", "customer": "cus_test_subscriber"}),
        ("checkout.session.completed", {"id": "cs_1", "customer": "cus_test_subscriber", "mode": "payment"}),
        ("customer.created", {"id": "cus_test_subscriber"}),
    ],
)
def test_events_without_a_subscription_are_ignored(app, subscriber, mock_stripe, send_event, event_type, obj):
    response = send_event(event_type, obj)

    assert response.status_code == 200
    mock_stripe.subscription_retrieve.assert_not_called()
    assert status_of(app, subscriber) == "active"


def test_event_for_an_unknown_customer_changes_nothing(app, subscriber, mock_stripe, send_event):
    live_subscription(mock_stripe, "canceled", customer="cus_unknown")

    response = send_event("customer.subscription.deleted", {"id": "sub_test", "customer": "cus_unknown"})

    assert response.status_code == 200
    assert status_of(app, subscriber) == "active"


def test_database_failure_is_500_so_stripe_retries(app, subscriber, mock_stripe, send_event, monkeypatch):
    live_subscription(mock_stripe, "canceled")

    def broken_commit():
        raise OperationalError("UPDATE user", {}, Exception("MySQL server has gone away"))

    with monkeypatch.context() as patch:
        patch.setattr(db.session, "commit", broken_commit)
        response = send_event("customer.subscription.deleted", {"id": "sub_test", "customer": "cus_test_subscriber"})

    assert response.status_code == 500
    assert status_of(app, subscriber) == "active"
    with app.app_context():
        assert StripeEvent.query.count() == 0  # the retry will be processed


@pytest.mark.parametrize("failing", ["subscription_retrieve", "subscription_list"])
def test_stripe_failure_while_retrieving_is_500(app, subscriber, mock_stripe, send_event, failing):
    live_subscription(mock_stripe, "canceled")
    getattr(mock_stripe, failing).side_effect = stripe.APIConnectionError("timeout")

    response = send_event("customer.subscription.deleted", {"id": "sub_test", "customer": "cus_test_subscriber"})

    assert response.status_code == 500
    assert status_of(app, subscriber) == "active"
    with app.app_context():
        assert StripeEvent.query.count() == 0


def test_cancellation_revokes_data_access(login_as, subscriber, mock_stripe, send_event, fake_mongo):
    client = login_as(subscriber)
    assert client.post("/api/get-labeled-data", json={}).status_code == 200

    live_subscription(mock_stripe, "canceled")
    send_event("customer.subscription.deleted", {"id": "sub_test", "customer": "cus_test_subscriber"})

    response = client.post("/api/get-labeled-data", json={})
    assert response.status_code == 403
    assert client.get("/api/check-subscription-status").get_json()["subscription_active"] is False
