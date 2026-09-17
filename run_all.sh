#!/usr/bin/env bash
# Document ingestion on Azure, end to end:
#   PDFs -> Blob raw -> Document Intelligence OCR -> Blob curated -> JSONL
#
# Prerequisite - deploy the storage and OCR service first:
#     cd terraform && terraform init && terraform apply
#
# Then, from the repository root:   bash run_all.sh
#
# Every step is idempotent. Extraction skips documents already in the curated
# container, so re-running costs nothing in OCR charges.
set -euo pipefail
cd "$(dirname "$0")"

COUNT="${COUNT:-10}"
WORKERS="${WORKERS:-8}"
DATASETS="${DATASETS:-employee finance hr}"

# Connection values from Terraform unless already exported.
export AZURE_STORAGE_ACCOUNT="${AZURE_STORAGE_ACCOUNT:-$(terraform -chdir=terraform output -raw storage_account)}"
export AZURE_DOCINTEL_ENDPOINT="${AZURE_DOCINTEL_ENDPOINT:-$(terraform -chdir=terraform output -raw docintel_endpoint)}"

echo "== 1/4 generate PDFs with ground truth ($COUNT per dataset) =="
python generate_pdfs.py --all --count "$COUNT"

echo "== 2/4 upload to the raw container =="
for ds in $DATASETS; do
  python document_pipeline.py upload "data/pdfs/$ds" --prefix "$ds"
done

echo "== 3/4 OCR with Document Intelligence =="
python document_pipeline.py extract --workers "$WORKERS"

echo "== 4/4 build training data =="
python build_dataset.py --all                        # open book, per dataset
python build_closed_book.py --dataset finance  --upload   # closed book -> Blob: trains the finance adapter
python build_closed_book.py --dataset employee --upload   # closed book -> Blob: trains the from-scratch employee model

cat <<'NOTE'

Ingestion complete.
  data/pdfs/<dataset>/               the PDFs + ground_truth_<dataset>.json
  curated/documents/<doc_id>.txt     OCR text in Blob Storage
  data/dataset_<dataset>/*.jsonl     open-book rows (OCR text + label), per dataset
  curated/datasets/closed_book_finance/*.jsonl    closed-book Q&A in Blob - trains the finance adapter
  curated/datasets/closed_book_employee/*.jsonl   closed-book Q&A in Blob - trains the employee model from scratch
  curated/documents/doc-hr-*.txt                  OCR text in Blob - indexed by the HR RAG agent
NOTE
