import json
import logging
import time

import requests
from requests import exceptions as req_exc


def _get_retry_settings(settings):
    attempts = settings.get("TWILIO_POST_RETRY_ATTEMPTS", 2)
    backoff = settings.get("TWILIO_POST_RETRY_BACKOFF_SECONDS", 0.3)
    try:
        attempts = int(attempts)
    except (TypeError, ValueError):
        attempts = 2
    try:
        backoff = float(backoff)
    except (TypeError, ValueError):
        backoff = 0.3
    attempts = max(1, attempts)
    backoff = max(0.0, backoff)
    return attempts, backoff


def _is_transient_post_error(exc: Exception) -> bool:
    return isinstance(exc, (req_exc.SSLError, req_exc.ConnectionError, req_exc.Timeout))


def _post_with_retry(*, session, url, data, auth, timeout, attempts, backoff):
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            resp = session.post(url, data=data, auth=auth, timeout=timeout)
            resp.raise_for_status()
            return resp
        except req_exc.RequestException as exc:
            last_exc = exc
            if not _is_transient_post_error(exc) or attempt >= attempts:
                raise
            sleep_s = backoff * (2 ** (attempt - 1))
            logging.warning(
                "Falha transitoria no POST Twilio (%s) tentativa %d/%d sleep=%.2fs",
                type(exc).__name__,
                attempt,
                attempts,
                sleep_s,
            )
            time.sleep(sleep_s)
    if last_exc:
        raise last_exc


def send_whatsapp_text(
    to_whatsapp: str,
    body: str,
    messaging_service_sid: str = None,
    *,
    settings,
    http_session,
) -> bool:
    account_sid = (settings.get("TWILIO_ACCOUNT_SID") or "").strip()
    auth_token = (settings.get("TWILIO_AUTH_TOKEN_REST") or settings.get("AUTH_TOKEN") or "").strip()
    from_number = (settings.get("TWILIO_FROM") or "").strip()

    if not account_sid or not auth_token:
        logging.error("Sem credenciais Twilio REST; não dá para enviar via API.")
        return False
    if not (messaging_service_sid or from_number):
        logging.error("Sem MessagingServiceSid e sem TWILIO_FROM; não dá para enviar via API.")
        return False

    to = to_whatsapp if to_whatsapp.startswith("whatsapp:") else f"whatsapp:{to_whatsapp}"
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    data = {"To": to, "Body": body}
    if messaging_service_sid:
        data["MessagingServiceSid"] = messaging_service_sid
    else:
        data["From"] = from_number

    session = http_session or requests.Session()
    attempts, backoff = _get_retry_settings(settings)

    try:
        resp = _post_with_retry(
            session=session,
            url=url,
            data=data,
            auth=(account_sid, auth_token),
            timeout=30,
            attempts=attempts,
            backoff=backoff,
        )
        sid = resp.json().get("sid")
        logging.info(f"Enviado via REST: SID={sid} para {to}")
        return True
    except req_exc.RequestException as exc:
        logging.error(f"Falha no envio REST: {exc}")
        if hasattr(exc, "response") and exc.response is not None:
            logging.error(f"Response: {exc.response.text}")
        return False


def send_twilio_template(
    to_e164_plus: str,
    content_sid: str,
    vars_dict: dict = None,
    messaging_service_sid: str = None,
    *,
    settings,
    http_session,
):
    account_sid = (settings.get("TWILIO_ACCOUNT_SID") or "").strip()
    auth_token = (settings.get("TWILIO_AUTH_TOKEN_REST") or settings.get("AUTH_TOKEN") or "").strip()
    from_number = (settings.get("TWILIO_FROM") or "").strip()

    if not account_sid or not auth_token:
        logging.error("Credenciais Twilio REST não configuradas")
        raise ValueError("TWILIO_ACCOUNT_SID e TWILIO_AUTH_TOKEN_REST são obrigatórios")

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    vars_dict = vars_dict or {}
    user_name = vars_dict.get("user_name") or vars_dict.get("1") or "cliente"
    content_vars = {"user_name": user_name, "1": user_name}
    data = {
        "To": f"whatsapp:{to_e164_plus}" if not to_e164_plus.startswith("whatsapp:") else to_e164_plus,
        "ContentSid": content_sid,
        "ContentVariables": json.dumps(content_vars, ensure_ascii=False),
    }
    if messaging_service_sid:
        data["MessagingServiceSid"] = messaging_service_sid
    elif from_number:
        data["From"] = from_number
    else:
        logging.error("Nem MessagingServiceSid nem TWILIO_FROM configurados")
        raise ValueError("Configure TWILIO_WHATSAPP_FROM ou passe messaging_service_sid")

    logging.info(f"Enviando template {content_sid} para {to_e164_plus} com vars: {content_vars}")

    session = http_session or requests.Session()
    attempts, backoff = _get_retry_settings(settings)

    try:
        resp = _post_with_retry(
            session=session,
            url=url,
            data=data,
            auth=(account_sid, auth_token),
            timeout=20,
            attempts=attempts,
            backoff=backoff,
        )
        logging.info(f"Template enviado com sucesso: SID={resp.json().get('sid')}")
        return resp
    except req_exc.RequestException as exc:
        logging.error(f"Erro ao enviar template: {exc}")
        if hasattr(exc, "response") and exc.response is not None:
            logging.error(f"Response: {exc.response.text}")
        raise
