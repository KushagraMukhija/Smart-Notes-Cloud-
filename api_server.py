# api_server.py
from flask import Flask, request, jsonify
from flask_cors import CORS
import boto3
from boto3.dynamodb.conditions import Attr
import logging
import os
import re
import time
from urllib.parse import unquote_plus

# -----------------------------
# Configuration - EDIT/ENV VARS
# -----------------------------
DYNAMODB_TABLE_NAME = os.environ.get("DDB_TABLE", "Notes")
S3_BUCKET_NAME = os.environ.get("S3_BUCKET", "smart-notes-repo-yourname-2025")
REGION_NAME = os.environ.get("AWS_REGION", "eu-north-1")
# Presigned URL expiry in seconds (configurable)
PRESIGNED_EXPIRE_SECONDS = int(os.environ.get("PRESIGNED_EXPIRE_SECONDS", "300"))
# Max size hint (optional; only for front-end UX; not enforced server-side here)
MAX_UPLOAD_SIZE_BYTES = int(os.environ.get("MAX_UPLOAD_SIZE_BYTES", "52428800"))  # 50 MB

# -----------------------------
# App & Logging
# -----------------------------
app = Flask(__name__)
# For development convenience allow all origins. In production restrict this to your frontend domain(s).
CORS(app, resources={r"/*": {"origins": "*"}})

# Logging: both console and optional file
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("smart-notes-api")

# -----------------------------
# AWS Clients (use IAM role on EC2)
# -----------------------------
# boto3 will pick up the instance role credentials automatically on EC2.
dynamodb = boto3.resource("dynamodb", region_name=REGION_NAME)
table = dynamodb.Table(DYNAMODB_TABLE_NAME)
s3_client = boto3.client("s3", region_name=REGION_NAME)

# -----------------------------
# Utilities
# -----------------------------
def sanitize_s3_key(raw_key: str) -> str:
    """
    Basic sanitization for S3 object key derived from client filename.
    - URL-decodes '+' and %XX sequences.
    - Removes any leading path components.
    - Replaces dangerous characters with underscores.
    - Keeps reasonable length.
    """
    if not raw_key:
        return raw_key
    # decode + and %xx
    key = unquote_plus(raw_key)
    # keep only base name (prevent directory traversal like ../../)
    key = os.path.basename(key)
    # replace whitespace and problematic chars
    key = re.sub(r"[^\w\-.() ]+", "_", key)
    # collapse multiple underscores
    key = re.sub(r"_+", "_", key)
    # trim length to sane limit
    if len(key) > 240:
        name, ext = os.path.splitext(key)
        key = name[:200] + ext
    return key

def scan_table_all(projection_expression=None):
    """
    Retrieve all items from the DynamoDB table (paginated).
    """
    items = []
    scan_kwargs = {}
    if projection_expression:
        scan_kwargs["ProjectionExpression"] = projection_expression

    done = False
    start_key = None
    try:
        while not done:
            if start_key:
                scan_kwargs["ExclusiveStartKey"] = start_key
            response = table.scan(**scan_kwargs)
            items.extend(response.get("Items", []))
            start_key = response.get("LastEvaluatedKey", None)
            done = start_key is None
        return items
    except Exception as ex:
        logger.exception("DynamoDB scan error: %s", ex)
        raise

# -----------------------------
# Routes
# -----------------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": int(time.time())}), 200


@app.route("/search", methods=["GET"])
def search_notes():
    """
    Search for a substring in extracted_text (case-insensitive fallback).
    Uses DynamoDB contains() first (case-sensitive), then full-scan fallback for case-insensitive matching.
    """
    query_param = request.args.get("query")
    if not query_param:
        return jsonify({"error": "Query parameter is missing"}), 400

    try:
        # 1) Try DynamoDB contains (fast, but case-sensitive)
        try:
            response = table.scan(
                FilterExpression=Attr("extracted_text").contains(query_param)
            )
            items = response.get("Items", [])
            if items:
                logger.info("Returned %d items using DynamoDB contains()", len(items))
                return jsonify(items), 200
            # if zero items, continue to case-insensitive fallback
        except Exception as de:
            logger.info("DynamoDB contains() attempt failed: %s. Falling back to full scan.", de)

        # 2) Full scan + case-insensitive matching
        lower_q = query_param.lower()
        all_items = scan_table_all(projection_expression=None)
        results = []
        for item in all_items:
            text = item.get("extracted_text", "")
            if text and lower_q in text.lower():
                results.append(item)

        logger.info("Returned %d items using full-scan fallback", len(results))
        return jsonify(results), 200

    except Exception as e:
        logger.exception("Error querying DynamoDB: %s", e)
        return jsonify({"error": "Failed to query database"}), 500


