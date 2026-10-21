import hashlib
import io
import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid

from azure.identity import DefaultAzureCredential
from azure.core.exceptions import AzureError
from azure.storage.blob import BlobServiceClient
from fastapi import FastAPI, HTTPException, UploadFile
from pypdf import PdfReader
from pypdf.errors import PyPdfError

app = FastAPI(title="NIAI Document Workshop", version="1.0")
log = logging.getLogger("uvicorn.error")
MAX_BYTES = 2 * 1024 * 1024


@app.get("/health")
def health():
    return {"status": "ok", "version": os.getenv("APP_VERSION", "v1")}


def pdf_bytes(file: UploadFile):
    try:
        data = file.file.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise HTTPException(413, "Use a PDF smaller than 2 MiB.")
        if not file.filename or not file.filename.lower().endswith(".pdf") or not data.startswith(b"%PDF-"):
            raise HTTPException(400, "Upload a PDF file.")
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise HTTPException(400, "Use an unencrypted PDF.")
        if len(reader.pages) > 2:
            raise HTTPException(400, "Use at most two pages in this workshop.")
        return data, reader
    except PyPdfError as error:
        raise HTTPException(400, "Could not read this PDF.") from error
    finally:
        file.file.close()


@app.post("/upload-pdf")
def read_pdf(file: UploadFile):
    _, reader = pdf_bytes(file)
    request_id = uuid.uuid4().hex
    for page in reader.pages:
        for line in (page.extract_text() or "").splitlines():
            if line.strip():
                log.info("request_id=%s operation=read outcome=success", request_id)
                return {"filename": file.filename, "first_line": line.strip(), "request_id": request_id}
    raise HTTPException(422, "No embedded text. Use /ocr for a scanned PDF.")


@app.post("/store-pdf")
def store_pdf(file: UploadFile):
    data, _ = pdf_bytes(file)
    account = os.getenv("STORAGE_ACCOUNT_URL")
    if not account:
        raise HTTPException(503, "Configure STORAGE_ACCOUNT_URL first.")
    request_id = uuid.uuid4().hex
    name = request_id + ".pdf"
    try:
        with DefaultAzureCredential() as credential:
            with BlobServiceClient(account_url=account, credential=credential) as client:
                client.get_blob_client(os.getenv("STORAGE_CONTAINER", "documents"), name).upload_blob(data, overwrite=False)
    except AzureError as error:
        status = getattr(error, "status_code", None)
        log.warning("request_id=%s operation=store service_status=%s", request_id, status)
        raise HTTPException(502, "Storage service rejected the request. Check identity, role and network access.") from error
    log.info("request_id=%s operation=store outcome=success", request_id)
    return {"blob_name": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


@app.post("/ocr")
def ocr_pdf(file: UploadFile):
    data, _ = pdf_bytes(file)
    endpoint = os.getenv("DOCUMENT_INTELLIGENCE_ENDPOINT", "").rstrip("/")
    if not endpoint.startswith("https://"):
        raise HTTPException(503, "Configure DOCUMENT_INTELLIGENCE_ENDPOINT first.")
    request_id = uuid.uuid4().hex
    with DefaultAzureCredential() as credential:
        token = credential.get_token("https://cognitiveservices.azure.com/.default").token
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/pdf"}
        url = endpoint + "/documentintelligence/documentModels/prebuilt-read:analyze?api-version=2024-11-30"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method="POST"), timeout=20) as response:
                operation = response.headers["Operation-Location"]
            if not operation.startswith(endpoint + "/"):
                raise HTTPException(502, "Unexpected analysis operation URL.")
            # shortcut: synchronous polling is for this small lab. Use durable jobs for long documents.
            deadline = time.monotonic() + 75
            while time.monotonic() < deadline:
                with urllib.request.urlopen(urllib.request.Request(operation, headers={"Authorization": "Bearer " + token}), timeout=20) as response:
                    result = json.load(response)
                    delay = min(max(float(response.headers.get("Retry-After", "2")), 1), 10)
                if result["status"] == "succeeded":
                    analysis = result["analyzeResult"]
                    log.info("request_id=%s operation=ocr outcome=success", request_id)
                    return {"request_id": request_id, "status": "succeeded", "content": analysis.get("content", ""), "pages": len(analysis.get("pages", []))}
                if result["status"] == "failed":
                    raise HTTPException(502, "Document analysis failed. Inspect the service operation.")
                time.sleep(delay)
        except urllib.error.HTTPError as error:
            log.warning("request_id=%s operation=ocr service_status=%s", request_id, error.code)
            raise HTTPException(502, "OCR service returned HTTP " + str(error.code)) from error
    raise HTTPException(504, "OCR exceeded the workshop wait limit.")
