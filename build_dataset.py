"""Build training JSONL from OCR'd documents, not from strings in code.

Joins two things by doc_id:
  * the TEXT Textract produced, read from the curated bucket
  * the correct ANSWER, from the ground-truth file the PDF generator wrote

The `input` field is therefore genuine OCR output - with whatever line ordering
and spacing Textract produced - rather than a Python f-string. That matters:
the model is trained on the same kind of text it will see in production.

    python build_dataset.py --out-dir data/dataset_pdf
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

import os

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
CURATED = os.environ.get("DOCINTEL_CURATED_BUCKET", "")   # resolved in main()


def resolve_bucket() -> None:
    global CURATED
    if CURATED:
        return
    try:
        account = boto3.client("sts").get_caller_identity()["Account"]
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"AWS credentials are not usable ({exc.__class__.__name__}). "
                         "Run `aws sts get-caller-identity`, or set DOCINTEL_CURATED_BUCKET.") from exc
    CURATED = f"docintel-dev-curated-datasets-{account}-{REGION}"


def load_extracted(s3) -> dict[str, str]:
    """doc_id -> OCR text, straight from the curated bucket."""
    out: dict[str, str] = {}
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=CURATED, Prefix="documents/")
    for page in pages:
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".txt"):
                continue
            doc_id = Path(key).stem
            out[doc_id] = s3.get_object(Bucket=CURATED, Key=key)["Body"].read().decode("utf-8")
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pdf-dir", default="data/pdfs")
    p.add_argument("--out-dir", default="data/dataset_pdf")
    p.add_argument("--val-fraction", type=float, default=0.15)
    p.add_argument("--max-train", type=int, default=0,
                   help="cap the training split at N documents (0 = use all). "
                        "The cap is applied AFTER shuffling and is stratified by "
                        "task, so the task mix is preserved rather than skewed.")
    p.add_argument("--seed", type=int, default=13)
    args = p.parse_args()
    resolve_bucket()

    s3 = boto3.client("s3")
    texts = load_extracted(s3)
    if not texts:
        raise SystemExit("no extracted text found - run document_pipeline.py extract first")
    print(f"OCR text available for {len(texts)} documents")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    written: dict[str, int] = {}
    all_rows: dict[str, list[dict]] = {}

    for split in ("train", "test"):
        gt_path = Path(args.pdf_dir) / f"ground_truth_{split}.json"
        if not gt_path.exists():
            print(f"skip {split}: {gt_path} not found")
            continue
        truth = json.loads(gt_path.read_text(encoding="utf-8"))

        rows, missing = [], []
        for doc_id, record in truth.items():
            text = texts.get(doc_id)
            if not text:
                missing.append(doc_id)
                continue
            rows.append({
                "task": record["task"],
                "doc_id": doc_id,
                "instruction": record["instruction"],
                "input": text,              # <- real OCR output
                "output": record["output"],
            })
        if missing:
            print(f"  {split}: {len(missing)} document(s) had no extracted text "
                  f"({', '.join(missing[:4])}{' ...' if len(missing) > 4 else ''})")
        all_rows[split] = rows

    train_rows = all_rows.get("train", [])
    rng.shuffle(train_rows)

    # Cap the training set if asked, keeping the task mix intact. Taking a flat
    # head of a shuffled list would drift the proportions on a small sample.
    if args.max_train and len(train_rows) > args.max_train:
        by_task: dict[str, list[dict]] = {}
        for r in train_rows:
            by_task.setdefault(r["task"], []).append(r)
        share = args.max_train / len(train_rows)
        capped: list[dict] = []
        for task, rows in by_task.items():
            keep = max(1, round(len(rows) * share))
            capped.extend(rows[:keep])
        rng.shuffle(capped)
        print(f"capped train {len(train_rows)} -> {len(capped)} documents "
              f"(stratified by task)")
        train_rows = capped[:args.max_train]

    # carve a validation split out of train
    cut = int(len(train_rows) * args.val_fraction)
    splits = {
        "validation": train_rows[:cut],
        "train": train_rows[cut:],
        "test": all_rows.get("test", []),
    }

    for name, rows in splits.items():
        path = out_dir / f"{name}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        written[name] = len(rows)
        tasks: dict[str, int] = {}
        for r in rows:
            tasks[r["task"]] = tasks.get(r["task"], 0) + 1
        print(f"{path}  {len(rows):>4} rows  {tasks}")

    # the property the whole evaluation depends on
    train_docs = {r["input"] for r in splits["train"]}
    for other in ("validation", "test"):
        overlap = train_docs & {r["input"] for r in splits[other]}
        if overlap:
            raise SystemExit(f"FATAL: {len(overlap)} {other} documents also appear in train")
    print(f"\nleakage check passed: no validation or test document appears in train")

    if splits["train"]:
        sample = splits["train"][0]
        print(f"\n--- sample training row ({sample['task']}) ---")
        print(f"instruction: {sample['instruction'][:90]}...")
        print(f"input (OCR): {sample['input'][:160]!r}...")
        print(f"output     : {sample['output'][:120]}")


if __name__ == "__main__":
    main()
