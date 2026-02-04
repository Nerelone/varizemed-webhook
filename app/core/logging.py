import json
import logging


def setup_logging(app):
    level = app.config.get("LOG_LEVEL", "INFO")
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s: %(message)s")


def log_event(action, **kw):
    try:
        payload = {"component": "webh", "action": action}
        payload.update({k: v for k, v in kw.items() if v is not None})
        logging.getLogger("webh").info(json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass
