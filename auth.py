"""
auth.py -- authentication for PayablesTool.

Real login via Flask-Login (a departure from CloseTool, whose login is a
no-op). The /login form is CSRF-protected via Flask-WTF. A role_required
decorator is provided for later phases; Phase 0 only needs login_required.

init_auth(app) wires the LoginManager and registers the blueprint, keeping
app.py free of circular imports.
"""

from functools import wraps

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_wtf import FlaskForm
from werkzeug.security import check_password_hash
from wtforms import PasswordField, StringField
from wtforms.validators import DataRequired

from models import get_user_by_id, get_user_by_username

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Please log in to continue."

bp = Blueprint("auth", __name__)


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired()])


@login_manager.user_loader
def load_user(user_id):
    return get_user_by_id(user_id)


def init_auth(app) -> None:
    login_manager.init_app(app)
    app.register_blueprint(bp)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    form = LoginForm()
    if form.validate_on_submit():
        user = get_user_by_username(form.username.data.strip())
        # Validate credentials. Inactive accounts (e.g. the v1 CEO) and
        # password-less seeds cannot log in. Use a single generic message
        # so we don't reveal which half failed.
        if (
            user is not None
            and user.is_active
            and user.password_hash
            and check_password_hash(user.password_hash, form.password.data)
        ):
            login_user(user)
            flash(f"Signed in as {user.name} ({user.role}).", "ok")
            next_url = request.args.get("next")
            return redirect(next_url or url_for("index"))
        flash("Invalid username or password.", "error")

    return render_template("login.html", form=form)


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Signed out.", "ok")
    return redirect(url_for("auth.login"))


def role_required(*roles):
    """Gate a route to one or more roles. Phase 0 doesn't use it yet, but the
    approval workflow (Phase 3+) will."""
    def decorator(f):
        @wraps(f)
        @login_required
        def wrapped(*args, **kwargs):
            if not current_user.has_role(*roles):
                abort(403)
            return f(*args, **kwargs)
        return wrapped
    return decorator
