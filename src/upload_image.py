"""
Lambda: upload_image
Recibe una imagen JPG desde API Gateway (POST /upload), la guarda en S3
y registra sus metadatos en DynamoDB.

Variables de entorno requeridas:
  BUCKET_NAME -> nombre del bucket S3 (my-image-storage-bucket-...)
  TABLE_NAME  -> nombre de la tabla DynamoDB (ImageMetadata)

Handler en la consola: lambda_function.upload_image
(también funciona con el handler por defecto lambda_function.lambda_handler)
"""
import base64
import json
import logging
import os
import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Se leen una sola vez por contenedor (fuera del handler) para reutilizarlos
# entre invocaciones "calientes" y reducir la latencia.
BUCKET_NAME = os.environ.get("BUCKET_NAME")
TABLE_NAME = os.environ.get("TABLE_NAME")

s3_client = boto3.client("s3")
table = boto3.resource("dynamodb").Table(TABLE_NAME) if TABLE_NAME else None

JPEG_MAGIC = b"\xff\xd8\xff"          # Firma binaria de todo archivo JPEG
MAX_SIZE_BYTES = 5 * 1024 * 1024      # 5 MB (límite de payload de Lambda: 6 MB)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token",
    "Access-Control-Allow-Methods": "OPTIONS,POST,GET",
}


def build_response(status_code, body):
    """Respuesta en el formato que exige la integración Lambda Proxy."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json", **CORS_HEADERS},
        "body": json.dumps(body, ensure_ascii=False),
    }


def extract_image_bytes(event):
    """Obtiene los bytes de la imagen desde el evento de API Gateway.

    - Si API Gateway tiene configurado 'image/jpeg' como Binary Media Type,
      el cuerpo llega en base64 con isBase64Encoded = true.
    - Como alternativa se acepta un JSON {"image_base64": "..."}.
    """
    body = event.get("body")
    if not body:
        raise ValueError("No se recibió ninguna imagen en el cuerpo de la solicitud.")

    if event.get("isBase64Encoded"):
        return base64.b64decode(body)

    try:
        payload = json.loads(body)
        if isinstance(payload, dict) and payload.get("image_base64"):
            return base64.b64decode(payload["image_base64"])
    except (json.JSONDecodeError, ValueError):
        pass

    # Cuerpo de texto plano: si era un binario, API Gateway lo habrá alterado
    # y la validación JPEG posterior lo detectará.
    return body.encode("utf-8")


def upload_image(event, context):
    request_id = getattr(context, "aws_request_id", "local")
    logger.info("Solicitud recibida. request_id=%s", request_id)

    if not BUCKET_NAME or not TABLE_NAME:
        logger.error("Faltan variables de entorno BUCKET_NAME y/o TABLE_NAME")
        return build_response(500, {"error": "Configuración incompleta: revisar variables de entorno."})

    try:
        image_bytes = extract_image_bytes(event)
    except ValueError as e:
        logger.warning("Solicitud inválida: %s", e)
        return build_response(400, {"error": str(e)})

    if not image_bytes.startswith(JPEG_MAGIC):
        logger.warning("El archivo recibido no es un JPEG válido (primeros bytes: %s)", image_bytes[:4].hex())
        return build_response(415, {
            "error": "El archivo no es un JPG válido. Verifique Content-Type: image/jpeg y que "
                     "'image/jpeg' esté en Binary Media Types de API Gateway (y que la API se haya redesplegado)."
        })

    size_bytes = len(image_bytes)
    if size_bytes > MAX_SIZE_BYTES:
        return build_response(413, {"error": f"La imagen supera el máximo de {MAX_SIZE_BYTES} bytes."})

    image_id = uuid.uuid4().hex                  # 32 caracteres hexadecimales, sin guiones
    s3_key = f"images/{image_id}.jpg"
    uploaded_at = datetime.now(timezone.utc).isoformat()

    try:
        s3_client.put_object(
            Bucket=BUCKET_NAME,
            Key=s3_key,
            Body=image_bytes,
            ContentType="image/jpeg",
        )
        logger.info("Imagen guardada en s3://%s/%s (%d bytes)", BUCKET_NAME, s3_key, size_bytes)

        item = {
            "image_id": image_id,
            "bucket": BUCKET_NAME,
            "s3_key": s3_key,
            "s3_uri": f"s3://{BUCKET_NAME}/{s3_key}",
            "url": f"https://{BUCKET_NAME}.s3.amazonaws.com/{s3_key}",
            "content_type": "image/jpeg",
            "size_bytes": size_bytes,
            "uploaded_at": uploaded_at,
            "request_id": request_id,
        }
        table.put_item(Item=item)
        logger.info("Metadatos registrados en DynamoDB. image_id=%s", image_id)

    except ClientError as e:
        code = e.response["Error"]["Code"]
        logger.exception("Error de AWS (%s) al procesar la imagen", code)
        return build_response(502, {"error": f"Error al comunicarse con AWS: {code}"})
    except Exception:
        logger.exception("Error inesperado")
        return build_response(500, {"error": "Error interno al procesar la imagen."})

    return build_response(201, {
        "message": "Image uploaded successfully!",
        "image_id": image_id,
        "url": item["url"],
        "size_bytes": size_bytes,
        "uploaded_at": uploaded_at,
    })


# Alias para que también funcione con el handler por defecto de la consola.
lambda_handler = upload_image
