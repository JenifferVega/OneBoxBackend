"""OneBox HTTP API package (FastAPI).

Structure:
  - api.app        → FastAPI application factory (create_app)
  - api.deps       → shared resources (DynamoDB, auth, pagination)
  - api.schemas    → Pydantic request/response models
  - api.controllers → routers (thin controllers)
  - api.services   → per-domain internal logic

Required environment variables (in addition to AWS credentials):
  - COGNITO_USER_POOL_ID  → user pool for email invitations
  - GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET → Gmail OAuth
  - GOOGLE_REDIRECT_URI   → OAuth callback (/api/gmail/callback public)
  - GOOGLE_SCOPES         → space-separated OAuth scopes (has default)
  - GOOGLE_CLOUD_PROJECT  → GCP project for Gmail watch Pub/Sub
  - TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_WHATSAPP_NUMBER → WhatsApp
"""
