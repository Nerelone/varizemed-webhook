import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from google.cloud import firestore
from google.cloud import dialogflowcx_v3 as dfcx
from google.cloud import speech
from google.api_core.client_options import ClientOptions
from werkzeug.middleware.proxy_fix import ProxyFix

http_session = None
fs = None
df_client = None
speech_client = None


def _get_retry_session(retries=3, backoff_factor=0.5, status_forcelist=(500, 502, 503, 504)):
    """Session with retry for transient errors."""
    session = requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def init_extensions(app):
    global http_session, fs, df_client, speech_client

    if http_session is None:
        http_session = _get_retry_session()
    if fs is None:
        fs = firestore.Client()

    df_location = app.config.get("DF_LOCATION", "global")
    endpoint = f"{df_location}-dialogflow.googleapis.com"
    df_client = dfcx.SessionsClient(client_options=ClientOptions(api_endpoint=endpoint))

    if speech_client is None:
        try:
            speech_client = speech.SpeechClient()
        except Exception as exc:
            app.logger.error("Falha ao inicializar SpeechClient: %s", exc, exc_info=True)
            speech_client = None

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def get_http_session():
    return http_session


def get_fs():
    return fs


def get_df_client():
    return df_client


def get_speech_client():
    return speech_client
