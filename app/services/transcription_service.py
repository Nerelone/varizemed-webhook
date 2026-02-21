import logging

import requests
from google.api_core import exceptions as gexc
from google.cloud import speech

_ENCODING_MAP = {
    "audio/ogg": speech.RecognitionConfig.AudioEncoding.OGG_OPUS,
    "audio/ogg; codecs=opus": speech.RecognitionConfig.AudioEncoding.OGG_OPUS,
    "audio/opus": speech.RecognitionConfig.AudioEncoding.OGG_OPUS,
    "application/ogg": speech.RecognitionConfig.AudioEncoding.OGG_OPUS,
    "audio/mpeg": speech.RecognitionConfig.AudioEncoding.MP3,
    "audio/mp3": speech.RecognitionConfig.AudioEncoding.MP3,
    "audio/wav": speech.RecognitionConfig.AudioEncoding.LINEAR16,
    "audio/x-wav": speech.RecognitionConfig.AudioEncoding.LINEAR16,
    "audio/flac": speech.RecognitionConfig.AudioEncoding.FLAC,
    "audio/amr": speech.RecognitionConfig.AudioEncoding.AMR,
    "audio/amr-wb": speech.RecognitionConfig.AudioEncoding.AMR_WB,
}

_SAMPLE_RATE_MAP = {
    speech.RecognitionConfig.AudioEncoding.OGG_OPUS: 48000,
    speech.RecognitionConfig.AudioEncoding.AMR: 8000,
    speech.RecognitionConfig.AudioEncoding.AMR_WB: 16000,
    speech.RecognitionConfig.AudioEncoding.LINEAR16: 16000,
    speech.RecognitionConfig.AudioEncoding.FLAC: 16000,
    speech.RecognitionConfig.AudioEncoding.MP3: 16000,
}

_OPUS_SAMPLE_RATE_CANDIDATES = (16000, 24000, 12000, 48000, 8000)


def is_audio_media_type(media_type: str | None) -> bool:
    normalized = (media_type or "").split(";")[0].strip().lower()
    return normalized.startswith("audio/") or normalized == "application/ogg"


def _resolve_encoding(media_type: str | None):
    if not media_type:
        return speech.RecognitionConfig.AudioEncoding.OGG_OPUS, _SAMPLE_RATE_MAP[speech.RecognitionConfig.AudioEncoding.OGG_OPUS]

    media_type_raw = media_type.strip().lower()
    media_type_normalized = media_type_raw.split(";")[0].strip()

    encoding = _ENCODING_MAP.get(media_type_raw) or _ENCODING_MAP.get(media_type_normalized)
    if not encoding:
        logging.warning("Content-type de audio nao mapeado (%s). Fallback OGG_OPUS.", media_type)
        encoding = speech.RecognitionConfig.AudioEncoding.OGG_OPUS

    return encoding, _SAMPLE_RATE_MAP.get(encoding)


def download_twilio_media(media_url: str, account_sid: str, auth_token: str, *, http_session):
    if not media_url:
        raise ValueError("media_url vazio")
    if not account_sid or not auth_token:
        raise ValueError("credenciais Twilio ausentes")

    session = http_session or requests.Session()

    resp = session.get(
        media_url,
        auth=(account_sid, auth_token),
        timeout=30,
    )
    resp.raise_for_status()

    content = resp.content or b""
    if len(content) < 64:
        raise ValueError(f"conteudo de audio muito pequeno: {len(content)} bytes")

    return content


def _recognize(speech_client, *, config_data: dict, audio_content: bytes, timeout_s: float):
    config = speech.RecognitionConfig(**config_data)
    audio = speech.RecognitionAudio(content=audio_content)
    return speech_client.recognize(config=config, audio=audio, timeout=timeout_s)


def _extract_transcript_text(response):
    if not response or not response.results:
        return ""

    out = []
    for result in response.results:
        if result.alternatives:
            text = (result.alternatives[0].transcript or "").strip()
            if text:
                out.append(text)
    return " ".join(out).strip()


def transcribe_audio(
    speech_client,
    audio_content: bytes,
    *,
    media_type: str | None = None,
    language_code: str = "pt-BR",
    timeout_s: float = 30.0,
):
    if not audio_content:
        return ""

    encoding, sample_rate = _resolve_encoding(media_type)
    config_data = {
        "encoding": encoding,
        "language_code": language_code,
        "enable_automatic_punctuation": True,
        "model": "default",
        "audio_channel_count": 1,
    }

    attempts = []
    if encoding == speech.RecognitionConfig.AudioEncoding.OGG_OPUS:
        for rate in _OPUS_SAMPLE_RATE_CANDIDATES:
            attempts.append({**config_data, "sample_rate_hertz": rate})
    else:
        if sample_rate:
            attempts.append({**config_data, "sample_rate_hertz": sample_rate})
        attempts.append(config_data)

    for idx, cfg in enumerate(attempts, start=1):
        try:
            response = _recognize(
                speech_client,
                config_data=cfg,
                audio_content=audio_content,
                timeout_s=timeout_s,
            )
        except gexc.InvalidArgument:
            logging.warning(
                "STT rejeitou sample_rate_hertz para media_type=%s. Tentando fallback.",
                media_type,
            )
            continue

        text = _extract_transcript_text(response)
        if text:
            if "sample_rate_hertz" in cfg:
                logging.info("STT com sucesso usando sample_rate_hertz=%s", cfg["sample_rate_hertz"])
            return text

        if idx < len(attempts):
            logging.info("STT sem resultados na tentativa %d/%d. Tentando proxima configuracao.", idx, len(attempts))

    return ""


def transcribe_twilio_audio(
    speech_client,
    media_url: str,
    media_type: str | None,
    *,
    settings,
    http_session,
    language_code: str | None = None,
    timeout_s: float | None = None,
):
    account_sid = (settings.get("TWILIO_ACCOUNT_SID") or "").strip()
    auth_token = (settings.get("TWILIO_AUTH_TOKEN_REST") or settings.get("AUTH_TOKEN") or "").strip()
    lang = language_code or settings.get("STT_LANGUAGE_CODE", "pt-BR")
    stt_timeout = timeout_s if timeout_s is not None else float(settings.get("STT_TIMEOUT_SECONDS", 30.0))

    if not account_sid or not auth_token:
        logging.error("Credenciais Twilio ausentes para transcricao de audio")
        return ""

    try:
        audio_content = download_twilio_media(
            media_url,
            account_sid,
            auth_token,
            http_session=http_session,
        )
        return transcribe_audio(
            speech_client,
            audio_content,
            media_type=media_type,
            language_code=lang,
            timeout_s=stt_timeout,
        )
    except Exception as exc:
        logging.error("Falha na transcricao de audio Twilio: %s", exc, exc_info=True)
        return ""
