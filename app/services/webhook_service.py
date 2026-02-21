import logging
import threading
import time
import uuid

from flask import Response
from twilio.request_validator import RequestValidator
from google.cloud import firestore

from app.core.logging import log_event
from app.services.cx_service import cx_all_params_dict, detect_intent_text
from app.services.transcription_service import is_audio_media_type, transcribe_twilio_audio
from app.services.twilio_service import send_whatsapp_text


def twiml_empty(status: int = 200) -> Response:
    twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response></Response>"""
    resp = Response(twiml, status=status, mimetype="text/xml; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store"
    return resp


_message_buffers = {}
_buffers_lock = threading.Lock()
FALLBACK_STABILITY_TEXT = "Tivemos um problema de estabilidade, pode repetir sua pergunta?"


def is_valid_twilio_request(req, auth_token: str) -> bool:
    if not auth_token:
        return True
    signature = req.headers.get("X-Twilio-Signature", "")
    params = req.form.to_dict(flat=True)
    validator = RequestValidator(auth_token)
    try:
        ok = validator.validate(req.url, params, signature)
        if not ok:
            logging.warning(
                "Assinatura inválida: url=%s proto=%s host=%s",
                req.url,
                req.headers.get("X-Forwarded-Proto"),
                req.headers.get("X-Forwarded-Host"),
            )
        return ok
    except Exception:
        return False


def _session_id_from_from_field(from_field: str) -> str:
    if not from_field:
        return str(uuid.uuid4())
    sid = from_field.replace("whatsapp:", "").replace("+", "").strip()
    return sid or str(uuid.uuid4())


def _conversation_id_e164(from_field: str) -> str:
    sid = _session_id_from_from_field(from_field)
    return f"+{sid}" if sid and not sid.startswith("+") else sid


def _is_truthy(value) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    if isinstance(value, dict):
        for v in value.values():
            if _is_truthy(v):
                return True
    return False


def _bool_setting(settings, key, default=False) -> bool:
    if key not in settings:
        return default
    return _is_truthy(settings.get(key))


def _normalize_for_exact_match(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def _handoff_from_cx(resp, texts, allow_param: bool, settings) -> bool:
    try:
        for text in texts or []:
            text_norm = _normalize_for_exact_match(text)
            for hint in settings.get("DF_HANDOFF_TEXT_HINTS", []):
                if hint and text_norm == hint:
                    logging.info("Handoff detectado via hint exato.")
                    return True

        if allow_param:
            sp_dict = cx_all_params_dict(resp)
            key = settings.get("DF_HANDOFF_PARAM")
            if key and _is_truthy(sp_dict.get(key)):
                logging.info("Handoff detectado via parametro %s: %s", key, sp_dict.get(key))
                return True

    except Exception:
        pass
    return False


def _join_bot_texts(texts):
    try:
        parts = [t.strip() for t in (texts or []) if isinstance(t, str) and t.strip()]
        return "\n\n".join(parts)
    except Exception:
        return ""


def _merge_audio_transcript(body: str, transcript: str) -> str:
    if "[Audio]" in (body or ""):
        return body.replace("[Audio]", transcript)
    if (body or "").strip():
        return f"{body}\n\n[Transcricao de audio] {transcript}"
    return transcript


def extract_inbound_request(req):
    frm = req.form.get("From", "")
    to = req.form.get("To", "")
    body = req.form.get("Body", "") or ""
    sid = req.form.get("MessageSid")
    profile_name = (req.form.get("ProfileName") or "").strip()
    wa_id = (req.form.get("WaId") or "").strip()

    try:
        num_media = int(req.form.get("NumMedia", 0))
    except (TypeError, ValueError):
        num_media = 0

    media_url = None
    media_type = None

    if num_media > 0:
        media_url = req.form.get("MediaUrl0")
        media_type = req.form.get("MediaContentType0")

        if is_audio_media_type(media_type) and not body.strip():
            body = "[Audio]"
        elif media_type and media_type.startswith("image/") and not body.strip():
            body = "[Imagem]"
        elif media_type and media_type.startswith("video/") and not body.strip():
            body = "[Video]"
        elif media_type and not body.strip():
            body = "[Documento]"

    conversation_id = _conversation_id_e164(frm)
    session_id = _session_id_from_from_field(frm)

    return {
        "from": frm,
        "to": to,
        "body": body,
        "sid": sid,
        "profile_name": profile_name,
        "wa_id": wa_id,
        "media_url": media_url,
        "media_type": media_type,
        "conversation_id": conversation_id,
        "session_id": session_id,
    }


def _get_or_create_buffer(conversation_id: str) -> dict:
    with _buffers_lock:
        if conversation_id not in _message_buffers:
            _message_buffers[conversation_id] = {
                "messages": [],
                "timer": None,
                "lock": threading.Lock(),
                "first_ts": None,
                "first_data": None,
            }
        return _message_buffers[conversation_id]


def _clear_buffer(conversation_id: str):
    with _buffers_lock:
        buffer = _message_buffers.pop(conversation_id, None)
        if buffer and buffer.get("timer"):
            try:
                buffer["timer"].cancel()
            except Exception:
                pass


def _calculate_next_delay(buffer: dict, settings) -> float:
    now = time.time()
    first_ts = buffer.get("first_ts") or now
    elapsed = now - first_ts

    initial_seconds = float(settings.get("MESSAGE_DEBOUNCE_INITIAL_SECONDS", 5.0))
    extend_seconds = float(settings.get("MESSAGE_DEBOUNCE_EXTEND_SECONDS", 3.0))
    max_seconds = float(settings.get("MESSAGE_DEBOUNCE_MAX_SECONDS", 10.0))

    remaining_to_max = max_seconds - elapsed
    if remaining_to_max <= 0:
        return 0.1

    if len(buffer.get("messages", [])) <= 1:
        return min(initial_seconds, remaining_to_max)

    return min(extend_seconds, remaining_to_max)


def _process_aggregated_messages(conversation_id: str):
    buffer = _message_buffers.get(conversation_id)
    if not buffer:
        logging.warning("Buffer nao encontrado para %s no momento do processamento", conversation_id)
        return

    with buffer["lock"]:
        messages = list(buffer.get("messages", []))
        first_data = buffer.get("first_data")

        if not messages or not first_data:
            logging.info("Buffer vazio ou sem dados para %s, ignorando", conversation_id)
            _clear_buffer(conversation_id)
            return

        buffer["timer"] = None

    _clear_buffer(conversation_id)

    message_bodies = [m.get("body", "").strip() for m in messages if m.get("body", "").strip()]
    if not message_bodies:
        logging.info("Nenhum corpo de mensagem valido para %s", conversation_id)
        return

    if len(message_bodies) == 1:
        aggregated_body = message_bodies[0]
    else:
        aggregated_body = " | ".join(message_bodies)

    frm = first_data.get("frm")
    session_id = first_data.get("session_id")
    conv_data = first_data.get("conv_data", {})
    inbound_id = first_data.get("inbound_id", "unknown")
    settings = first_data.get("settings", {})
    repo = first_data.get("repo")
    cx_client = first_data.get("cx_client")
    http_session = first_data.get("http_session")
    speech_client = first_data.get("speech_client")

    media_url = None
    media_type = None
    media_message_id = None
    for msg in messages:
        if msg.get("media_url"):
            media_url = msg["media_url"]
            media_type = msg.get("media_type")
            media_message_id = msg.get("inbound_id")
            break

    aggregated_id = f"agg:{inbound_id}:{len(messages)}"

    logging.info(
        "Processando %d mensagens agregadas para %s: %r",
        len(messages),
        conversation_id,
        aggregated_body[:100],
    )
    log_event(
        "aggregated_messages",
        conversation_id=conversation_id,
        message_count=len(messages),
        aggregated_text=aggregated_body[:200],
    )

    process_message_async(
        frm=frm,
        body=aggregated_body,
        sid=aggregated_id,
        conversation_id=conversation_id,
        session_id=session_id,
        media_url=media_url,
        media_type=media_type,
        conv_data=conv_data,
        settings=settings,
        repo=repo,
        cx_client=cx_client,
        http_session=http_session,
        speech_client=speech_client,
        source_message_id=media_message_id,
    )


def _add_to_aggregation_buffer(
    *,
    conversation_id: str,
    frm: str,
    body: str,
    inbound_id: str,
    session_id: str,
    media_url: str,
    media_type: str,
    conv_data: dict,
    settings,
    repo,
    cx_client,
    http_session,
    speech_client=None,
):
    if not _bool_setting(settings, "FEATURE_MESSAGE_AGGREGATION", default=True):
        return False

    buffer = _get_or_create_buffer(conversation_id)

    with buffer["lock"]:
        now = time.time()
        if not buffer["messages"]:
            buffer["first_ts"] = now
            buffer["first_data"] = {
                "frm": frm,
                "session_id": session_id,
                "conv_data": conv_data,
                "inbound_id": inbound_id,
                "settings": settings,
                "repo": repo,
                "cx_client": cx_client,
                "http_session": http_session,
                "speech_client": speech_client,
            }

        buffer["messages"].append({
            "body": body,
            "inbound_id": inbound_id,
            "media_url": media_url,
            "media_type": media_type,
            "ts": now,
        })

        if buffer.get("timer"):
            try:
                buffer["timer"].cancel()
            except Exception:
                pass

        delay = _calculate_next_delay(buffer, settings)

        logging.info(
            "Mensagem %d adicionada ao buffer de %s. Delay: %.1fs. Texto: %r",
            len(buffer["messages"]),
            conversation_id,
            delay,
            body[:50] if body else "",
        )

        timer = threading.Timer(delay, _process_aggregated_messages, args=(conversation_id,))
        timer.daemon = True
        buffer["timer"] = timer
        timer.start()

    return True


def get_aggregation_debug_info(settings):
    with _buffers_lock:
        info = {}
        for conv_id, buf in _message_buffers.items():
            info[conv_id] = {
                "message_count": len(buf.get("messages", [])),
                "first_ts": buf.get("first_ts"),
                "has_timer": buf.get("timer") is not None,
            }

    return {
        "aggregation_enabled": _bool_setting(settings, "FEATURE_MESSAGE_AGGREGATION", default=True),
        "config": {
            "initial_seconds": float(settings.get("MESSAGE_DEBOUNCE_INITIAL_SECONDS", 5.0)),
            "extend_seconds": float(settings.get("MESSAGE_DEBOUNCE_EXTEND_SECONDS", 3.0)),
            "max_seconds": float(settings.get("MESSAGE_DEBOUNCE_MAX_SECONDS", 10.0)),
        },
        "active_buffers": info,
    }


def handle_webhook(req, *, settings, repo, cx_client, http_session, speech_client=None):
    if not is_valid_twilio_request(req, settings.get("AUTH_TOKEN")):
        logging.warning("Twilio signature inválida para URL %s", req.url)
        return Response("Invalid signature", status=403)

    inbound = extract_inbound_request(req)

    frm = inbound["from"]
    to = inbound["to"]
    body = inbound["body"]
    sid = inbound["sid"]
    profile_name = inbound.get("profile_name") or ""
    media_type = inbound["media_type"]
    media_url = inbound["media_url"]
    conversation_id = inbound["conversation_id"]
    session_id = inbound["session_id"]

    logging.info("Inbound: from=%s sid=%s body=%r", frm, sid, body[:50] if body else "")
    log_event(
        "inbound",
        conversation_id=conversation_id,
        from_=frm,
        to=to,
        twilio_sid=sid,
        text=body,
        media_type=media_type,
        wa_profile_name=profile_name or None,
    )

    conv_snap, _existed = repo.ensure_conversation(conversation_id, session_id)
    conv_data = conv_snap.to_dict() if conv_snap else {}

    idem = req.headers.get("I-Twilio-Idempotency-Token")
    inbound_id = sid or idem or str(uuid.uuid4())

    created = repo.add_message_if_new(
        conversation_id,
        inbound_id,
        "in",
        "user",
        body,
        media_url=media_url,
        media_type=media_type,
    )
    if not created:
        logging.info(
            "Webhook duplicado (inbound ja existe). Ignorando processamento. inbound_id=%s sid=%s idem=%s",
            inbound_id,
            sid,
            idem,
        )
        return twiml_empty(status=200)

    conv_updates = {
        "last_message_text": body,
        "last_in_from": "user",
        "last_inbound_at": firestore.SERVER_TIMESTAMP,
    }
    if profile_name:
        conv_updates["wa_profile_name"] = profile_name

    repo.update_conversation(conversation_id, **conv_updates)

    added_to_buffer = _add_to_aggregation_buffer(
        conversation_id=conversation_id,
        frm=frm,
        body=body,
        inbound_id=inbound_id,
        session_id=session_id,
        media_url=media_url,
        media_type=media_type,
        conv_data=conv_data,
        settings=settings,
        repo=repo,
        cx_client=cx_client,
        http_session=http_session,
        speech_client=speech_client,
    )

    if not added_to_buffer:
        thread = threading.Thread(
            target=process_message_async,
            args=(
                frm,
                body,
                inbound_id,
                conversation_id,
                session_id,
                media_url,
                media_type,
                conv_data,
            ),
            kwargs={
                "settings": settings,
                "repo": repo,
                "cx_client": cx_client,
                "http_session": http_session,
                "speech_client": speech_client,
                "source_message_id": inbound_id,
            },
            daemon=True,
        )
        thread.start()
        logging.info("Respondendo ao Twilio imediatamente (processamento async iniciado - sem agregacao)")
    else:
        logging.info("Respondendo ao Twilio imediatamente (mensagem adicionada ao buffer de agregacao)")
    return twiml_empty(status=200)


def process_message_async(
    frm: str,
    body: str,
    sid: str,
    conversation_id: str,
    session_id: str,
    media_url: str,
    media_type: str,
    conv_data: dict,
    *,
    settings,
    repo,
    cx_client,
    http_session,
    speech_client=None,
    source_message_id: str | None = None,
):
    try:
        logging.info("Processando async: %s - %s...", conversation_id, body[:50])

        inbound_id = sid or session_id
        transcription_message_id = source_message_id or inbound_id
        out_msg_id = f"bot:{inbound_id}"

        status = (conv_data.get("status") or "bot").lower()
        handoff_active = bool(conv_data.get("handoff_active"))

        if status in ("pending_handoff", "claimed", "active") or handoff_active:
            if settings.get("FEATURE_DISABLE_HANDOFF") and settings.get("FEATURE_FORCE_BOT_WHEN_HANDOFF_DISABLED"):
                logging.info(
                    "Handoff desabilitado: convertendo conversa (%s) para bot: %s",
                    status,
                    conversation_id,
                )
                repo.update_conversation(
                    conversation_id,
                    status="bot",
                    handoff_active=False,
                    assignee=None,
                    assignee_name=None,
                )
                status = "bot"
                handoff_active = False
            else:
                logging.info("Handoff mode (%s): sem resposta automática para %s", status, conversation_id)
                return

        was_resolved = status == "resolved"

        reset_cx_params = {}
        if was_resolved:
            logging.info("Reabrindo bot após resolved para %s", conversation_id)
            repo.update_conversation(
                conversation_id,
                status="bot",
                handoff_active=False,
                assignee=None,
                assignee_name=None,
            )
            status = "bot"

            if settings.get("DF_HANDOFF_PARAM"):
                reset_cx_params[settings.get("DF_HANDOFF_PARAM")] = None
            reset_cx_params["handoff_request"] = None
            reset_cx_params["handoff_requested"] = None

        stored_params = conv_data.get("session_parameters") or {}
        saved_name = None
        raw_name = stored_params.get("user_name")

        if raw_name:
            if isinstance(raw_name, str):
                saved_name = raw_name
            elif isinstance(raw_name, dict):
                saved_name = raw_name.get("user_name")

        if saved_name and isinstance(saved_name, str):
            reset_cx_params["user_name"] = saved_name

        if (
            speech_client
            and _bool_setting(settings, "FEATURE_AUDIO_TRANSCRIPTION", default=True)
            and media_url
            and is_audio_media_type(media_type)
        ):
            logging.info(
                "Audio recebido para %s (msg=%s type=%s). Iniciando transcricao.",
                conversation_id,
                transcription_message_id,
                media_type,
            )

            transcript = transcribe_twilio_audio(
                speech_client,
                media_url,
                media_type,
                settings=settings,
                http_session=http_session,
            )

            if transcript:
                body = _merge_audio_transcript(body, transcript)
                log_event(
                    "audio_transcribed",
                    conversation_id=conversation_id,
                    message_id=transcription_message_id,
                    transcript_length=len(transcript),
                )

                if transcription_message_id:
                    try:
                        repo.update_message(
                            conversation_id,
                            transcription_message_id,
                            transcription=transcript,
                            original_media_type=media_type,
                            transcription_source="google-stt",
                        )
                    except Exception as exc:
                        logging.warning(
                            "Falha ao salvar transcricao no Firestore (conv=%s msg=%s): %s",
                            conversation_id,
                            transcription_message_id,
                            exc,
                        )

                try:
                    repo.update_conversation(conversation_id, last_message_text=body)
                except Exception:
                    logging.warning("Falha ao atualizar last_message_text com transcricao", exc_info=True)
            else:
                log_event(
                    "audio_transcription_empty",
                    conversation_id=conversation_id,
                    message_id=transcription_message_id,
                )
                fallback = (settings.get("STT_FALLBACK_TEXT") or "").strip()
                if fallback:
                    body = _merge_audio_transcript(body, fallback)

        try:
            texts, resp = detect_intent_text(
                cx_client,
                settings,
                session_id,
                body,
                user_id=conversation_id,
                session_params=reset_cx_params if reset_cx_params else None,
                timeout_s=float(settings.get("CX_TIMEOUT_SECONDS", 15.0)),
                attempts=int(settings.get("CX_RETRY_ATTEMPTS", 3)),
            )

            params_dict = cx_all_params_dict(resp)
            if params_dict:
                logging.info(
                    "Salvando session_parameters para %s: %s",
                    conversation_id,
                    list(params_dict.keys()),
                )
                repo.update_conversation(conversation_id, session_parameters=params_dict)

        except Exception:
            logging.error("DetectIntent falhou", exc_info=True)
            reply_text = FALLBACK_STABILITY_TEXT
            created_out = repo.add_message_if_new(conversation_id, out_msg_id, "out", "bot", reply_text)
            if created_out:
                fallback_success = send_whatsapp_text(frm, reply_text, settings=settings, http_session=http_session)
                if not fallback_success:
                    logging.error(
                        "Falha ao enviar fallback para %s out_msg_id=%s inbound_id=%s",
                        conversation_id,
                        out_msg_id,
                        inbound_id,
                    )
            return

        allow_handoff_param = status == "bot" and bool(settings.get("DF_HANDOFF_PARAM"))
        handoff_requested = _handoff_from_cx(resp, texts, allow_param=allow_handoff_param, settings=settings)

        bot_reply_text = _join_bot_texts(texts)

        if handoff_requested:
            if settings.get("FEATURE_DISABLE_HANDOFF"):
                logging.info("CX pediu handoff, mas está desabilitado: %s", conversation_id)
                log_event("handoff_disabled", conversation_id=conversation_id)
                reply_text = (
                    settings.get("HANDOFF_DISABLED_TEXT")
                    or bot_reply_text
                    or settings.get("HANDOFF_ACK_TEXT")
                    or FALLBACK_STABILITY_TEXT
                )
            else:
                logging.info("CX pediu handoff: %s -> status=pending_handoff", conversation_id)
                log_event("handoff_pending", conversation_id=conversation_id)
                repo.update_conversation(
                    conversation_id,
                    status="pending_handoff",
                    handoff_active=False,
                    assignee=None,
                    assignee_name=None,
                    pending_since=firestore.SERVER_TIMESTAMP,
                )
                reply_text = (
                    bot_reply_text
                    or settings.get("HANDOFF_ACK_TEXT")
                    or FALLBACK_STABILITY_TEXT
                )
        else:
            reply_text = bot_reply_text or FALLBACK_STABILITY_TEXT

        created_out = repo.add_message_if_new(conversation_id, out_msg_id, "out", "bot", reply_text)
        if not created_out:
            logging.info("Resposta do bot ja registrada para inbound_id=%s (skip send)", inbound_id)
            return

        success = send_whatsapp_text(frm, reply_text, settings=settings, http_session=http_session)

        if success:
            logging.info(
                "Resposta enviada com sucesso para %s out_msg_id=%s inbound_id=%s",
                conversation_id,
                out_msg_id,
                inbound_id,
            )
        else:
            logging.error(
                "Falha ao enviar resposta para %s out_msg_id=%s inbound_id=%s",
                conversation_id,
                out_msg_id,
                inbound_id,
            )

    except Exception as exc:
        logging.error(f"Erro no processamento async: {exc}", exc_info=True)
