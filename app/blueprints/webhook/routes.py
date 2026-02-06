from flask import current_app, request

from app.extensions import get_df_client, get_fs, get_http_session
from app.repositories.firestore_repo import FirestoreRepository
from app.services.webhook_service import (
    get_aggregation_debug_info,
    handle_webhook,
    twiml_empty,
)

from . import bp


@bp.post("/twiml-test")
def twiml_test():
    return twiml_empty(status=200)


@bp.post("/webhook")
def webhook():
    settings = current_app.config
    repo = FirestoreRepository(
        get_fs(),
        settings.get("FS_CONV_COLL"),
        settings.get("FS_MSG_SUBCOLL"),
    )
    return handle_webhook(
        request,
        settings=settings,
        repo=repo,
        cx_client=get_df_client(),
        http_session=get_http_session(),
    )


@bp.get("/debug/buffers")
def debug_buffers():
    return get_aggregation_debug_info(current_app.config)
