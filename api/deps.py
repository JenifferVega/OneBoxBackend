"""Recursos compartidos de la API: tablas DynamoDB propias y helpers de auth/scan.

Las tablas del agente (projects, tasks, insights, etc.) viven en agent.tools;
aquí solo se definen las tablas que usa exclusivamente la capa HTTP.
"""
import os

import boto3
from fastapi import HTTPException

dynamodb = boto3.resource('dynamodb', region_name=os.environ.get('AWS_REGION', 'us-east-1'))

attachments_table = dynamodb.Table('onebox-attachments')
user_phones_table = dynamodb.Table('onebox-user-phones')
user_tokens_table = dynamodb.Table('onebox-user-tokens')
sessions_table = dynamodb.Table('onebox-whatsapp-sessions')


def require_uid(uid_value: str) -> str:
    """Devuelve el userId del request o lanza 401 si falta.
    NUNCA cae en un USER_ID por defecto: ese fallback filtraba datos de una
    cuenta real a cualquiera que llamara sin identificarse (fuga entre usuarios)."""
    if not uid_value:
        raise HTTPException(status_code=401, detail="x-user-id requerido")
    return uid_value


def scan_all_pages(table, **scan_kwargs):
    """Realiza un Scan paginado completo en una tabla DynamoDB.
    Necesario porque scan() devuelve máximo 1 MB de items y aplica el FilterExpression
    DESPUÉS de leer; sin paginar, items que coinciden con el filtro pueden quedar
    invisibles si están en páginas posteriores. Devuelve la lista completa de Items."""
    items = []
    last_key = None
    while True:
        kwargs = dict(scan_kwargs)
        if last_key:
            kwargs['ExclusiveStartKey'] = last_key
        res = table.scan(**kwargs)
        items.extend(res.get('Items', []))
        last_key = res.get('LastEvaluatedKey')
        if not last_key:
            break
    return items
