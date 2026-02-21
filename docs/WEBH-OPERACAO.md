# WEBH - Manual de Operacao e Administracao

Ultima atualizacao: 2026-02-20

Este documento descreve como o servico webh esta organizado, como configurar ambiente, e como fazer deploy em staging.

## Visao Geral

O webh e o webhook do Twilio (WhatsApp) que integra com Dialogflow CX e persiste conversas no Firestore. O fluxo principal:

1. Recebe o POST do Twilio em `/webhook`.
2. Valida assinatura (se `TWILIO_AUTH_TOKEN` estiver configurado).
3. Extrai dados da mensagem e salva inbound no Firestore (idempotente).
4. Responde imediatamente com TwiML vazio (evita timeout).
5. (Opcional) Agrega multiplas mensagens e processa em lote.
6. Processa async, chama Dialogflow CX, e envia resposta via Twilio REST.

## Estrutura do Repo

```
webh/
  webh.py                    # entrypoint (gunicorn webh:app)
  run.py                     # entrypoint local (python run.py)
  Procfile                   # comando gunicorn
  app/
    __init__.py              # create_app()
    config.py                # env vars e defaults
    core/
      logging.py             # logging estruturado
    extensions.py            # Firestore, Dialogflow CX, HTTP session
    repositories/
      firestore_repo.py      # persistencia conv/messages
    services/
      webhook_service.py     # logica principal do webhook
      cx_service.py          # Dialogflow CX + retry
      twilio_service.py      # envio WhatsApp via REST
    blueprints/
      health/                # /, /healthz, /abacaxi
      webhook/               # /webhook, /twiml-test, /debug/buffers
```

## Endpoints

- `POST /webhook` (Twilio inbound)
- `POST /twiml-test` (retorna TwiML vazio)
- `GET /debug/buffers` (debug de agregacao)
- `GET /healthz`
- `GET /abacaxi`
- `GET /` (status)

## Idempotencia

Inbound:
- Usa `MessageSid` ou `I-Twilio-Idempotency-Token` para gerar `inbound_id`.
- Se a mensagem ja existe no Firestore, o webhook retorna sem reprocessar.

Outbound:
- Resposta do bot usa `out_msg_id = "bot:<inbound_id>"`.
- Se a resposta ja existe, nao envia novamente.

## Message Aggregation (Debounce)

Variaveis:
- `FEATURE_MESSAGE_AGGREGATION` (true/false)
- `MESSAGE_DEBOUNCE_INITIAL_SECONDS` (default 5.0)
- `MESSAGE_DEBOUNCE_EXTEND_SECONDS` (default 3.0)
- `MESSAGE_DEBOUNCE_MAX_SECONDS` (default 10.0)

Endpoint de debug:
- `GET /debug/buffers`

## Dialogflow CX

Variaveis:
- `DF_PROJECT_ID`
- `DF_LOCATION`
- `DF_AGENT_ID`
- `DF_LANG_CODE`
- `CX_TIMEOUT_SECONDS` (default: `15.0`)
- `CX_RETRY_ATTEMPTS` (default: `3`)

Retry:
- `detect_intent_text` tenta ate `CX_RETRY_ATTEMPTS` vezes por erros transitorios (500/503/timeout).
- Backoff exponencial com jitter.

### Handoff (regra atual)
- Deteccao de handoff considera:
  - frases de `DF_HANDOFF_TEXT_HINTS` por comparacao exata (apos normalizacao de espacos e `casefold`).
  - parametro de sessao configurado em `DF_HANDOFF_PARAM` com valor truthy (producao: `handoff_request=true`).
- Quando handoff e detectado com handoff habilitado:
  - conversa vai para `pending_handoff`.
  - resposta enviada ao usuario prioriza o texto retornado pelo CX.
  - `HANDOFF_ACK_TEXT` e fallback somente se o CX nao retornar texto.
- Quando handoff esta desabilitado (`FEATURE_DISABLE_HANDOFF=true`):
  - prioridade de resposta: `HANDOFF_DISABLED_TEXT` -> texto do CX -> `HANDOFF_ACK_TEXT`.
- Se CX falhar apos retries:
  - fallback enviado: `Tivemos um problema de estabilidade, pode repetir sua pergunta?`
- Se o CX responder sem texto (sem excecao):
  - fallback enviado: `Nao consegui gerar uma resposta agora, pode repetir sua pergunta?`

## Firestore

Colecoes:
- `FS_CONV_COLL` (default: `conversations`)
- `FS_MSG_SUBCOLL` (default: `messages`)

Campos adicionais em `conversations`:
- `wa_profile_name` (ProfileName do WhatsApp)


## Twilio / WhatsApp

