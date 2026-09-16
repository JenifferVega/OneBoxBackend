"""
Cognito Pre-SignUp trigger
==========================
Prevents duplicate accounts when the same email is registered through multiple
paths (Google OAuth, native email+password, etc.).

Cases handled:
- PreSignUp_ExternalProvider (first-time Google login):
    1) Auto-confirms the user (the email was already verified by Google).
    2) If a NATIVE (COGNITO) user already exists with the same email, link
       the external identity to the native user via AdminLinkProviderForUser.
       From then on, Google login and native login resolve to the SAME
       `sub` and therefore to the SAME data in our backend.

- PreSignUp_SignUp (native email+pwd registration):
    If an EXTERNAL_PROVIDER user (Google, etc.) already exists with the same
    email, we REJECT the signup. The user will see a message asking them to
    use the method they originally registered with (Google). This way we
    also avoid creating duplicates in this direction.

Permissions required on the Lambda role:
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
        # 1) Auto-confirm (Google already verified the email).
        event["response"]["autoConfirmUser"] = True
        event["response"]["autoVerifyEmail"] = True

        # 2) Link to an existing native user with the same email (if any).
        if email and "_" in new_username:
            try:
                resp = cognito.list_users(
                    UserPoolId=pool_id,
                    Filter=f'email = "{email}"',
                    Limit=20,
                )
                for user in resp.get("Users", []):
                    # We only care about native users (not another EXTERNAL_PROVIDER).
                    if user.get("UserStatus") == "EXTERNAL_PROVIDER":
                        continue
                    provider_name, provider_user_id = new_username.split("_", 1)
                    print(f"[PreSignUp] Linking {provider_name}/{provider_user_id} -> native {user['Username']}")
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
                    print("[PreSignUp] Link OK")
                    break
            except Exception as e:
                # We do not abort the signup; the external user is created even if not linked.
                print(f"[PreSignUp] Error linking: {e}")

    elif trigger == "PreSignUp_SignUp":
        # Native registration (email + password). If an external user (Google,
        # etc.) with the same email already exists, we reject it to avoid duplicates.
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
                # If the query fails, better to allow than to block.
                print(f"[PreSignUp] Could not verify prior existence: {e}")

            if existing_external:
                print(f"[PreSignUp] Rejecting native signup: Google account already exists for {email}")
                # Cognito returns this message to the client as UserLambdaValidationException
                raise Exception(
                    "This account already exists with Google. Please sign in with Google."
                )

    return event
