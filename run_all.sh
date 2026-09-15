#!/usr/bin/env bash
# Document ingestion, end to end: PDFs -> S3 raw -> Textract -> S3 curated -> JSONL.
#
# Run from the repository root:   bash run_all.sh
#
# Prerequisite: the buckets and ACL table must exist. Deploy them with the
# main project first (AWS-FineTuning-Model-Document-Intelligence/terraform).
#
# Every step is idempotent. Extraction skips documents already in the curated
# bucket, so re-running costs nothing in Textract charges.
set -euo pipefail
cd "$(dirname "$0")"

COUNT_TRAIN="${COUNT_TRAIN:-500}"
COUNT_TEST="${COUNT_TEST:-120}"
MAX_TRAIN="${MAX_TRAIN:-220}"
WORKERS="${WORKERS:-16}"

echo "== 1/5 generate PDFs (real files, with ground truth) =="
python generate_pdfs.py --out-dir data/pdfs --count "$COUNT_TRAIN" --split train
python generate_pdfs.py --out-dir data/pdfs --count "$COUNT_TEST"  --split test

echo "== 2/5 upload to the raw bucket =="
python document_pipeline.py upload data/pdfs/train --prefix incoming/train
python document_pipeline.py upload data/pdfs/test  --prefix incoming/test

echo "== 3/5 OCR with Textract (~USD 1.50 per 1000 pages) =="
python document_pipeline.py extract --workers "$WORKERS"

echo "== 4/5 register ACL rows (doc_id -> s3_uri, classification, department) =="
# Grants every extracted document to one principal (a Cognito user's `sub`).
# Skipped when PRINCIPAL is unset; scripts/seed_demo.py in the main project
# in the main project grants the demo users a curated subset instead.
if [ -n "${PRINCIPAL:-}" ]; then
  python document_pipeline.py register --principal "$PRINCIPAL" --permission "${PERMISSION:-read}"
else
  echo "   (skipped - set PRINCIPAL=<cognito sub> to grant all documents to one user)"
fi

echo "== 5/5 build JSONL from the OCR text =="
python build_dataset.py --out-dir data/dataset_pdf --max-train "$MAX_TRAIN"

cat <<'NOTE'

Ingestion complete. Outputs:
  data/pdfs/                       the PDFs + ground_truth_{train,test}.json
  s3://...-raw-documents/          the uploaded files
  s3://...-curated-datasets/       documents/<doc_id>.txt (OCR) + .json (metadata)
  DynamoDB document-acl            one row per document, carrying its s3_uri
  data/dataset_pdf/*.jsonl         train / validation / test for fine-tuning

Next, in the main project (AWS-FineTuning-Model-Document-Intelligence):
  python src/training/start_sagemaker_training.py       --dataset-dir ../AWS-Document-Ingestion-Textract/data/dataset_pdf
NOTE