@app.route("/get-upload-url", methods=["GET"])
def get_upload_url():
    """
    Returns a presigned POST (recommended) and a fallback presigned PUT URL for direct browser upload.
    Frontend can choose POST (preferred) or PUT.
    Query params:
      - filename (required)
      - content_type (optional) : used for conditions in presigned POST
    """
    file_name_raw = request.args.get("filename")
    content_type = request.args.get("content_type")  # optional hint for POST conditions

    if not file_name_raw:
        return jsonify({"error": "filename parameter is missing"}), 400

    try:
        # Sanitize key
        key = sanitize_s3_key(file_name_raw)
        # Recommended: use presigned POST so browsers can set Content-Type and CORS works cleanly
        post_fields = {"acl": "private"}  # default fields; you can change if you want public-read
        conditions = [
            {"acl": "private"},
            ["content-length-range", 1, MAX_UPLOAD_SIZE_BYTES],
            ["starts-with", "$key", ""],  # allow any key (we set exact key in fields)
        ]
        if content_type:
            # require the content-type to start with the provided value (e.g., "image/" or "application/pdf")
            # the condition below ensures content-type starts with the string; browsers set content-type automatically
            conditions.append(["starts-with", "$Content-Type", content_type])

        # Generate presigned POST
        post = s3_client.generate_presigned_post(
            Bucket=S3_BUCKET_NAME,
            Key=key,
            Fields={"key": key, **(post_fields or {})},
            Conditions=conditions,
            ExpiresIn=PRESIGNED_EXPIRE_SECONDS,
        )

        # Also provide a fallback presigned PUT url (some clients use PUT)
        put_url = s3_client.generate_presigned_url(
            "put_object",
            Params={"Bucket": S3_BUCKET_NAME, "Key": key},
            ExpiresIn=PRESIGNED_EXPIRE_SECONDS,
        )

        return jsonify({
            "key": key,
            "upload_post": post,  # contains url, fields (use with form POST)
            "upload_put_url": put_url,  # fallback: PUT directly to this URL
            "expires_in": PRESIGNED_EXPIRE_SECONDS
        }), 200

    except Exception as e:
        logger.exception("Could not generate presigned upload URL: %s", e)
        return jsonify({"error": "Could not get upload URL"}), 500


@app.route("/get-download-url", methods=["GET"])
def get_download_url():
    """
    Returns a presigned GET URL for the requested file.
    """
    file_name_raw = request.args.get("filename")
    if not file_name_raw:
        return jsonify({"error": "filename parameter is missing"}), 400

    try:
        key = sanitize_s3_key(file_name_raw)
        presigned_url = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET_NAME, "Key": key},
            ExpiresIn=PRESIGNED_EXPIRE_SECONDS,
        )
        return jsonify({"download_url": presigned_url, "expires_in": PRESIGNED_EXPIRE_SECONDS}), 200
    except Exception as e:
        logger.exception("Error generating download URL: %s", e)
        return jsonify({"error": "Could not get download URL"}), 500


@app.route("/all-notes", methods=["GET"])
def get_all_notes():
    """
    Return all notes stored in DynamoDB.
    Note: For large tables consider adding pagination or limiting results.
    """
    try:
        items = scan_table_all()
        return jsonify(items), 200
    except Exception as e:
        logger.exception("Failed to fetch notes: %s", e)
        return jsonify({"error": "Failed to fetch notes"}), 500


# -----------------------------
# Run (development only)
# -----------------------------
if __name__ == "__main__":
    # In production: run under gunicorn or uwsgi; do not enable debug.
    app.run(host="0.0.0.0", port=8000)