Obrigatorias:
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN_REST`
- `TWILIO_AUTH_TOKEN` (assinatura do webhook)
- `TWILIO_WHATSAPP_FROM`



## ProfileName (WhatsApp)

- O campo `ProfileName` do webhook Twilio e salvo em `conversations.wa_profile_name`.
- Esse valor e usado pelo CRM para exibir o nome do perfil do WhatsApp.
- Nao substitui o nome declarado pelo cliente no Dialogflow CX.

## Variaveis de Ambiente Importantes

Principais (alem das obrigatorias):
- `CX_TIMEOUT_SECONDS` (tempo maximo por chamada `detect_intent`, em segundos)
- `CX_RETRY_ATTEMPTS` (numero de tentativas para erros transitorios do CX)
- `DF_HANDOFF_PARAM` (valor esperado: `handoff_request`)
- `DF_HANDOFF_TEXT_HINTS` (frases separadas por `||` ou quebra de linha; comparacao exata)
- `HANDOFF_ACK_TEXT` (fallback para handoff sem texto vindo do CX)
- `HANDOFF_DISABLED_TEXT`
- `FEATURE_DISABLE_HANDOFF`
- `FEATURE_FORCE_BOT_WHEN_HANDOFF_DISABLED`
- `TWILIO_POST_RETRY_ATTEMPTS` (default: 2)
- `TWILIO_POST_RETRY_BACKOFF_SECONDS` (default: 0.3)

Observacao importante sobre `DF_HANDOFF_TEXT_HINTS`:
- Nao use fragmentos curtos de frase (ex.: apenas `por favor`), pois o objetivo e bater com texto completo.
- Use frases completas separadas por `||` (ou quebra de linha):
```powershell
gcloud run services update webh `
  --project=val-02-469714 `
  --region=southamerica-east1 `
  --update-env-vars "^@^DF_HANDOFF_TEXT_HINTS=frase completa 1||frase completa 2"
```

Use `env.staging.example.yaml` como referencia de staging e mantenha `env.staging.yaml` apenas local (nao versionado).

## Deploy (Cloud Run)

1) Entre na pasta:
```powershell
cd webh
```

2) Gere o arquivo local de env (uma vez, depois ajuste valores):
```powershell
Copy-Item env.staging.example.yaml env.staging.yaml
```

3) Deploy (exemplo):
```powershell
gcloud run deploy webh-staging `
  --source . `
  --project=val-02-469714 `
  --region=southamerica-east1 `
  --allow-unauthenticated `
  --env-vars-file env.staging.yaml
```

Observacoes:
- Ajuste service name, project e region se necessario.
- Se nao usar `--env-vars-file`, configure as env vars manualmente no Cloud Run.

## Operacao no Dia-a-dia

- Verificar status: `GET /healthz`
- Validar buffers de agregacao: `GET /debug/buffers`
- Teste rapido do webhook: `POST /twiml-test`

## Troubleshooting Rapido

1) Nao responde ao Twilio:
- Verifique `TWILIO_AUTH_TOKEN` e assinatura.
- Confira logs do Cloud Run.

2) Resposta duplicada:
- Verifique se `inbound_id` e `out_msg_id` estao sendo logados corretamente.

3) CX falhando:
- Verifique credenciais GCP e `DF_*`.
- Verifique logs de retry/transitorio.

## Logs (Cloud Run)

```powershell
gcloud logging read "resource.labels.service_name=webh-staging" --limit=50
```

## Testes Rapidos (Staging)

### 1) Idempotencia inbound (curl)
Envie duas vezes o mesmo payload com o mesmo `MessageSid`. A segunda deve ser ignorada.

```powershell
$url = "https://webh-staging-110818688721.southamerica-east1.run.app/webhook"
$body = @{
  From = "whatsapp:+553183440484"
  To = "whatsapp:+14155238886"
  Body = "teste idempotencia"
  MessageSid = "SM_TESTE_001"
}

Invoke-WebRequest -Method Post -Uri $url -Body $body | Out-Null
Invoke-WebRequest -Method Post -Uri $url -Body $body | Out-Null
```

Logs esperados:
- Primeiro: `Inbound: ...`
- Segundo: `Webhook duplicado (inbound ja existe)...`

### 2) Ver logs do registro (Cloud Run)

Ultimos registros (1h):
```powershell
gcloud logging read `
  "resource.type=cloud_run_revision AND resource.labels.service_name=webh-staging" `
  --project=val-02-469714 `
  --freshness=1h `
  --limit=200 `
  --format="value(textPayload)"
```

Filtrar apenas registros de envio REST e idempotencia:
```powershell
gcloud logging read `
  "resource.type=cloud_run_revision AND resource.labels.service_name=webh-staging AND (textPayload:Enviado OR textPayload:Resposta OR textPayload:Webhook)" `
  --project=val-02-469714 `
  --freshness=1h `
  --limit=200 `
  --format="value(textPayload)"
```
