"""
models.py -- domain row helpers.

User is the one entity with a helper class here: a thin wrapper over a
sqlite3.Row that also satisfies Flask-Login's UserMixin contract. Other
entities (Bill, BillMetadata, PayRun, etc.) are accessed as raw sqlite3 rows
directly in their blueprints rather than wrapped here.
"""

from flask_login import UserMixin

from db import q1

ROLES = ("ap_clerk", "controller", "cfo", "ceo")


class User(UserMixin):
    """Wraps a users-table row for Flask-Login.

    Flask-Login calls get_id() and reads is_active. We treat inactive rows
    (e.g. the v1 CEO placeholder) as unable to log in.
    """

    def __init__(self, row):
        self.id = row["id"]
        self.username = row["username"]
        self.name = row["name"]
        self.email = row["email"]
        self.role = row["role"]
        self.password_hash = row["password_hash"]
        self._is_active = bool(row["is_active"])
        self.must_change_password = bool(row["must_change_password"])

    # Flask-Login expects a string id.
    def get_id(self):
        return str(self.id)

    @property
    def is_active(self):
        return self._is_active

    def has_role(self, *roles) -> bool:
        return self.role in roles


def _wrap(row):
    return User(row) if row else None


def get_user_by_id(user_id):
    return _wrap(q1("SELECT * FROM users WHERE id = ?", (user_id,)))


def get_user_by_username(username):
    return _wrap(q1("SELECT * FROM users WHERE username = ?", (username,)))
