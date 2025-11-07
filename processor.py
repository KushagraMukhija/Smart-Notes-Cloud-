#!/usr/bin/env python3
"""
processor.py

SQS worker that:
 - polls the SQS queue for S3 ObjectCreated notifications
 - downloads the object from S3 to a temp file
 - extracts text (PDF via PyPDF2; images via pytesseract)
 - stores metadata + extracted_text into DynamoDB

Notes:
 - Make sure the EC2 instance has an IAM role with SQS, S3 and DynamoDB permissions.
 - Requires tesseract-ocr installed on the instance for image OCR.
"""

import boto3
import json
import os
import time
import logging
import tempfile
import sys
import traceback
from urllib.parse import unquote_plus
from pathlib import Path

# Third-party libs (ensure installed in environment)
import PyPDF2
from PIL import Image
import pytesseract

# -----------------------------
# Configuration (env or edit)
# -----------------------------
QUEUE_URL = os.environ.get("QUEUE_URL", "https://sqs.eu-north-1.amazonaws.com/401725075930/notes-to-process-queue")
DYNAMODB_TABLE_NAME = os.environ.get("DDB_TABLE", "Notes")
REGION_NAME = os.environ.get("AWS_REGION", "eu-north-1")
# How many messages to fetch each poll
MAX_MESSAGES = int(os.environ.get("MAX_MESSAGES", "1"))
# Long poll wait
WAIT_TIME = int(os.environ.get("SQS_WAIT_SECONDS", "20"))
# If you want to limit what extensions to process:
ALLOWED_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".bmp"}

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("notes-processor")

# -----------------------------
# AWS clients
# -----------------------------
sqs_client = boto3.client("sqs", region_name=REGION_NAME)
s3_client = boto3.client("s3", region_name=REGION_NAME)
dynamodb = boto3.resource("dynamodb", region_name=REGION_NAME)
table = dynamodb.Table(DYNAMODB_TABLE_NAME)


# -----------------------------
# Helpers
# -----------------------------
def sanitize_s3_key(raw_key: str) -> str:
    """URL-decode and keep only basename to avoid accidental traversal.
       Replace repeated whitespace and limit length."""
    if raw_key is None:
        return raw_key
    key = unquote_plus(raw_key)
    key = Path(key).name  # drop any path components
    # Replace problematic characters with underscore
    key = "".join(ch if ch.isalnum() or ch in ".-_() " else "_" for ch in key)
    # collapse multiple underscores
    while "__" in key:
        key = key.replace("__", "_")
    if len(key) > 240:
        name, ext = os.path.splitext(key)
        key = name[:200] + ext
    return key


