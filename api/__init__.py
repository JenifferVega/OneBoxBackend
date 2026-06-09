"""Paquete de la API HTTP de OneBox (FastAPI).

Estructura:
  - api.app        → fábrica de la aplicación FastAPI (create_app)
  - api.deps       → recursos compartidos (DynamoDB, auth, paginación)
  - api.schemas    → modelos Pydantic de request/response
  - api.controllers  → routers (controladores delgados)
  - api.services   → lógica interna de cada dominio

Variables de entorno requeridas (además de las credenciales AWS):
  - COGNITO_USER_POOL_ID  → pool de usuarios para invitaciones por email
  - GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET → OAuth de Gmail
  - GOOGLE_REDIRECT_URI   → callback OAuth (/api/gmail/callback público)
  - GOOGLE_SCOPES         → scopes OAuth separados por espacio (tiene default)
  - GOOGLE_CLOUD_PROJECT  → proyecto GCP para Pub/Sub de Gmail watch
  - TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_WHATSAPP_NUMBER → WhatsApp
"""
