"""Linked phones internal logic (WhatsApp ↔ Cognito user)."""
from datetime import datetime

from boto3.dynamodb.conditions import Attr

from api.deps import user_phones_table


def link_phone(uid: str, phone_number: str, email: str, name: str) -> dict:
    """Link a WhatsApp number to the authenticated user."""
    phone = phone_number.strip()
    if not phone.startswith('+'):
        phone = '+' + phone

    user_phones_table.put_item(Item={
        'phoneNumber': phone,
        'userId': uid,
        'email': email,
        'name': name,
        'linkedAt': datetime.utcnow().isoformat()
    })
    return {"success": True, "phoneNumber": phone, "userId": uid}


def get_user_phone(uid: str) -> dict:
    """Get the user's linked phone."""
    result = user_phones_table.scan(
        FilterExpression=Attr('userId').eq(uid)
    )
    items = result.get('Items', [])
    if items:
        return {"phoneNumber": items[0]['phoneNumber'], "linked": True}
    return {"phoneNumber": "", "linked": False}


def get_user_phones(uid: str) -> dict:
    """Get all of the user's linked WhatsApp phones."""
    result = user_phones_table.scan(
        FilterExpression=Attr('userId').eq(uid)
    )
    items = result.get('Items', [])
    phones = [
        {
            'phoneNumber': item['phoneNumber'],
            'name': item.get('name', ''),
            'email': item.get('email', ''),
            'linkedAt': item.get('linkedAt', '')
        }
        for item in items
    ]
    return {"success": True, "phones": phones}


def unlink_phone(uid: str) -> dict:
    """Unlink the user's phone."""
    result = user_phones_table.scan(
        FilterExpression=Attr('userId').eq(uid)
    )
    for item in result.get('Items', []):
        user_phones_table.delete_item(Key={'phoneNumber': item['phoneNumber']})
    return {"success": True}


def lookup_user_by_phone(phone_number: str) -> dict:
    """Look up which user has this number linked."""
    try:
        # Normalize
        phone = phone_number.strip()
        if not phone.startswith('+'):
            phone = '+' + phone

        result = user_phones_table.get_item(Key={'phoneNumber': phone})
        item = result.get('Item')
        if item:
            return {
                'userId': item['userId'],
                'email': item.get('email', ''),
                'name': item.get('name', '')
            }
        return {}
    except Exception:
        return {}


def auto_link_phone(phone: str, user_id: str, email: str, name: str) -> bool:
    """Link a WhatsApp number to a Cognito user (if it was not already linked)."""
    try:
        phone_clean = phone if phone.startswith('+') else '+' + phone
        existing = user_phones_table.get_item(Key={'phoneNumber': phone_clean}).get('Item')
        if existing:
            return False  # Already linked
        user_phones_table.put_item(Item={
            'phoneNumber': phone_clean,
            'userId': user_id,
            'email': email,
            'name': name,
            'linkedAt': datetime.utcnow().isoformat(),
            'linkedVia': 'whatsapp_wizard'
        })
        print(f"[auto_link_phone] {phone_clean} → {user_id}")
        return True
    except Exception as e:
        print(f"[auto_link_phone] Error: {e}")
        return False
