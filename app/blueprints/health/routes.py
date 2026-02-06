from datetime import datetime, timezone

from . import bp


@bp.get("/abacaxi")
def abacaxi():
    return {"status": "ok", "ts": str(datetime.now(timezone.utc))}


@bp.get("/healthz")
def healthz():
    return "ok", 200


@bp.get("/")
def root():
    return "Webh Flask ativo (v3 - async + message aggregation). Use /abacaxi, /healthz e /webhook (POST do Twilio)."
