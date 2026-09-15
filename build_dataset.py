"""Open-book training JSONL: OCR text + labels, one dataset at a time.

    python build_dataset.py --dataset finance
    python build_dataset.py --all

Reads the OCR text from the curated container, joins it to the ground truth
by doc_id, and writes data/dataset_<name>/{train,validation,test}.jsonl.

Each row is four strings:

    {"task": ..., "instruction": ..., "input": <OCR text>, "output": ...}

`input` is the genuine Document Intelligence output, so a model trained on
it meets the same kind of text in production.

With ten documents per dataset the split is 8 / 1 / 1. That is enough to
train the output convention, and deliberately NOT enough to claim a
generalisation score - see build_closed_book.py for the split that measures
whether facts were actually learned.
"""

from __future__ import annotations

import argparse
import json
import os
import random
from pathlib import Path

DATASETS = ("employee", "finance", "hr")


def load_curated_text(dataset: str) -> dict[str, str]:
    """doc_id -> OCR text from the curated container."""
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient
    account = os.environ.get("AZURE_STORAGE_ACCOUNT", "")
    if not account:
        raise SystemExit("AZURE_STORAGE_ACCOUNT is not set - deploy terraform/ first, then\n"
                         "  export AZURE_STORAGE_ACCOUNT=$(terraform -chdir=terraform output -raw storage_account)")
    svc = BlobServiceClient(f"https://{account}.blob.core.windows.net",
                            credential=DefaultAzureCredential(exclude_interactive_browser_credential=True))
    curated = svc.get_container_client("curated")
    out: dict[str, str] = {}
    for b in curated.list_blobs(name_starts_with=f"documents/doc-{dataset}-"):
        if b.name.endswith(".txt"):
            doc_id = Path(b.name).stem
            out[doc_id] = curated.download_blob(b.name).readall().decode("utf-8")
    return out


def build(dataset: str, pdf_dir: Path, out_root: Path, seed: int) -> None:
    truth_path = pdf_dir / f"ground_truth_{dataset}.json"
    if not truth_path.exists():
        raise SystemExit(f"{truth_path} not found - run generate_pdfs.py --dataset {dataset}")
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    text = load_curated_text(dataset)

    rows, missing = [], []
    for doc_id, rec in sorted(truth.items()):
        if doc_id not in text:
            missing.append(doc_id)
            continue
        rows.append({"task": rec["task"], "instruction": rec["instruction"],
                     "input": text[doc_id], "output": rec["output"], "doc_id": doc_id})
    if missing:
        print(f"  {len(missing)} document(s) have no OCR text yet: {', '.join(missing)}")
    if not rows:
        raise SystemExit(f"no rows for {dataset} - run document_pipeline.py extract first")

    rng = random.Random(seed)
    rng.shuffle(rows)
    n_val = max(1, len(rows) // 10)
    n_test = max(1, len(rows) // 10)
    splits = {"test": rows[:n_test], "validation": rows[n_test:n_test + n_val],
              "train": rows[n_test + n_val:]}

    out = out_root / f"dataset_{dataset}"
    out.mkdir(parents=True, exist_ok=True)
    for name, data in splits.items():
        with (out / f"{name}.jsonl").open("w", encoding="utf-8") as fh:
            for r in data:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"  {name:<11} {len(data):>3} rows")
    print(f"{dataset}: {len(rows)} documents -> {out}\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", choices=DATASETS)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--pdf-dir", type=Path, default=Path("data/pdfs"))
    ap.add_argument("--out-dir", type=Path, default=Path("data"))
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()
    if not args.dataset and not args.all:
        ap.error("choose --dataset <name> or --all")
    for ds in (DATASETS if args.all else [args.dataset]):
        build(ds, args.pdf_dir, args.out_dir, args.seed)


if __name__ == "__main__":
    main()
