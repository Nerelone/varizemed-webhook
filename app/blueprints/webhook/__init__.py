from flask import Blueprint

bp = Blueprint("webhook", __name__)

from . import routes  # noqa: E402,F401
