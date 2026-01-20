import os, uuid, logging, json, requests, html
from datetime import datetime, timezone
from flask import Flask, request, Response
from twilio.request_validator import RequestValidator
from werkzeug.middleware.proxy_fix import ProxyFix
from collections.abc import Mapping
import threading

from google.cloud import firestore
from google.cloud import dialogflowcx_v3 as dfcx
from google.api_core.client_options import ClientOptions
from google.protobuf.struct_pb2 import Struct, Value
from google.protobuf.json_format import MessageToDict

# ================== RETRY CONFIG (SSLEOFError patch) ==================
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

def _get_retry_session(retries=3, backoff_factor=0.5, status_forcelist=(500, 502, 503, 504)):
    """Sessão requests com retry automático para erros de SSL/conexão."""
    session = requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        allowed_methods=["GET", "POST"],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

# Sessão global com retry (reutilizada para performance)
http_session = _get_retry_session()

# ================== FLASK ==================
app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ================== ENV / CONFIG ==================
AUTH_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN", "").strip()

# Twilio REST API (para envio de templates)
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN_REST = os.getenv("TWILIO_AUTH_TOKEN_REST", "").strip() or AUTH_TOKEN
TWILIO_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()  # ex: "whatsapp:+553123915744"

DF_PROJECT   = os.getenv("DF_PROJECT_ID", "").strip()
DF_LOCATION  = os.getenv("DF_LOCATION", "global").strip()
DF_AGENT_ID  = os.getenv("DF_AGENT_ID", "").strip()
LANG_CODE    = os.getenv("DF_LANG_CODE", "pt-br").strip()

# Handoff
DF_HANDOFF_PARAM   = (os.getenv("DF_HANDOFF_PARAM", "handoff_requested") or "").strip()
DF_HANDOFF_MARKER  = os.getenv("DF_HANDOFF_MARKER", "##HANDOFF_TRIGGER##")
FEATURE_AUTOREPLY_DURING_PENDING = (os.getenv("FEATURE_AUTOREPLY_DURING_PENDING", "false").lower() == "true")
HANDOFF_ACK_TEXT   = os.getenv("HANDOFF_ACK_TEXT", "Certo! Um atendente vai assumir esta conversa em instantes.")
FEATURE_DISABLE_HANDOFF = (os.getenv("FEATURE_DISABLE_HANDOFF", "false").lower() == "true")
HANDOFF_DISABLED_TEXT  = os.getenv(
    "HANDOFF_DISABLED_TEXT",
    "Olá! Nossos atendentes estão em recesso de fim de ano e o atendimento humano está temporariamente indisponível. "
    "Retornamos em 26/12/2025. Você pode deixar sua mensagem por aqui e responderemos assim que voltarmos."
).strip()
FEATURE_FORCE_BOT_WHEN_HANDOFF_DISABLED = (os.getenv("FEATURE_FORCE_BOT_WHEN_HANDOFF_DISABLED", "true").lower() == "true")

DF_HANDOFF_TEXT_HINTS = [
    s.strip().casefold() for s in
    (os.getenv("DF_HANDOFF_TEXT_HINTS", "transferindo você agora para um de nossos atendentes,atendente continuará seu atendimento em instantes").split(","))
    if s.strip()
]

# Firestore coleções
FS_CONV_COLL    = os.getenv("FS_CONV_COLL", "conversations").strip()
FS_MSG_SUBCOLL  = os.getenv("FS_MSG_SUBCOLL", "messages").strip()

# ================== CLIENTES GCP ==================
fs = firestore.Client()
_endpoint = f"{DF_LOCATION}-dialogflow.googleapis.com"
df_client = dfcx.SessionsClient(client_options=ClientOptions(api_endpoint=_endpoint))

