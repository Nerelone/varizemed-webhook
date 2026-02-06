import logging
from collections.abc import Mapping
import random
import time

from google.api_core import exceptions as gexc
from google.cloud import dialogflowcx_v3 as dfcx
from google.protobuf.struct_pb2 import Struct, Value
from google.protobuf.json_format import MessageToDict

_TRANSIENT_DF_ERRORS = (
    gexc.InternalServerError,
    gexc.ServiceUnavailable,
    gexc.DeadlineExceeded,
    gexc.Aborted,
    gexc.ResourceExhausted,
)


def struct_to_dict(obj):
    if obj is None:
        return {}

    if hasattr(obj, "_pb"):
        obj = obj._pb

    if isinstance(obj, Struct):
        try:
            return MessageToDict(obj, preserving_proto_field_name=True)
        except Exception as exc:
            logging.warning(f"MessageToDict failed for Struct: {exc}")

    if isinstance(obj, Value):
        kind = obj.WhichOneof("kind")
        if kind == "null_value":
            return None
        if kind == "number_value":
            return obj.number_value
        if kind == "string_value":
            return obj.string_value
        if kind == "bool_value":
            return obj.bool_value
        if kind == "struct_value":
            return struct_to_dict(obj.struct_value)
        if kind == "list_value":
            return [struct_to_dict(v) for v in obj.list_value.values]
        return None

    if isinstance(obj, Mapping):
        def cv(value):
            if hasattr(value, "_pb"):
                value = value._pb
            if isinstance(value, (Struct, Value)):
                return struct_to_dict(value)
            if isinstance(value, Mapping):
                return struct_to_dict(value)
            return value

        return {k: cv(v) for k, v in obj.items()}

    logging.warning(f"struct_to_dict received unexpected type: {type(obj).__name__}")
    return {}


def cx_all_params_dict(resp) -> dict:
    out = {}
    try:
        qr = getattr(resp, "query_result", None)
        if qr and getattr(qr, "parameters", None):
            out.update(struct_to_dict(qr.parameters) or {})
    except Exception:
        pass

    try:
        qr = getattr(resp, "query_result", None)
        si = getattr(qr, "session_info", None) if qr else None
        if si and getattr(si, "parameters", None):
            out.update(struct_to_dict(si.parameters) or {})
    except Exception:
        pass

    try:
        si2 = getattr(resp, "session_info", None)
        if si2 and getattr(si2, "parameters", None):
            out.update(struct_to_dict(si2.parameters) or {})
    except Exception:
        pass

    return out


def _cx_session_path(settings, session_id: str) -> str:
    return (
        f"projects/{settings.get('DF_PROJECT')}/locations/{settings.get('DF_LOCATION')}"
        f"/agents/{settings.get('DF_AGENT_ID')}/sessions/{session_id}"
    )


def detect_intent_text(
    df_client,
    settings,
    session_id,
    text,
    user_id=None,
    session_params=None,
    *,
    timeout_s: float = 15.0,
    attempts: int = 3,
):
    session = _cx_session_path(settings, session_id)

    query_params = dfcx.QueryParameters()
    has_params = False

    if user_id or session_params:
        params_struct = Struct()

        if user_id:
            params_struct.fields["user_id"].string_value = user_id
            has_params = True

        if session_params:
            for key, value in session_params.items():
                if value is None:
                    params_struct.fields[key].null_value = 0
                elif isinstance(value, bool):
                    params_struct.fields[key].bool_value = value
                else:
                    params_struct.fields[key].string_value = str(value)
            has_params = True

        if has_params:
            query_params.parameters = params_struct
            logging.info(f"CX QueryParams: user_id={user_id}, extras={session_params}")

    req = dfcx.DetectIntentRequest(
        session=session,
        query_input=dfcx.QueryInput(
            text=dfcx.TextInput(text=text),
            language_code=settings.get("LANG_CODE", "pt-br"),
        ),
        query_params=query_params if has_params else None,
    )

    attempts = max(1, int(attempts or 1))
    last_exc = None
    for i in range(attempts):
        try:
            resp = df_client.detect_intent(request=req, timeout=timeout_s)
            texts = []
            for msg in resp.query_result.response_messages:
                if msg.text and msg.text.text:
                    for piece in msg.text.text:
                        if piece:
                            texts.append(piece)
            return texts, resp
        except _TRANSIENT_DF_ERRORS as exc:
            last_exc = exc
            if i + 1 >= attempts:
                break
            sleep_s = (0.4 * (2 ** i)) + random.uniform(0, 0.25)
            logging.warning(
                "CX detect_intent transitorio (%s) tentativa %d/%d timeout=%.1fs sleep=%.2fs session_id=%s",
                type(exc).__name__,
                i + 1,
                attempts,
                timeout_s,
                sleep_s,
                session_id,
                exc_info=True,
            )
            time.sleep(sleep_s)

    if last_exc:
        raise last_exc
    raise RuntimeError("CX detect_intent falhou sem excecao registrada")
