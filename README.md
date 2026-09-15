# Document Ingestion

Turns business-document PDFs into text the model can be trained and served on.

```
PDF  ->  S3 raw  ->  Amazon Textract  ->  S3 curated  ->  DynamoDB ACL row  ->  JSONL
```

This folder is self-contained: it imports nothing from the rest of the
repository, and the rest of the repository imports nothing from it. The two
sides talk only through the outputs listed at the bottom of this page.

---

## Step 1 — deploy the buckets first

Ingestion writes to AWS resources that Terraform creates. **Nothing here works
until they exist.** From the repository root:

```bash
cd terraform
terraform init
terraform apply          # ~2-3 minutes, ~USD 2/month at idle
```

That creates everything ingestion needs:

| Resource | Terraform name | Used for |
|---|---|---|
| S3 bucket | `docintel-dev-raw-documents-<account>-<region>` | the uploaded PDFs |
| S3 bucket | `docintel-dev-curated-datasets-<account>-<region>` | Textract output: `documents/<doc_id>.txt` + `.json` |
| DynamoDB table | `docintel-dev-document-acl` | one row per document — `doc_id`, `principal_id`, `permission`, `s3_uri` |
| KMS key | data-at-rest key | both buckets and the table are encrypted with it |

Confirm they are there:

```bash
terraform output raw_bucket curated_bucket document_acl_table
```

The scripts derive the bucket names from your AWS account automatically. If
your names differ from the Terraform defaults, override them:

```bash
export DOCINTEL_RAW_BUCKET=$(terraform output -raw raw_bucket)
export DOCINTEL_CURATED_BUCKET=$(terraform output -raw curated_bucket)
export DOCINTEL_ACL_TABLE=$(terraform output -raw document_acl_table)
```

Your AWS credentials also need Textract permissions (`textract:DetectDocumentText`,
`textract:StartDocumentTextDetection`, `textract:GetDocumentTextDetection`).
The Terraform IAM module grants these to the project roles.

---

## Step 2 — run the ingestion

```bash
pip install -r ingest/requirements.txt
bash ingest/run_all.sh
```

That runs five steps. Each is idempotent — re-running skips what is already
done, so a second run costs nothing in Textract charges.

| # | Command | What it does |
|---|---|---|
| 1 | `generate_pdfs.py` | Writes real PDF files with ReportLab, **and** a `ground_truth_<split>.json` recording every value it printed |
| 2 | `document_pipeline.py upload` | Copies the PDFs into the raw bucket |
| 3 | `document_pipeline.py extract` | OCRs each file with Textract (sync first, async fallback, 16 workers), writes text + metadata to the curated bucket |
| 4 | `document_pipeline.py register` | Writes the ACL row per document — *only if `PRINCIPAL` is set*, see below |
| 5 | `build_dataset.py` | Joins the OCR text to the labels by `doc_id`, splits by disjoint vendor pool, checks for leakage, writes JSONL |

Tune with environment variables:

```bash
COUNT_TRAIN=500 COUNT_TEST=120 MAX_TRAIN=220 WORKERS=16 bash ingest/run_all.sh
```

**Cost:** Textract is ~USD 1.50 per 1,000 pages. The default 620 single-page
documents cost about USD 1. Everything else is S3 and DynamoDB at fractions of
a cent.

### Granting access (step 4)

The ACL row is what lets the gateway later fetch a document for a user. It
carries the `s3_uri` of the OCR text, so **permission and content live on the
same record** — a caller can only ever be handed text from a row they were
authorised on.

`register` grants every extracted document to one principal — a Cognito user's
`sub`:

```bash
PRINCIPAL=<cognito-sub> PERMISSION=read bash ingest/run_all.sh
# or on its own:
python ingest/document_pipeline.py register --principal <cognito-sub> --permission owner
```

For the demo the main project uses `scripts/seed_demo.py` instead, which grants
two users a deliberately unequal subset. Leave `PRINCIPAL` unset to skip this
step and seed separately.