# ================== HELPERS ==================
def log_event(action, **kw):
    try:
        payload = {"component": "webh", "action": action}
        payload.update({k: v for k, v in kw.items() if v is not None})
        app.logger.info(json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


def _twiml_empty(status: int = 200) -> Response:
    """Retorna TwiML vazio - acknowledge rápido sem enviar mensagem."""
    twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response></Response>"""
    resp = Response(twiml, status=status, mimetype="text/xml; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _struct_to_dict(obj):
    """
    Converte objetos do Dialogflow CX (Struct/Value/MapComposite/proto-plus) em dict/list/valores nativos.
    """
    if obj is None:
        return {}

    # Desembrulha proto-plus (obj._pb -> mensagem protobuf real)
    if hasattr(obj, "_pb"):
        obj = obj._pb

    # Caso 1: é um Struct "de verdade"
    if isinstance(obj, Struct):
        try:
            return MessageToDict(obj, preserving_proto_field_name=True)
        except Exception as e:
            logging.warning(f"MessageToDict falhou para Struct: {e}")

    # Caso 2: é um Value
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
            return _struct_to_dict(obj.struct_value)
        if kind == "list_value":
            return [_struct_to_dict(v) for v in obj.list_value.values]
        return None

    # Caso 3: é um Mapping (ex.: MapComposite do proto-plus)
    if isinstance(obj, Mapping):
        def cv(v):
            if hasattr(v, "_pb"):
                v = v._pb
            if isinstance(v, (Struct, Value)):
                return _struct_to_dict(v)
            if isinstance(v, Mapping):
                return _struct_to_dict(v)
            return v
        return {k: cv(v) for k, v in obj.items()}

    logging.warning(f"_struct_to_dict recebeu tipo inesperado: {type(obj).__name__}")
    return {}

def _cx_all_params_dict(resp) -> dict:
    out = {}
    try:
        # 1) query_result.parameters
        qr = getattr(resp, "query_result", None)
        if qr and getattr(qr, "parameters", None):
            out.update(_struct_to_dict(qr.parameters) or {})
    except Exception:
        pass

    try:
        # 2) query_result.session_info.parameters (quando existir)
        qr = getattr(resp, "query_result", None)
        si = getattr(qr, "session_info", None) if qr else None
        if si and getattr(si, "parameters", None):
            out.update(_struct_to_dict(si.parameters) or {})
    except Exception:
        pass

    try:
        # 3) resp.session_info.parameters (dependendo da versão/SDK)
        si2 = getattr(resp, "session_info", None)
        if si2 and getattr(si2, "parameters", None):
            out.update(_struct_to_dict(si2.parameters) or {})
    except Exception:
        pass

    return out

def send_whatsapp_text(to_whatsapp: str, body: str, messaging_service_sid: str = None) -> bool:
    """Envia mensagem WhatsApp via REST API - método principal de envio."""
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN_REST:
        logging.error("Sem credenciais Twilio REST; não dá para enviar via API.")
        return False
    if not (messaging_service_sid or TWILIO_FROM):
        logging.error("Sem MessagingServiceSid e sem TWILIO_FROM; não dá para enviar via API.")
        return False
    to = to_whatsapp if to_whatsapp.startswith("whatsapp:") else f"whatsapp:{to_whatsapp}"
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    data = {"To": to, "Body": body}
    if messaging_service_sid:
        data["MessagingServiceSid"] = messaging_service_sid
    else:
        data["From"] = TWILIO_FROM
    try:
        # PATCH: Usando http_session com retry automático
        r = http_session.post(url, data=data, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN_REST), timeout=30)
        r.raise_for_status()
        sid = r.json().get("sid")
        logging.info(f"📨 Enviado via REST: SID={sid} para {to}")
        return True
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Falha no envio REST: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logging.error(f"Response: {e.response.text}")
        return False


def send_twilio_template(to_e164_plus: str, content_sid: str, vars_dict: dict = None, messaging_service_sid: str = None):
    """Envia template WhatsApp via Twilio REST API com ContentVariables corretas."""
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN_REST:
        logging.error("Credenciais Twilio REST não configuradas")
        raise ValueError("TWILIO_ACCOUNT_SID e TWILIO_AUTH_TOKEN_REST são obrigatórios")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    vars_dict = vars_dict or {}
    user_name = vars_dict.get("user_name") or vars_dict.get("1") or "cliente"
    content_vars = {"user_name": user_name, "1": user_name}
    data = {"To": f"whatsapp:{to_e164_plus}" if not to_e164_plus.startswith("whatsapp:") else to_e164_plus,
            "ContentSid": content_sid,
            "ContentVariables": json.dumps(content_vars, ensure_ascii=False)}
    if messaging_service_sid:
        data["MessagingServiceSid"] = messaging_service_sid
    elif TWILIO_FROM:
        data["From"] = TWILIO_FROM
    else:
        logging.error("Nem MessagingServiceSid nem TWILIO_FROM configurados")
        raise ValueError("Configure TWILIO_WHATSAPP_FROM ou passe messaging_service_sid")
    logging.info(f"📤 Enviando template {content_sid} para {to_e164_plus} com vars: {content_vars}")
    try:
        # PATCH: Usando http_session com retry automático
        resp = http_session.post(url, data=data, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN_REST), timeout=20)
        resp.raise_for_status()
        logging.info(f"✅ Template enviado com sucesso: SID={resp.json().get('sid')}")
        return resp
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Erro ao enviar template: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logging.error(f"Response: {e.response.text}")
        raise


def _is_truthy(value) -> bool:
    """Verifica se um valor representa 'true' (boolean, string, ou map aninhado)."""
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


def _handoff_from_cx(resp, texts, allow_param: bool) -> bool:
    """Detecta pedido de handoff vindo do CX."""
    try:
        if any((DF_HANDOFF_MARKER in (t or "")) for t in texts):
            return True

        for t in texts or []:
            t_norm = (t or "").casefold()
            for hint in DF_HANDOFF_TEXT_HINTS:
                if hint and hint in t_norm:
                    return True

        for m in resp.query_result.response_messages:
            try:
                if m.payload:
                    payload_dict = _struct_to_dict(m.payload)
                    if _is_truthy(payload_dict.get("handoff")):
                        return True
            except Exception:
                pass

        if allow_param:
            sp_dict = _cx_all_params_dict(resp)
            for key in [DF_HANDOFF_PARAM, "handoff_request", "handoff_requested"]:
                if not key:
                    continue
                if _is_truthy(sp_dict.get(key)):
                    logging.info(f"Handoff detectado via parametro {key}: {sp_dict.get(key)}")
                    return True

    except Exception:
        pass
    return False


def _is_valid_twilio_request(req) -> bool:
    if not AUTH_TOKEN:
        return True
    signature = req.headers.get("X-Twilio-Signature", "")
    params = req.form.to_dict(flat=True)
    validator = RequestValidator(AUTH_TOKEN)
    try:
        ok = validator.validate(req.url, params, signature)
        if not ok:
            logging.warning("Assinatura inválida: url=%s proto=%s host=%s",
                            req.url, req.headers.get("X-Forwarded-Proto"), req.headers.get("X-Forwarded-Host"))
        return ok
    except Exception:
        return False


def _session_id_from_from_field(from_field: str) -> str:
    if not from_field:
        return str(uuid.uuid4())
    s = from_field.replace("whatsapp:", "").replace("+", "").strip()
    return s or str(uuid.uuid4())


def _conversation_id_e164(from_field: str) -> str:
    """conversation_id = E.164 com '+', ex.: '+553183440484'"""
    sid = _session_id_from_from_field(from_field)
    return f"+{sid}" if sid and not sid.startswith("+") else sid


def _cx_session_path(session_id: str) -> str:
    return f"projects/{DF_PROJECT}/locations/{DF_LOCATION}/agents/{DF_AGENT_ID}/sessions/{session_id}"


def _cx_detect_intent_text(session_id, text, user_id=None, session_params=None):
    """Chama Dialogflow CX."""
    session = _cx_session_path(session_id)
    
    query_params = dfcx.QueryParameters()
    has_params = False
    
    if user_id or session_params:
        params_struct = Struct()
        
        if user_id:
            params_struct.fields["user_id"].string_value = user_id
            has_params = True

        if session_params:
            for k, v in session_params.items():
                if v is None:
                    params_struct.fields[k].null_value = 0 
                elif isinstance(v, bool):
                    params_struct.fields[k].bool_value = v
                else:
                    params_struct.fields[k].string_value = str(v)
            has_params = True
            
        if has_params:
            query_params.parameters = params_struct
            logging.info(f"📤 CX QueryParams: user_id={user_id}, extras={session_params}")
    
    req = dfcx.DetectIntentRequest(
        session=session,
        query_input=dfcx.QueryInput(
            text=dfcx.TextInput(text=text), 
            language_code=LANG_CODE
        ),
        query_params=query_params if has_params else None,
    )
    
    resp = df_client.detect_intent(request=req)
    texts = []
    for m in resp.query_result.response_messages:
        if m.text and m.text.text:
            for piece in m.text.text:
                if piece:
                    texts.append(piece)
    return texts, resp


# ------------------ Firestore utils ------------------

def _conv_ref(conversation_id: str):
    return fs.collection(FS_CONV_COLL).document(conversation_id)


def _msg_ref(conversation_id: str, message_id: str):
    return _conv_ref(conversation_id).collection(FS_MSG_SUBCOLL).document(message_id)


def _ensure_conversation(conversation_id: str, session_id: str):
    """Cria doc base se não existir; retorna (doc_snapshot, existed: bool)."""
    ref = _conv_ref(conversation_id)
    snap = ref.get()
    if snap.exists:
        return snap, True
    data = {
        "conversation_id": conversation_id,
        "user_id": conversation_id,
        "session_id": session_id,
        "status": "bot",
        "handoff_active": False,
        "assignee": None,
        "assignee_name": None,
        "last_message_text": "",
        "last_in_from": None,
        "unread_for_assignee": 0,
        "created_at": firestore.SERVER_TIMESTAMP,
        "updated_at": firestore.SERVER_TIMESTAMP,
        "lock_version": 0,
    }
    ref.set(data)
    return ref.get(), False


def _add_message_if_new(conversation_id: str, message_id: str, direction: str, by: str, text: str, media_url: str = None, media_type: str = None):
    """Idempotente: só cria se não existir."""
    if not message_id:
        message_id = str(uuid.uuid4())
    ref = _msg_ref(conversation_id, message_id)
    if ref.get().exists:
        return False
    
    msg_data = {
        "message_id": message_id,
        "direction": direction,
        "by": by,
        "text": text,
        "ts": firestore.SERVER_TIMESTAMP,
    }
    
    if media_url:
        msg_data["media_url"] = media_url
    if media_type:
        msg_data["media_type"] = media_type
    
    ref.set(msg_data)
    return True


def _update_conversation(conversation_id: str, **fields):
    fields["updated_at"] = firestore.SERVER_TIMESTAMP
    _conv_ref(conversation_id).set(fields, merge=True)


def _save_session_parameters(conversation_id: str, cx_response):
    try:
        if not cx_response:
            return
        params_dict = _cx_all_params_dict(cx_response)
        if not params_dict:
            return

        logging.info(f"💾 Salvando session_parameters para {conversation_id}: {list(params_dict.keys())}")
        _update_conversation(conversation_id, session_parameters=params_dict)

    except Exception as e:
        logging.warning(f"Erro ao salvar session_parameters: {e}")



def _join_bot_texts(texts):
    try:
        parts = [t.strip() for t in (texts or []) if isinstance(t, str) and t.strip()]
        return "\n\n".join(parts)
    except Exception:
        return ""


# ================== PROCESSAMENTO ASYNC ==================

def _process_message_async(frm: str, body: str, sid: str, conversation_id: str,
                           session_id: str, media_url: str, media_type: str, conv_data: dict):
    """
    Processa a mensagem em background e envia resposta via REST API.
    Esta função é executada em uma thread separada após o webhook já ter respondido ao Twilio.
    """
    try:
        logging.info(f"🔄 Processando async: {conversation_id} - {body[:50]}...")

        status = (conv_data.get("status") or "bot").lower()
        handoff_active = bool(conv_data.get("handoff_active"))

        # Se estiver em handoff/claimed/active, não processa
        if status in ("pending_handoff", "claimed", "active") or handoff_active:
            if FEATURE_DISABLE_HANDOFF and FEATURE_FORCE_BOT_WHEN_HANDOFF_DISABLED:
                logging.info("Handoff desabilitado: convertendo conversa (%s) para bot: %s", status, conversation_id)
                _update_conversation(
                    conversation_id,
                    status="bot",
                    handoff_active=False,
                    assignee=None,
                    assignee_name=None
                )
                status = "bot"
                handoff_active = False
            else:
                logging.info("Handoff mode (%s): sem resposta automática para %s", status, conversation_id)
                return  # Não envia nada

        # Guarda se veio de resolved (antes de qualquer alteração)
        was_resolved = (status == "resolved")

        # Se estava resolved, reabrir para bot e limpar flag de handoff no CX (evita “handoff antigo” preso)
        reset_cx_params = {}
        if was_resolved:
            logging.info("Reabrindo bot após resolved para %s", conversation_id)
            _update_conversation(
                conversation_id,
                status="bot",
                handoff_active=False,
                assignee=None,
                assignee_name=None
            )
            status = "bot"  # ✅ MUITO IMPORTANTE: atualiza a variável local

            # Limpa o parâmetro de handoff no CX para não vazar estado antigo
            # (se você usa DF_HANDOFF_PARAM="handoff_request", isso já resolve)
            if DF_HANDOFF_PARAM:
                reset_cx_params[DF_HANDOFF_PARAM] = None
            # opcional: limpar os dois nomes comuns, caso você tenha migrado nome de param
            reset_cx_params["handoff_request"] = None
            reset_cx_params["handoff_requested"] = None

        # Recupera o user_name salvo no Firestore (pra reforçar no CX)
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

        # Chama Dialogflow CX
        try:
            texts, resp = _cx_detect_intent_text(
                session_id,
                body,
                user_id=conversation_id,
                session_params=reset_cx_params if reset_cx_params else None
            )

            _save_session_parameters(conversation_id, resp)

        except Exception:
            logging.error("DetectIntent falhou", exc_info=True)
            reply_text = "Certo! Estou processando sua mensagem."
            _add_message_if_new(conversation_id, str(uuid.uuid4()), "out", "bot", reply_text)
            send_whatsapp_text(frm, reply_text)
            return

        # ✅ Verifica handoff (NÃO bloqueia por was_resolved)
        allow_handoff_param = (status == "bot" and bool(DF_HANDOFF_PARAM))
        handoff_requested = _handoff_from_cx(resp, texts, allow_param=allow_handoff_param)

        if handoff_requested:
            if FEATURE_DISABLE_HANDOFF:
                logging.info("CX pediu handoff, mas está desabilitado: %s", conversation_id)
                log_event("handoff_disabled", conversation_id=conversation_id)
                reply_text = HANDOFF_DISABLED_TEXT or HANDOFF_ACK_TEXT
            else:
                logging.info("CX pediu handoff: %s -> status=pending_handoff", conversation_id)
                log_event("handoff_pending", conversation_id=conversation_id)
                _update_conversation(
                    conversation_id,
                    status="pending_handoff",
                    handoff_active=False,
                    assignee=None,
                    assignee_name=None,
                    pending_since=firestore.SERVER_TIMESTAMP
                )
                reply_text = HANDOFF_ACK_TEXT
        else:
            reply_text = _join_bot_texts(texts) or "Certo! Estou processando sua mensagem."

        # Registra outbound do BOT no Firestore
        _add_message_if_new(conversation_id, str(uuid.uuid4()), "out", "bot", reply_text)

        # ✅ ENVIA VIA REST API
        success = send_whatsapp_text(frm, reply_text)

        if success:
            logging.info(f"✅ Resposta enviada com sucesso para {conversation_id}")
        else:
            logging.error(f"❌ Falha ao enviar resposta para {conversation_id}")

    except Exception as e:
        logging.error(f"❌ Erro no processamento async: {e}", exc_info=True)



# ================== ROTAS ==================

@app.get("/abacaxi")
def abacaxi():
    return {"status": "ok", "ts": str(datetime.now(timezone.utc))}


@app.get("/healthz")
def healthz():
    return "ok", 200


@app.post("/twiml-test")
def twiml_test():
    return _twiml_empty(status=200)


@app.post("/webhook")
def webhook():
    """
    Webhook principal do Twilio.
    
    ESTRATÉGIA: Acknowledge First, Process Async
    1. Valida a requisição
    2. Extrai dados e salva mensagem inbound
    3. Responde IMEDIATAMENTE ao Twilio (TwiML vazio)
    4. Processa em background (thread separada)
    5. Envia resposta via REST API
    
    Isso ELIMINA o problema de timeout.
    """
    # Validação rápida
    if not _is_valid_twilio_request(request):
        logging.warning("Twilio signature inválida para URL %s", request.url)
        return Response("Invalid signature", status=403)

    # Extrai dados rapidamente
    frm   = request.form.get("From", "")
    to    = request.form.get("To", "")
    body  = request.form.get("Body", "") or ""
    sid   = request.form.get("MessageSid")
    
    # Detecta mídia
    num_media = int(request.form.get("NumMedia", 0))
    media_url = None
    media_type = None
    
    if num_media > 0:
        media_url = request.form.get("MediaUrl0")
        media_type = request.form.get("MediaContentType0")
        
        if media_type and media_type.startswith("audio/") and not body.strip():
            body = "🎤 Áudio"
        elif media_type and media_type.startswith("image/") and not body.strip():
            body = "🖼️ Imagem"
        elif media_type and media_type.startswith("video/") and not body.strip():
            body = "🎥 Vídeo"
        elif media_type and not body.strip():
            body = "📄 Documento"

    conversation_id = _conversation_id_e164(frm)
    session_id = _session_id_from_from_field(frm)

    logging.info("📥 Inbound: from=%s sid=%s body=%r", frm, sid, body[:50] if body else "")
    log_event("inbound", conversation_id=conversation_id, from_=frm, to=to, twilio_sid=sid, text=body, media_type=media_type)

    # Operações rápidas no Firestore (necessárias antes de responder)
    conv_snap, existed = _ensure_conversation(conversation_id, session_id)
    conv_data = conv_snap.to_dict()
    
    # Registra inbound
    _add_message_if_new(conversation_id, sid, "in", "user", body, media_url=media_url, media_type=media_type)
    _update_conversation(conversation_id, last_message_text=body, last_in_from="user")

    # ✅ RESPONDE IMEDIATAMENTE AO TWILIO (evita timeout)
    # O processamento continua em background
    
    # Inicia processamento em thread separada
    thread = threading.Thread(
        target=_process_message_async,
        args=(frm, body, sid, conversation_id, session_id, media_url, media_type, conv_data),
        daemon=True
    )
    thread.start()
    
    # Retorna TwiML vazio imediatamente (< 1 segundo)
    logging.info("⚡ Respondendo ao Twilio imediatamente (processamento async iniciado)")
    return _twiml_empty(status=200)


@app.get("/")
def root():
    return "Webh Flask ativo (v2 - async). Use /abacaxi, /healthz e /webhook (POST do Twilio)."


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
