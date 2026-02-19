import os


def _bool_env(name, default="false"):
    return os.getenv(name, default).lower() == "true"


def _csv_casefold(name, default=""):
    return [
        s.strip().casefold()
        for s in os.getenv(name, default).split(",")
        if s.strip()
    ]


def _float_env(name, default="0"):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _int_env(name, default="0"):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


class BaseConfig:
    ENV_NAME = "base"
    JSON_AS_ASCII = False

    AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()

    TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    TWILIO_AUTH_TOKEN_REST = os.getenv("TWILIO_AUTH_TOKEN_REST", "").strip() or AUTH_TOKEN
    TWILIO_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()
    TWILIO_POST_RETRY_ATTEMPTS = _int_env("TWILIO_POST_RETRY_ATTEMPTS", "2")
    TWILIO_POST_RETRY_BACKOFF_SECONDS = _float_env("TWILIO_POST_RETRY_BACKOFF_SECONDS", "0.3")

    DF_PROJECT = os.getenv("DF_PROJECT_ID", "").strip()
    DF_LOCATION = os.getenv("DF_LOCATION", "global").strip()
    DF_AGENT_ID = os.getenv("DF_AGENT_ID", "").strip()
    LANG_CODE = os.getenv("DF_LANG_CODE", "pt-br").strip()

    DF_HANDOFF_PARAM = (os.getenv("DF_HANDOFF_PARAM", "handoff_request") or "").strip()
    DF_HANDOFF_MARKER = os.getenv("DF_HANDOFF_MARKER", "##HANDOFF_TRIGGER##")
    FEATURE_AUTOREPLY_DURING_PENDING = _bool_env("FEATURE_AUTOREPLY_DURING_PENDING", "false")
    HANDOFF_ACK_TEXT = os.getenv(
        "HANDOFF_ACK_TEXT",
        "Certo! Um atendente vai assumir esta conversa em instantes."
    )
    FEATURE_DISABLE_HANDOFF = _bool_env("FEATURE_DISABLE_HANDOFF", "false")
    HANDOFF_DISABLED_TEXT = os.getenv(
        "HANDOFF_DISABLED_TEXT",
        "Atendimento humano temporariamente indisponível. "
        "Você pode deixar sua mensagem por aqui e responderemos assim que possível."
    ).strip()
    FEATURE_FORCE_BOT_WHEN_HANDOFF_DISABLED = _bool_env(
        "FEATURE_FORCE_BOT_WHEN_HANDOFF_DISABLED",
        "true"
    )

    DF_HANDOFF_TEXT_HINTS = _csv_casefold(
        "DF_HANDOFF_TEXT_HINTS",
        "transferindo você agora para um de nossos atendentes,atendente continuará seu atendimento em instantes"
    )

    FS_CONV_COLL = os.getenv("FS_CONV_COLL", "conversations").strip()
    FS_MSG_SUBCOLL = os.getenv("FS_MSG_SUBCOLL", "messages").strip()

    MESSAGE_DEBOUNCE_INITIAL_SECONDS = _float_env("MESSAGE_DEBOUNCE_INITIAL_SECONDS", "5.0")
    MESSAGE_DEBOUNCE_EXTEND_SECONDS = _float_env("MESSAGE_DEBOUNCE_EXTEND_SECONDS", "3.0")
    MESSAGE_DEBOUNCE_MAX_SECONDS = _float_env("MESSAGE_DEBOUNCE_MAX_SECONDS", "10.0")
    FEATURE_MESSAGE_AGGREGATION = _bool_env("FEATURE_MESSAGE_AGGREGATION", "true")

    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


class DevelopmentConfig(BaseConfig):
    ENV_NAME = "development"
    DEBUG = True


class StagingConfig(BaseConfig):
    ENV_NAME = "staging"
    DEBUG = False


class ProductionConfig(BaseConfig):
    ENV_NAME = "production"
    DEBUG = False


class TestingConfig(BaseConfig):
    ENV_NAME = "testing"
    TESTING = True


def get_config():
    env = (os.getenv("APP_ENV") or os.getenv("FLASK_ENV") or "production").strip().lower()
    if env in ("development", "dev", "local"):
        return DevelopmentConfig
    if env in ("staging", "stage"):
        return StagingConfig
    if env in ("testing", "test"):
        return TestingConfig
    return ProductionConfig