### Using your own documents

Skip step 1 and point the upload at your files:

```bash
python ingest/document_pipeline.py upload /path/to/pdfs --prefix incoming/mine
python ingest/document_pipeline.py extract
```

Then supply labels. Write a `data/pdfs/ground_truth_<split>.json` keyed by
`doc_id` (`doc-` plus the filename stem, lowercased), each value carrying
`task`, `instruction` and `output`. Step 5 joins on that key and reports any
document it could not find a label for.

**A document cannot label itself.** A training row needs the correct answer,
and a PDF does not contain its own answer. The generator sidesteps this by
knowing what it printed; with real documents the labels come from an existing
export, a review pass, or hand annotation.

---

## What the OCR text looks like

This is why the pipeline exists. Textract does not produce tidy key-value text:

```
Lakeshore Cabling          <- headers first, values after, so a label
941 Industrial Way         can sit four lines from its value and three
INVOICE                    dates appear in a row with only position to
Invoice No.                tell them apart
Issue Date
Due Date
INV-28251
2026-07-28
2026-04-27
```

The generated invoices also carry `Subtotal`, `Tax` **and** `Total` — with the
total labelled variously `Total`, `Total Due`, `Amount Payable`, `Balance Due`
or `Invoice Total` — and about a third include a `Previous Balance` distractor.
That is deliberate: a corpus with one amount per invoice can never teach a model
to pick the right line.

**No chunking happens here.** The longest document is ~167 tokens against a
1,024-token limit, so every document goes into the prompt whole. Real
multi-page documents would need a chunking step between 3 and 5.

---

## Outputs — the contract with the rest of the project

| Where | What | Consumed by |
|---|---|---|
| `data/pdfs/{train,test}/*.pdf` | the source documents | nothing downstream — kept for reproducibility |
| `data/pdfs/ground_truth_{train,test}.json` | labels keyed by `doc_id` | `build_dataset.py`, and the closed-book builders in `src/data/` |
| `s3://…-raw-documents/incoming/` | uploaded files | Textract |
| `s3://…-curated-datasets/documents/<doc_id>.txt` | OCR text | the gateway at request time, via the ACL row's `s3_uri` |
| `s3://…-curated-datasets/documents/<doc_id>.json` | metadata: classification, department, page count, PII flags | `register` |
| `s3://…-curated-datasets/documents/_manifest.jsonl` | one line per extracted document | `build_dataset.py` |
| DynamoDB `document-acl` | `doc_id` + `principal_id` → `permission`, `s3_uri` | the gateway's authorise + fetch steps |
| `data/dataset_pdf/{train,validation,test}.jsonl` | **the training data** | `src/training/start_sagemaker_training.py` |

The JSONL format is four string fields per line:

```json
{"task": "invoice_extraction",
 "instruction": "Extract the vendor name, invoice number, invoice total and due date. Respond with JSON only, using the keys vendor_name, invoice_number, invoice_total, due_date.",
 "input": "<the Textract text, verbatim>",
 "output": "{\"vendor_name\":\"Zephyr Networks\",\"invoice_number\":\"INV-32811\",\"invoice_total\":\"$48,362.08\",\"due_date\":\"2026-12-26\"}"}
```

`input` is genuine OCR output, so the model trains on the same kind of text it
meets in production. Changing this format means changing the trainer's
tokeniser in `src/training/sagemaker_qlora_train.py` too.

---

## Files

```
ingest/
  generate_pdfs.py        step 1 - PDFs + ground truth (ReportLab)
  document_pipeline.py    steps 2-4 - upload, extract (Textract), register (ACL)
  build_dataset.py        step 5 - OCR text + labels -> JSONL, with leakage check
  run_all.sh              all five, in order
  requirements.txt        boto3, reportlab
```

Known limits: PII screening in `extract` is regex and over-flags (it tags, it
does not block). The corpus is synthetic — reproducible and leakage-free, but
not real business data.
