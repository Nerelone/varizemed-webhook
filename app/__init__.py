from flask import Flask

from .config import get_config
from .extensions import init_extensions
from .core.logging import setup_logging
from .blueprints.health import bp as health_bp
from .blueprints.webhook import bp as webhook_bp


def create_app():
    app = Flask(__name__)
    app.config.from_object(get_config())
    app.config["JSON_AS_ASCII"] = False

    setup_logging(app)
    init_extensions(app)

    app.register_blueprint(health_bp)
    app.register_blueprint(webhook_bp)

    return app
