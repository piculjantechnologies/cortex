import pytest
import stripe

from app.extensions import db
from app.models import User

DELETE = "/api/delete-account"


def user_exists(app, user_id):
    with app.app_context():
        return db.session.get(User, user_id) is not None


def test_delete_account_requires_login(client, mock_stripe):
    assert client.delete(DELETE).status_code == 401


def test_subscriber_is_unsubscribed_and_deleted(app, login_as, subscriber, mock_stripe):
    sub = mock_stripe.obj
    mock_stripe.subscription_list.return_value = mock_stripe.list_of(
        sub(stripe.Subscription, id="sub_active", status="active"),
        sub(stripe.Subscription, id="sub_old", status="canceled"),
        sub(stripe.Subscription, id="sub_trial", status="trialing"),
        sub(stripe.Subscription, id="sub_expired", status="incomplete_expired"),
        sub(stripe.Subscription, id="sub_late", status="past_due"),
    )
    client = login_as(subscriber)

    response = client.delete(DELETE)

    assert response.status_code == 200
    assert response.get_json() == {"message": "Account deleted successfully"}
    list_call = mock_stripe.subscription_list.call_args
    assert (list_call.kwargs["customer"], list_call.kwargs["status"]) == ("cus_test_subscriber", "all")
    canceled = [call.args[0] for call in mock_stripe.subscription_cancel.call_args_list]
    assert canceled == ["sub_active", "sub_trial", "sub_late"]
    assert mock_stripe.customer_delete.call_args.args == ("cus_test_subscriber",)
    assert not user_exists(app, subscriber.id)
    assert client.get("/api/check-auth").status_code == 401


def test_account_without_billing_is_deleted_without_stripe(app, logged_in_client, user, mock_stripe):
    assert logged_in_client.delete(DELETE).status_code == 200
    mock_stripe.subscription_list.assert_not_called()
    mock_stripe.customer_delete.assert_not_called()
    assert not user_exists(app, user.id)


@pytest.mark.parametrize("failing", ["subscription_list", "subscription_cancel", "customer_delete"])
def test_stripe_failure_keeps_the_account(app, login_as, subscriber, mock_stripe, failing, caplog):
    mock_stripe.subscription_list.return_value = mock_stripe.list_of(
        mock_stripe.obj(stripe.Subscription, id="sub_active", status="active"),
    )
    getattr(mock_stripe, failing).side_effect = stripe.APIConnectionError("Stripe is down")
    client = login_as(subscriber)

    response = client.delete(DELETE)

    assert response.status_code == 502
    assert "Stripe is down" not in response.get_data(as_text=True)
    assert "Stripe cleanup failed" in caplog.text
    assert user_exists(app, subscriber.id)
    assert client.get("/api/check-auth").status_code == 200  # still logged in, can retry


def test_customer_already_deleted_in_stripe_does_not_block_deletion(app, login_as, subscriber, mock_stripe):
    mock_stripe.subscription_list.side_effect = stripe.InvalidRequestError(
        "No such customer: 'cus_test_subscriber'", "customer", code="resource_missing"
    )

    assert login_as(subscriber).delete(DELETE).status_code == 200
    assert not user_exists(app, subscriber.id)


def test_subscription_gone_before_its_cancel_does_not_stop_the_cleanup(app, login_as, subscriber, mock_stripe):
    mock_stripe.subscription_list.return_value = mock_stripe.list_of(
        mock_stripe.obj(stripe.Subscription, id="sub_gone", status="active"),
        mock_stripe.obj(stripe.Subscription, id="sub_live", status="active"),
    )

    def cancel(subscription_id, **kwargs):
        if subscription_id == "sub_gone":
            raise stripe.InvalidRequestError("No such subscription: 'sub_gone'", "id", code="resource_missing")

    mock_stripe.subscription_cancel.side_effect = cancel

    assert login_as(subscriber).delete(DELETE).status_code == 200
    canceled = [call.args[0] for call in mock_stripe.subscription_cancel.call_args_list]
    assert canceled == ["sub_gone", "sub_live"]
    assert mock_stripe.customer_delete.call_args.args == ("cus_test_subscriber",)
    assert not user_exists(app, subscriber.id)


def test_customer_deleted_in_stripe_after_the_list_does_not_block_deletion(app, login_as, subscriber, mock_stripe):
    mock_stripe.customer_delete.side_effect = stripe.InvalidRequestError(
        "No such customer: 'cus_test_subscriber'", "id", code="resource_missing"
    )

    assert login_as(subscriber).delete(DELETE).status_code == 200
    assert not user_exists(app, subscriber.id)


@pytest.mark.parametrize("failing", ["subscription_cancel", "customer_delete"])
def test_other_invalid_requests_keep_the_account(app, login_as, subscriber, mock_stripe, failing):
    mock_stripe.subscription_list.return_value = mock_stripe.list_of(
        mock_stripe.obj(stripe.Subscription, id="sub_active", status="active"),
    )
    getattr(mock_stripe, failing).side_effect = stripe.InvalidRequestError(
        "Invalid API key", None, code="api_key_invalid"
    )

    assert login_as(subscriber).delete(DELETE).status_code == 502
    assert user_exists(app, subscriber.id)


@pytest.mark.parametrize(("method", "path"), [("get", "/api/get-user-info"), ("post", "/api/set-user-info")])
def test_leftover_profile_routes_are_gone(logged_in_client, method, path):
    assert getattr(logged_in_client, method)(path, json={}).status_code == 404
