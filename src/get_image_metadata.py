"""
Lambda: get_image_metadata
Devuelve los metadatos de una imagen (GET /metadata/{image_id}) desde DynamoDB,
junto con una URL prefirmada temporal para descargarla desde el bucket privado.

Variable de entorno requerida:
  TABLE_NAME -> nombre de la tabla DynamoDB (ImageMetadata)

Handler en la consola: lambda_function.get_image_metadata
(también funciona con el handler por defecto lambda_function.lambda_handler)
"""
import json
import logging
import os
import re
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("TABLE_NAME")

table = boto3.resource("dynamodb").Table(TABLE_NAME) if TABLE_NAME else None
s3_client = boto3.client("s3")

IMAGE_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")   # Formato generado por upload_image
PRESIGNED_URL_TTL = 3600                           # 1 hora

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token",
    "Access-Control-Allow-Methods": "OPTIONS,POST,GET",
}


def to_json_safe(obj):
    """DynamoDB devuelve números como Decimal, que json.dumps no serializa."""
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    raise TypeError(f"Tipo no serializable: {type(obj)}")


def build_response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json", **CORS_HEADERS},
        "body": json.dumps(body, default=to_json_safe, ensure_ascii=False),
    }


def get_image_metadata(event, context):
    if not TABLE_NAME:
        logger.error("Falta la variable de entorno TABLE_NAME")
        return build_response(500, {"error": "Configuración incompleta: revisar variables de entorno."})

    # pathParameters llega como None cuando no hay parámetros de ruta
    image_id = (event.get("pathParameters") or {}).get("image_id")
    logger.info("Consulta de metadatos. image_id=%s", image_id)

    if not image_id:
        return build_response(400, {"message": "Image ID is required"})
    if not IMAGE_ID_PATTERN.match(image_id):
        return build_response(400, {"message": "Formato de image_id inválido (se esperan 32 caracteres hexadecimales)."})

    try:
        response = table.get_item(Key={"image_id": image_id})
    except ClientError as e:
        code = e.response["Error"]["Code"]
        logger.exception("Error de DynamoDB (%s)", code)
        return build_response(502, {"error": f"Error al consultar DynamoDB: {code}"})

    item = response.get("Item")
    if not item:
        logger.info("image_id no encontrado: %s", image_id)
        return build_response(404, {"message": "Image not found"})

    # El bucket es privado: se entrega una URL temporal firmada para ver la imagen.
    try:
        item["download_url"] = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": item["bucket"], "Key": item["s3_key"]},
            ExpiresIn=PRESIGNED_URL_TTL,
        )
        item["download_url_expires_in"] = PRESIGNED_URL_TTL
    except (ClientError, KeyError):
        logger.warning("No se pudo generar la URL prefirmada para %s", image_id)

    return build_response(200, item)


# Alias para que también funcione con el handler por defecto de la consola.
lambda_handler = get_image_metadata