def extract_text_from_pdf(path: str) -> str:
    """Extract text from PDF using PyPDF2. Returns empty string if nothing extracted."""
    text = ""
    try:
        with open(path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                try:
                    p_text = page.extract_text()
                    if p_text:
                        text += p_text + "\n"
                except Exception:
                    logger.debug("Failed to extract text from a page (continuing).")
        return text.strip()
    except Exception as e:
        logger.exception("PDF extraction failed: %s", e)
        return ""


def extract_text_from_image(path: str) -> str:
    """Use pytesseract to OCR an image file."""
    try:
        img = Image.open(path)
        text = pytesseract.image_to_string(img)
        return text.strip()
    except Exception as e:
        logger.exception("Image OCR failed: %s", e)
        return ""


def put_to_dynamo(file_key, extracted_text, extra_meta=None):
    """Write item into DynamoDB. Adds uploaded_at timestamp and file_type automatically."""
    try:
        item = {
            "file_name": file_key,
            "extracted_text": extracted_text if extracted_text is not None else "",
            "processed_at": int(time.time())
        }
        # file_type from extension
        ext = os.path.splitext(file_key)[1].lower()
        if ext:
            item["file_type"] = ext.lstrip(".")
        if extra_meta and isinstance(extra_meta, dict):
            item.update(extra_meta)
        table.put_item(Item=item)
        logger.info("Wrote item to DynamoDB: %s (chars=%d)", file_key, len(item["extracted_text"]))
    except Exception as e:
        logger.exception("Failed to write to DynamoDB: %s", e)
        raise


def parse_s3_event_from_message(msg_body):
    """
    SQS message body might be:
      - an S3 event JSON directly (Records => s3)
      - an SNS wrapper (Message contains the S3 event JSON string)
    Return a list of S3 records (each has bucket/name).
    """
    try:
        body = msg_body
        if isinstance(body, str):
            body = json.loads(body)
    except Exception:
        # not JSON, return empty
        logger.exception("Failed to parse message body as JSON.")
        return []

    # If this message came from SNS, it often has "Message" which is a stringified JSON
    if isinstance(body, dict) and "Message" in body:
        try:
            nested = body.get("Message")
            # sometimes Message is already a dict; sometimes a JSON string
            if isinstance(nested, str):
                nested = json.loads(nested)
            body = nested
        except Exception:
            logger.exception("Failed to decode nested SNS Message.")

    # body should now have Records
    records = body.get("Records") if isinstance(body, dict) else None
    if not records:
        logger.warning("Message body does not contain Records: %s", body)
        return []

    s3_records = []
    for rec in records:
        s3 = rec.get("s3")
        if not s3:
            continue
        bucket = s3.get("bucket", {}).get("name")
        key = s3.get("object", {}).get("key")
        if bucket and key:
            s3_records.append({"bucket": bucket, "key": key})
    return s3_records


# -----------------------------
# Processing logic
# -----------------------------
def process_s3_object(bucket_name: str, raw_key: str):
    """Download the S3 object, extract text, and store into DynamoDB."""
    key = sanitize_s3_key(raw_key)
    ext = os.path.splitext(key)[1].lower()
    logger.info("Processing s3://%s/%s (sanitized: %s)", bucket_name, raw_key, key)

    # optional quick ext check
    if ext and ext not in ALLOWED_EXT:
        logger.info("Skipping file due to unsupported extension: %s", ext)
        # write minimal metadata to DynamoDB so dashboard shows it (optional)
        put_to_dynamo(key, "", {"skipped": True, "reason": "unsupported_extension"})
        return

    # use a temp file to download
    with tempfile.TemporaryDirectory() as tmpdir:
        local_path = os.path.join(tmpdir, key)
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
        except Exception:
            pass

        # Download
        try:
            logger.info("Downloading from S3 to %s", local_path)
            s3_client.download_file(bucket_name, raw_key, local_path)
        except Exception as e:
            logger.exception("Failed to download S3 object: %s", e)
            # Do not put to dynamo; raise so caller will not delete message and it can be retried
            raise

        extracted_text = ""

        # PDF
        if ext == ".pdf":
            extracted_text = extract_text_from_pdf(local_path)
            if not extracted_text:
                logger.info("PyPDF2 extracted no text — file might be scanned PDF. Attempting image OCR fallback is optional.")
                # fallback not implemented here because it requires poppler/pdf2image on instance.
                # If you have pdf2image + poppler, you can add an OCR fallback here.
        # Image formats
        elif ext in {".png", ".jpg", ".jpeg", ".tiff", ".bmp"}:
            extracted_text = extract_text_from_image(local_path)
        else:
            # Unknown extension: try to OCR as image (best-effort)
            try:
                extracted_text = extract_text_from_image(local_path)
            except Exception:
                extracted_text = ""

        # Save to DynamoDB (even if text is empty — still record file metadata)
        try:
            put_to_dynamo(key, extracted_text, extra_meta={"source_bucket": bucket_name})
        except Exception:
            # If saving fails, raise so message is not deleted and can be retried
            raise


def process_message(message):
    """
    message: one SQS message dict from receive_message
    """
    receipt_handle = message.get("ReceiptHandle")
    body = message.get("Body")
    logger.info("Message received. ReceiptHandle=%s", receipt_handle)

    s3_records = parse_s3_event_from_message(body)
    if not s3_records:
        logger.warning("No S3 records parsed from message; skipping deletion to allow investigation.")
        # we choose NOT to delete here so message remains for inspection/retry
        return False

    # Process each record (usually one)
    for rec in s3_records:
        bucket = rec["bucket"]
        key = rec["key"]
        try:
            process_s3_object(bucket, key)
        except Exception:
            logger.exception("Failed processing object s3://%s/%s", bucket, key)
            return False

    # If all records processed successfully:
    return True


# -----------------------------
# Main loop
# -----------------------------
def main():
    logger.info("Processor started. Polling SQS: %s", QUEUE_URL)
    try:
        while True:
            try:
                resp = sqs_client.receive_message(
                    QueueUrl=QUEUE_URL,
                    MaxNumberOfMessages=MAX_MESSAGES,
                    WaitTimeSeconds=WAIT_TIME,
                    MessageAttributeNames=["All"],
                    AttributeNames=["All"]
                )
            except Exception:
                logger.exception("Error receiving messages from SQS. Sleeping briefly then retrying.")
                time.sleep(5)
                continue

            messages = resp.get("Messages", [])
            if not messages:
                logger.debug("No messages in queue. Waiting...")
                continue

            for msg in messages:
                receipt_handle = msg.get("ReceiptHandle")
                try:
                    ok = process_message(msg)
                    if ok:
                        # delete message only after successful processing
                        sqs_client.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=receipt_handle)
                        logger.info("Message processed and deleted from queue.")
                    else:
                        logger.warning("Processing failed for message; leaving it in queue for retry.")
                except KeyboardInterrupt:
                    logger.info("KeyboardInterrupt received. Exiting without deleting current message.")
                    raise
                except Exception:
                    logger.exception("Unhandled error while processing message. Leaving message in queue.")

    except KeyboardInterrupt:
        logger.info("Processor interrupted by user. Exiting.")
    except Exception:
        logger.exception("Processor loop terminated unexpectedly.")
    finally:
        logger.info("Processor stopped.")


if __name__ == "__main__":
    main()
