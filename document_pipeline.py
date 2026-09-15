"""Turn real documents into text the model can be trained and served on.

Replaces the previous scaffold, which returned a hardcoded sha256 and did no
extraction at all.

    upload    put local files into the raw bucket
    extract   raw documents -> text + metadata in the curated bucket
    register  write ACL rows and catalog metadata for extracted documents

Extraction picks its method from the file type:
  * pdf / png / jpg / tiff -> Amazon Textract (real OCR, handles scans)
  * txt / md / csv / json  -> read directly, no OCR needed and no OCR cost

Textract is billed per page, so extraction runs ONCE per document and the text
is cached in the curated bucket. Re-running skips anything already extracted
unless --force is passed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

import os

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
ACL_TABLE = os.environ.get("DOCINTEL_ACL_TABLE", "docintel-dev-document-acl")

# Bucket names are resolved lazily in main(), not at import time, so `--help`
# works without credentials and a bad session fails with a clear message.
# Override with DOCINTEL_RAW_BUCKET / DOCINTEL_CURATED_BUCKET (e.g. from
# `terraform output`) if your names differ from the Terraform defaults.
RAW_BUCKET = ""
CURATED_BUCKET = ""


def resolve_buckets() -> None:
    global RAW_BUCKET, CURATED_BUCKET
    RAW_BUCKET = os.environ.get("DOCINTEL_RAW_BUCKET", "")
    CURATED_BUCKET = os.environ.get("DOCINTEL_CURATED_BUCKET", "")
    if RAW_BUCKET and CURATED_BUCKET:
        return
    try:
        account = boto3.client("sts").get_caller_identity()["Account"]
    except Exception as exc:  # noqa: BLE001 - surface the real cause
        raise SystemExit(f"AWS credentials are not usable ({exc.__class__.__name__}). "
                         "Run `aws sts get-caller-identity` to check, or set "
                         "DOCINTEL_RAW_BUCKET and DOCINTEL_CURATED_BUCKET.") from exc
    RAW_BUCKET = RAW_BUCKET or f"docintel-dev-raw-documents-{account}-{REGION}"
    CURATED_BUCKET = CURATED_BUCKET or f"docintel-dev-curated-datasets-{account}-{REGION}"

OCR_TYPES = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif"}
TEXT_TYPES = {".txt", ".md", ".csv", ".json", ".log"}

# Deliberately conservative: these patterns over-flag rather than miss. A
# document marked contains_pii should be reviewed before it reaches training.
PII_PATTERNS = {
    "email": r"[\w.+-]+@[\w-]+\.[\w.]+",
    "phone": r"\+?\d[\d\s().-]{8,}\d",
    "ssn": r"\b\d{3}-\d{2}-\d{4}\b",
    "card": r"\b(?:\d[ -]?){13,16}\b",
    "iban": r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b",
}


@dataclass
class DocumentRecord:
    doc_id: str
    source_key: str
    sha256: str
    doc_type: str
    classification: str
    department: str
    page_count: int
    char_count: int
    language: str
    contains_pii: bool
    pii_kinds: list[str]
    extracted_by: str
    extracted_at: str


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def detect_pii(text: str) -> list[str]:
    return sorted(k for k, pat in PII_PATTERNS.items() if re.search(pat, text))


def extract_with_textract(bucket: str, key: str) -> tuple[str, int]:
    """Run Textract over an S3 object and return (text, page_count).

    Tries the SYNCHRONOUS API first: it accepts single-page PDFs and returns
    immediately, which is roughly 20x faster than starting a job and polling.
    Falls back to the async API for anything multi-page, which is the only way
    to handle real multi-page contracts.
    """
    tx = boto3.client("textract", region_name=REGION)
    try:
        res = tx.detect_document_text(
            Document={"S3Object": {"Bucket": bucket, "Name": key}})
        lines = [b["Text"] for b in res["Blocks"] if b["BlockType"] == "LINE"]
        return chr(10).join(lines), res["DocumentMetadata"]["Pages"]
    except tx.exceptions.UnsupportedDocumentException:
        pass          # multi-page PDF - fall through to the async path
    except tx.exceptions.InvalidParameterException:
        pass

    job = tx.start_document_text_detection(
        DocumentLocation={"S3Object": {"Bucket": bucket, "Name": key}})["JobId"]

    while True:
        res = tx.get_document_text_detection(JobId=job)
        status = res["JobStatus"]
        if status == "SUCCEEDED":
            break
        if status == "FAILED":
            raise RuntimeError(f"textract failed for {key}: {res.get('StatusMessage')}")
        time.sleep(3)

    lines, pages, token = [], res["DocumentMetadata"]["Pages"], None
    while True:
        for block in res["Blocks"]:
            if block["BlockType"] == "LINE":
                lines.append(block["Text"])
        token = res.get("NextToken")
        if not token:
            break
        res = tx.get_document_text_detection(JobId=job, NextToken=token)
    return "\n".join(lines), pages


def classify_from_text(text: str) -> tuple[str, str]:
    """Cheap heuristic first pass at doc_type and department.

    This is a starting point, not ground truth - it seeds the metadata so a
    human (or the fine-tuned model itself) can correct it later.
    """
    low = text.lower()
    rules = [
        ("invoice", "finance", ("invoice no", "invoice #", "remit to", "amount due")),
        ("purchase_order", "operations", ("purchase order", "po number", "po-")),
        ("contract", "legal", ("agreement", "termination", "liability", "hereby")),
        ("policy", "hr", ("policy", "must be approved", "employees must")),
        ("handbook", "hr", ("handbook", "section")),
    ]
    for doc_type, dept, needles in rules:
        if any(n in low for n in needles):
            return doc_type, dept
    return "unknown", "operations"


def cmd_upload(args) -> None:
    s3 = boto3.client("s3", region_name=REGION)
    src = Path(args.path)
    files = [src] if src.is_file() else [p for p in src.rglob("*") if p.is_file()]
    if not files:
        raise SystemExit(f"no files found under {src}")
    for f in files:
        key = f"{args.prefix.strip('/')}/{f.name}"
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        s3.upload_file(str(f), RAW_BUCKET, key, ExtraArgs={"ContentType": ctype})
        print(f"uploaded {f.name:<40} -> s3://{RAW_BUCKET}/{key}")
    print(f"\n{len(files)} file(s) uploaded. Next: python ingest/document_pipeline.py extract")


def cmd_extract(args) -> None:
    s3 = boto3.client("s3", region_name=REGION)
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=RAW_BUCKET)
    objects = [o for pg in pages for o in pg.get("Contents", []) if o["Size"] > 0]
    if not objects:
        raise SystemExit(f"no documents in s3://{RAW_BUCKET}/ - run upload first")

    existing = set()
    if not args.force:
        pg = s3.get_paginator("list_objects_v2").paginate(
            Bucket=CURATED_BUCKET, Prefix="documents/")
        existing = {o["Key"] for p in pg for o in p.get("Contents", [])}

    def process(obj) -> DocumentRecord | None:
        """Extract one document. Returns None when skipped, never raises."""
        key = obj["Key"]
        doc_id = args.doc_prefix + Path(key).stem.lower().replace(" ", "-")
        text_key = f"documents/{doc_id}.txt"
        if text_key in existing:
            return None
        suffix = Path(key).suffix.lower()
        if suffix not in OCR_TYPES and suffix not in TEXT_TYPES:
            return None

        # each thread needs its own boto3 clients - they are not thread safe
        s3t = boto3.client("s3", region_name=REGION)
        try:
            body = s3t.get_object(Bucket=RAW_BUCKET, Key=key)["Body"].read()
            if suffix in OCR_TYPES:
                text, page_count = extract_with_textract(RAW_BUCKET, key)
                method = "textract"
            else:
                text = body.decode("utf-8", "replace")
                page_count = max(1, text.count("") + 1)
                method = "direct"
        except Exception as exc:
            print(f"error  {key}: {type(exc).__name__}: {str(exc)[:110]}")
            return None

        text = re.sub(r"[ 	]+", " ", text).strip()
        if not text:
            print(f"warn   {key} produced no text")
            return None

        pii = detect_pii(text)
        doc_type, dept = classify_from_text(text)
        rec = DocumentRecord(
            doc_id=doc_id, source_key=key, sha256=sha256_of(body),
            doc_type=doc_type, classification=args.classification, department=dept,
            page_count=page_count, char_count=len(text), language="en",
            contains_pii=bool(pii), pii_kinds=pii, extracted_by=method,
            extracted_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        s3t.put_object(Bucket=CURATED_BUCKET, Key=text_key,
                       Body=text.encode("utf-8"), ContentType="text/plain")
        s3t.put_object(Bucket=CURATED_BUCKET, Key=f"documents/{doc_id}.json",
                       Body=json.dumps(asdict(rec), indent=2).encode("utf-8"),
                       ContentType="application/json")
        return rec

    print(f"extracting {len(objects)} document(s) with {args.workers} workers ...")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(process, objects))
    records = [r for r in results if r is not None]

    for i, rec in enumerate(records, 1):
        if i <= 3 or i % 100 == 0:
            flag = f"  PII:{','.join(rec.pii_kinds)}" if rec.contains_pii else ""
            print(f"ok     {rec.doc_id:<24} {rec.extracted_by:<9} {rec.page_count}p "
                  f"{rec.char_count:>6} chars {rec.doc_type}/{rec.department}{flag}")

    if records:
        manifest = chr(10).join(json.dumps(asdict(r)) for r in records)
        s3.put_object(Bucket=CURATED_BUCKET, Key="documents/_manifest.jsonl",
                      Body=manifest.encode("utf-8"))
        flagged = sum(1 for r in records if r.contains_pii)
        print()
        print(f"{len(records)} extracted, {len(objects) - len(records)} skipped")
        print(f"REVIEW: {flagged} document(s) matched PII patterns")


def cmd_register(args) -> None:
    """Grant a principal access to every extracted document."""
    s3 = boto3.client("s3", region_name=REGION)
    table = boto3.resource("dynamodb", region_name=REGION).Table(ACL_TABLE)
    body = s3.get_object(Bucket=CURATED_BUCKET,
                         Key="documents/_manifest.jsonl")["Body"].read().decode()
    n = 0
    for line in body.splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        table.put_item(Item={
            "doc_id": r["doc_id"], "principal_id": args.principal,
            "permission": args.permission, "classification": r["classification"],
            "department": r["department"],
            "s3_uri": f"s3://{CURATED_BUCKET}/documents/{r['doc_id']}.txt",
        })
        n += 1
        print(f"granted {args.permission:<6} on {r['doc_id']} to {args.principal}")
    print(f"\n{n} ACL row(s) written to {ACL_TABLE}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("upload", help="copy local files into the raw bucket")
    up.add_argument("path", help="file or directory of documents")
    up.add_argument("--prefix", default="incoming")
    up.set_defaults(func=cmd_upload)

    ex = sub.add_parser("extract", help="raw -> text + metadata in curated")
    ex.add_argument("--force", action="store_true", help="re-extract already-done documents")
    ex.add_argument("--classification", default="confidential",
                    choices=["public", "internal", "confidential", "restricted"])
    ex.add_argument("--doc-prefix", default="doc-")
    ex.add_argument("--workers", type=int, default=12,
                    help="parallel Textract calls; lower if you hit throttling")
    ex.set_defaults(func=cmd_extract)

    rg = sub.add_parser("register", help="write ACL rows for extracted documents")
    rg.add_argument("--principal", required=True, help="Cognito sub of the grantee")
    rg.add_argument("--permission", default="read", choices=["read", "owner"])
    rg.set_defaults(func=cmd_register)

    args = p.parse_args()
    resolve_buckets()
    args.func(args)


if __name__ == "__main__":
    main()
