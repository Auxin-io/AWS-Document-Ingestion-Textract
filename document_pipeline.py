"""Turn PDFs into text on Azure: Blob Storage in, Document Intelligence OCR, Blob Storage out.

    upload    put local files into the `raw` container
    extract   raw documents -> text + metadata in the `curated` container

    python document_pipeline.py upload data/pdfs/finance --prefix finance
    python document_pipeline.py extract --workers 8

Authentication is `DefaultAzureCredential`: `az login` on a laptop, managed
identity on Azure compute. No keys are read from files or environment.

Connection values come from `terraform output` (or the environment):

    AZURE_STORAGE_ACCOUNT     storage account name
    AZURE_DOCINTEL_ENDPOINT   https://<name>.cognitiveservices.azure.com/

Every step is idempotent. `extract` skips documents already present in the
curated container, so re-running costs nothing in OCR charges.

There is deliberately NO access-control step here. Document permissions, when
needed, belong to the serving layer - this repo only produces text and labels.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

RAW_CONTAINER = "raw"
CURATED_CONTAINER = "curated"

OCR_TYPES = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif"}
TEXT_TYPES = {".txt", ".md", ".csv", ".json", ".log"}

# Regex screen only - it tags, it does not block, and it over-flags.
PII_PATTERNS = {
    "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    "phone": r"\b(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}\b",
    "ssn": r"\b\d{3}-\d{2}-\d{4}\b",
    "card": r"\b(?:\d[ -]*?){13,16}\b",
}


# ---------------------------------------------------------------- config ---
def _cfg(name: str) -> str:
    val = os.environ.get(name, "")
    if not val:
        raise SystemExit(
            f"{name} is not set. Deploy terraform/ first, then:\n"
            f"  export AZURE_STORAGE_ACCOUNT=$(terraform -chdir=terraform output -raw storage_account)\n"
            f"  export AZURE_DOCINTEL_ENDPOINT=$(terraform -chdir=terraform output -raw docintel_endpoint)")
    return val


_cred = None


def credential():
    """DefaultAzureCredential, imported lazily so --help needs no SDK or login."""
    global _cred
    if _cred is None:
        from azure.identity import DefaultAzureCredential
        _cred = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    return _cred


def blob_service():
    from azure.storage.blob import BlobServiceClient
    account = _cfg("AZURE_STORAGE_ACCOUNT")
    return BlobServiceClient(f"https://{account}.blob.core.windows.net", credential=credential())


def docintel():
    from azure.ai.documentintelligence import DocumentIntelligenceClient
    return DocumentIntelligenceClient(_cfg("AZURE_DOCINTEL_ENDPOINT"), credential())


# --------------------------------------------------------------- helpers ---
@dataclass
class DocumentRecord:
    doc_id: str
    source_blob: str
    dataset: str
    doc_type: str
    page_count: int
    char_count: int
    pii_flags: list[str]
    extracted_with: str


def doc_id_for(blob_name: str) -> str:
    return "doc-" + Path(blob_name).stem.lower()


def detect_pii(text: str) -> list[str]:
    return sorted(k for k, pat in PII_PATTERNS.items() if re.search(pat, text))


def classify_from_text(text: str) -> str:
    """Cheap heuristic for doc_type - a seed for metadata, not ground truth."""
    low = text.lower()
    rules = [
        ("purchase_order", ("purchase order", "order reference")),
        ("invoice", ("invoice no", "remit to", "invoice")),
        ("timesheet", ("timesheet", "week ending")),
        ("expense_report", ("expense report", "total claimed")),
        ("leave_request", ("leave request", "leave type")),
        ("policy", ("policy reference", "policy")),
    ]
    for doc_type, needles in rules:
        if any(n in low for n in needles):
            return doc_type
    return "unknown"


def extract_with_docintel(client, pdf_bytes: bytes) -> tuple[str, int]:
    """OCR with the prebuilt `read` model. Returns (text, page_count).

    `read` is the layout-free model: it returns lines in reading order across
    any number of pages - the equivalent of Textract's DetectDocumentText on
    AWS. Tables come back flattened into header-then-value lines the same way.
    """
    from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
    poller = client.begin_analyze_document(
        "prebuilt-read", AnalyzeDocumentRequest(bytes_source=pdf_bytes))
    result = poller.result()
    lines = [line.content for page in result.pages for line in (page.lines or [])]
    return "\n".join(lines), len(result.pages)


# -------------------------------------------------------------- commands ---
def cmd_upload(args) -> None:
    from azure.storage.blob import ContentSettings
    src = Path(args.path)
    files = sorted(p for p in (src.rglob("*") if src.is_dir() else [src])
                   if p.is_file() and p.suffix.lower() in OCR_TYPES | TEXT_TYPES)
    if not files:
        raise SystemExit(f"no supported files under {src}")

    container = blob_service().get_container_client(RAW_CONTAINER)
    for f in files:
        name = f"{args.prefix.strip('/')}/{f.name}" if args.prefix else f.name
        ctype = "application/pdf" if f.suffix.lower() == ".pdf" else "application/octet-stream"
        with f.open("rb") as fh:
            container.upload_blob(name, fh, overwrite=True,
                                  content_settings=ContentSettings(content_type=ctype))
        print(f"uploaded {f.name:<32} -> {RAW_CONTAINER}/{name}")
    print(f"\n{len(files)} file(s) uploaded. Next: python document_pipeline.py extract")


def cmd_extract(args) -> None:
    from azure.storage.blob import ContentSettings
    svc = blob_service()
    raw = svc.get_container_client(RAW_CONTAINER)
    curated = svc.get_container_client(CURATED_CONTAINER)

    blobs = [b.name for b in raw.list_blobs(name_starts_with=args.prefix or None)
             if Path(b.name).suffix.lower() in OCR_TYPES | TEXT_TYPES]
    if not blobs:
        raise SystemExit(f"no documents in container '{RAW_CONTAINER}' - run upload first")

    done = {Path(b.name).stem for b in curated.list_blobs(name_starts_with="documents/")
            if b.name.endswith(".txt")}
    todo = [b for b in blobs if doc_id_for(b) not in done]
    print(f"{len(blobs)} document(s) in raw, {len(todo)} to extract, {len(done)} already done")
    if not todo:
        return

    client = docintel()

    def work(name: str) -> DocumentRecord:
        data = raw.download_blob(name).readall()
        ext = Path(name).suffix.lower()
        if ext in TEXT_TYPES:
            text, pages, how = data.decode("utf-8", errors="replace"), 1, "plain-text"
        else:
            text, pages = extract_with_docintel(client, data)
            how = "document-intelligence:prebuilt-read"

        doc_id = doc_id_for(name)
        dataset = name.split("/")[0] if "/" in name else "unassigned"
        rec = DocumentRecord(doc_id, f"{RAW_CONTAINER}/{name}", dataset,
                             classify_from_text(text), pages, len(text),
                             detect_pii(text), how)
        curated.upload_blob(f"documents/{doc_id}.txt", text.encode("utf-8"), overwrite=True,
                            content_settings=ContentSettings(content_type="text/plain"))
        curated.upload_blob(f"documents/{doc_id}.json", json.dumps(asdict(rec)).encode(),
                            overwrite=True,
                            content_settings=ContentSettings(content_type="application/json"))
        return rec

    records: list[DocumentRecord] = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(work, b): b for b in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                rec = fut.result()
            except Exception as exc:  # noqa: BLE001 - report and keep going
                print(f"  [{i}/{len(todo)}] FAILED {futures[fut]}: {exc}")
                continue
            records.append(rec)
            flags = f"  pii={','.join(rec.pii_flags)}" if rec.pii_flags else ""
            print(f"  [{i}/{len(todo)}] {rec.doc_id:<22} {rec.doc_type:<15} "
                  f"{rec.page_count}p {rec.char_count:>6} chars{flags}")

    # manifest: one line per document, appended across runs
    lines: list[str] = []
    try:
        lines = curated.download_blob("documents/_manifest.jsonl").readall() \
            .decode("utf-8").splitlines()
    except Exception:  # noqa: BLE001 - first run, no manifest yet
        pass
    lines += [json.dumps(asdict(r)) for r in records]
    curated.upload_blob("documents/_manifest.jsonl", "\n".join(lines).encode(), overwrite=True)

    print(f"\n{len(records)} extracted in {time.time() - t0:.0f}s -> "
          f"{CURATED_CONTAINER}/documents/  (manifest: {len(lines)} rows)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("upload", help="copy local files into the raw container")
    up.add_argument("path")
    up.add_argument("--prefix", default="", help="blob prefix, e.g. the dataset name")
    up.set_defaults(fn=cmd_upload)

    ex = sub.add_parser("extract", help="OCR raw documents into the curated container")
    ex.add_argument("--prefix", default="", help="only blobs under this prefix")
    ex.add_argument("--workers", type=int, default=8)
    ex.set_defaults(fn=cmd_extract)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
