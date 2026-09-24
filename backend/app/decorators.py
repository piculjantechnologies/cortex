"""Access-control decorators for the routes."""

from functools import wraps

from flask import jsonify
from flask_login import current_user, login_required

# Stripe subscription statuses that grant access to the data.
ACCESS_STATUSES = ("active", "trialing")


def has_data_access(user):
    """True for a paying or trialing subscriber, or an account with complimentary access (superuser)."""
    return bool(user.superuser) or user.subscription_status in ACCESS_STATUSES


def subscription_required(view):
    """login_required plus data access; the status is read from the user's row on every request.

    Anonymous requests get 401 (from login_required), logged-in users without access get 403.
    """

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not has_data_access(current_user):
            return jsonify(error="Forbidden", message="An active subscription is required."), 403
        return view(*args, **kwargs)

    return login_required(wrapper)
