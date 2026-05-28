"""
Cognito Pre-SignUp trigger
==========================
Evita cuentas duplicadas cuando un mismo email se registra por múltiples vías
(Google OAuth, email+contraseña nativo, etc.).

Casos manejados:
- PreSignUp_ExternalProvider (login con Google por primera vez):
    1) Auto-confirma al usuario (el email ya fue verificado por Google).
    2) Si ya existe un usuario NATIVO (COGNITO) con el mismo email, enlaza
       la identidad externa al usuario nativo via AdminLinkProviderForUser.
       A partir de ahí, login con Google y login nativo resuelven al MISMO
       `sub` y por tanto a los MISMOS datos en nuestro backend.

- PreSignUp_SignUp (registro nativo con email+pwd):
    Si ya existe un usuario EXTERNAL_PROVIDER (Google, etc.) con el mismo
    email, RECHAZAMOS el signup. El usuario verá un mensaje pidiendo que
    use el método con el que se registró originalmente (Google). Así
    evitamos crear duplicados en este sentido también.

Permisos necesarios en el rol del Lambda:
- cognito-idp:ListUsers
- cognito-idp:AdminLinkProviderForUser
"""
import boto3

cognito = boto3.client("cognito-idp")


def lambda_handler(event, context):
    trigger = event.get("triggerSource", "")
    request = event.get("request", {}) or {}
    attrs = request.get("userAttributes", {}) or {}
    email = (attrs.get("email") or "").lower().strip()
    pool_id = event["userPoolId"]
    new_username = event.get("userName", "") or ""

    print(f"[PreSignUp] trigger={trigger} email={email} userName={new_username}")

    if trigger == "PreSignUp_ExternalProvider":
        # 1) Auto-confirmar (Google ya verificó el email).
        event["response"]["autoConfirmUser"] = True
        event["response"]["autoVerifyEmail"] = True

        # 2) Enlazar a un usuario nativo existente con el mismo email (si lo hay).
        if email and "_" in new_username:
            try:
                resp = cognito.list_users(
                    UserPoolId=pool_id,
                    Filter=f'email = "{email}"',
                    Limit=20,
                )
                for user in resp.get("Users", []):
                    # Solo nos interesan los nativos (no otro EXTERNAL_PROVIDER).
                    if user.get("UserStatus") == "EXTERNAL_PROVIDER":
                        continue
                    provider_name, provider_user_id = new_username.split("_", 1)
                    print(f"[PreSignUp] Enlazando {provider_name}/{provider_user_id} -> nativo {user['Username']}")
                    cognito.admin_link_provider_for_user(
                        UserPoolId=pool_id,
                        DestinationUser={
                            "ProviderName": "Cognito",
                            "ProviderAttributeValue": user["Username"],
                        },
                        SourceUser={
                            "ProviderName": provider_name,
                            "ProviderAttributeName": "Cognito_Subject",
                            "ProviderAttributeValue": provider_user_id,
                        },
                    )
                    print("[PreSignUp] Enlace OK")
                    break
            except Exception as e:
                # No abortamos el signup; el usuario externo se crea aunque no se enlace.
                print(f"[PreSignUp] Error enlazando: {e}")

    elif trigger == "PreSignUp_SignUp":
        # Registro nativo (email + contraseña). Si ya existe un usuario externo
        # (Google, etc.) con el mismo email, rechazamos para evitar duplicados.
        if email:
            existing_external = False
            try:
                resp = cognito.list_users(
                    UserPoolId=pool_id,
                    Filter=f'email = "{email}"',
                    Limit=20,
                )
                for user in resp.get("Users", []):
                    if user.get("UserStatus") == "EXTERNAL_PROVIDER":
                        existing_external = True
                        break
            except Exception as e:
                # Si fallamos en la consulta, mejor permitir que bloquear.
                print(f"[PreSignUp] No se pudo verificar existencia previa: {e}")

            if existing_external:
                print(f"[PreSignUp] Rechazando signup nativo: ya existe Google para {email}")
                # Cognito devuelve este mensaje al cliente como UserLambdaValidationException
                raise Exception(
                    "Esta cuenta ya existe con Google. Inicia sesión con Google."
                )

    return event
