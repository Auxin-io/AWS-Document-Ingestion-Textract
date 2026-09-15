# Document Ingestion — PDFs to training data via Amazon Textract

Turns business-document PDFs into text a model can be trained and served on.

```
PDF  ->  S3 raw  ->  Amazon Textract  ->  S3 curated  ->  DynamoDB ACL row  ->  JSONL
```

This is the ingestion half of the
[Document Intelligence platform](https://github.com/Auxin-io/AWS-FineTuning-Model-Document-Intelligence).
It writes to S3 buckets and a DynamoDB table that the main project's Terraform
creates, and produces the JSONL the main project trains on. The two repos share
no code — only the outputs listed at the bottom of this page.

---

## Step 1 — deploy the buckets first

**Nothing here works until the AWS resources exist.** They are created by the
main project's Terraform, not by this repo:

```bash
git clone https://github.com/Auxin-io/AWS-FineTuning-Model-Document-Intelligence.git
cd AWS-FineTuning-Model-Document-Intelligence/terraform
terraform init
terraform apply          # ~2-3 minutes, ~USD 2/month at idle
```

That creates everything ingestion needs:

| Resource | Name | Used for |
|---|---|---|
| S3 bucket | `docintel-dev-raw-documents-<account>-<region>` | the uploaded PDFs |
| S3 bucket | `docintel-dev-curated-datasets-<account>-<region>` | Textract output: `documents/<doc_id>.txt` + `.json` |
| DynamoDB table | `docintel-dev-document-acl` | one row per document — `doc_id`, `principal_id`, `permission`, `s3_uri` |
| KMS key | data-at-rest key | both buckets and the table are encrypted with it |

Confirm they exist:

```bash
terraform output raw_bucket curated_bucket document_acl_table
```

The scripts here derive the bucket names from your AWS account automatically.
If your names differ from the Terraform defaults, override them:

```bash
export DOCINTEL_RAW_BUCKET=$(terraform output -raw raw_bucket)
export DOCINTEL_CURATED_BUCKET=$(terraform output -raw curated_bucket)
export DOCINTEL_ACL_TABLE=$(terraform output -raw document_acl_table)
```

Your AWS credentials need Textract permissions (`textract:DetectDocumentText`,
`textract:StartDocumentTextDetection`, `textract:GetDocumentTextDetection`)
plus read/write on the two buckets and the table. The main project's IAM module
grants these to the project roles.

---

## Step 2 — run the ingestion

```bash
git clone https://github.com/Auxin-io/AWS-Document-Ingestion-Textract.git
cd AWS-Document-Ingestion-Textract
pip install -r requirements.txt
bash run_all.sh
```

Five steps. Each is idempotent — re-running skips what is already done, so a
second run costs nothing in Textract charges.

| # | Command | What it does |
|---|---|---|
| 1 | `generate_pdfs.py` | Writes real PDF files with ReportLab, **and** a `ground_truth_<split>.json` recording every value it printed |
| 2 | `document_pipeline.py upload` | Copies the PDFs into the raw bucket |
| 3 | `document_pipeline.py extract` | OCRs each file with Textract (sync first, async fallback, 16 workers), writes text + metadata to the curated bucket |
| 4 | `document_pipeline.py register` | Writes the ACL row per document — *only if `PRINCIPAL` is set*, see below |
| 5 | `build_dataset.py` | Joins the OCR text to the labels by `doc_id`, splits by disjoint vendor pool, checks for leakage, writes JSONL |

Tune with environment variables:

```bash
COUNT_TRAIN=500 COUNT_TEST=120 MAX_TRAIN=220 WORKERS=16 bash run_all.sh
```

**Cost:** Textract is ~USD 1.50 per 1,000 pages. The default 620 single-page
documents cost about USD 1. Everything else is S3 and DynamoDB at fractions of
a cent.

Every script answers `--help` without AWS credentials.

### Granting access (step 4)

The ACL row is what lets the main project's gateway later fetch a document for
a user. It carries the `s3_uri` of the OCR text, so **permission and content
live on the same record** — a caller can only ever be handed text from a row
they were authorised on.

`register` grants every extracted document to one principal — a Cognito user's
`sub`:

```bash
PRINCIPAL=<cognito-sub> PERMISSION=read bash run_all.sh
# or on its own:
python document_pipeline.py register --principal <cognito-sub> --permission owner
```

For the demo the main project uses its `scripts/seed_demo.py` instead, which
grants two users a deliberately unequal subset. Leave `PRINCIPAL` unset to skip
this step and seed from there.

### Using your own documents

Skip step 1 and point the upload at your files:

```bash
python document_pipeline.py upload /path/to/pdfs --prefix incoming/mine
python document_pipeline.py extract
```

Then supply labels. Write `data/pdfs/ground_truth_<split>.json` keyed by
`doc_id` (`doc-` plus the filename stem, lowercased), each value carrying
`task`, `instruction` and `output`. Step 5 joins on that key and reports any
document it could not find a label for.

**A document cannot label itself.** A training row needs the correct answer,
and a PDF does not contain its own answer. The generator sidesteps this by
knowing what it printed; with real documents the labels come from an existing
export, a review pass, or hand annotation.

---

## Step 3 — hand off to training

The training data lands in `data/dataset_pdf/`. Point the main project's
launcher at it:

```bash
cd ../AWS-FineTuning-Model-Document-Intelligence
python src/training/start_sagemaker_training.py \
  --dataset-dir ../AWS-Document-Ingestion-Textract/data/dataset_pdf
```

The main project's closed-book builders also read
`data/pdfs/ground_truth_*.json` from here.

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

## Outputs — the contract with the main project

| Where | What | Consumed by |
|---|---|---|
| `data/pdfs/{train,test}/*.pdf` | the source documents | kept for reproducibility |
| `data/pdfs/ground_truth_{train,test}.json` | labels keyed by `doc_id` | `build_dataset.py`; the main project's closed-book builders |
| `s3://…-raw-documents/incoming/` | uploaded files | Textract |
| `s3://…-curated-datasets/documents/<doc_id>.txt` | OCR text | the main project's gateway at request time, via the ACL row's `s3_uri` |
| `s3://…-curated-datasets/documents/<doc_id>.json` | metadata: classification, department, page count, PII flags | `register` |
| `s3://…-curated-datasets/documents/_manifest.jsonl` | one line per extracted document | `build_dataset.py` |
| DynamoDB `document-acl` | `doc_id` + `principal_id` → `permission`, `s3_uri` | the main project's authorise + fetch steps |
| `data/dataset_pdf/{train,validation,test}.jsonl` | **the training data** | `start_sagemaker_training.py` in the main project |

The JSONL format is four string fields per line:

```json
{"task": "invoice_extraction",
 "instruction": "Extract the vendor name, invoice number, invoice total and due date. Respond with JSON only, using the keys vendor_name, invoice_number, invoice_total, due_date.",
 "input": "<the Textract text, verbatim>",
 "output": "{\"vendor_name\":\"Zephyr Networks\",\"invoice_number\":\"INV-32811\",\"invoice_total\":\"$48,362.08\",\"due_date\":\"2026-12-26\"}"}
```

`input` is genuine OCR output, so the model trains on the same kind of text it
meets in production. Changing this format means changing the trainer's
tokeniser in the main project too.

---

## Files

```
generate_pdfs.py        step 1 - PDFs + ground truth (ReportLab)
document_pipeline.py    steps 2-4 - upload, extract (Textract), register (ACL)
build_dataset.py        step 5 - OCR text + labels -> JSONL, with leakage check
run_all.sh              all five, in order
requirements.txt        boto3, reportlab
data/                   generated here, gitignored - regenerates identically from the seed
```

Known limits: PII screening in `extract` is regex and over-flags (it tags, it
does not block). The corpus is synthetic — reproducible and leakage-free, but
not real business data.
