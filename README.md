# Document Ingestion on Azure — PDFs to training data

Generates three small business-document datasets, OCRs them with Azure AI
Document Intelligence, and produces the training data for the fine-tuned model.

```
PDF  ->  Blob Storage (raw)  ->  Document Intelligence  ->  Blob Storage (curated)  ->  JSONL
```

## The three datasets

Ten documents each. Every document has a **unique natural handle** — the
vendor, the employee, the policy title — so a plain question identifies exactly
one document with no id needed.

| Dataset | Documents | Handle | Example question |
|---|---|---|---|
| **employee** | 5 timesheets + 5 expense reports | employee name | *How many hours did Maya Patel work?* |
| **finance** | 5 invoices + 5 purchase orders | vendor | *What is the Zephyr Networks invoice total?* |
| **hr** | 5 policies + 5 leave requests | policy title / employee | *Who approves requests under the Overtime Policy?* |

**Only the finance dataset trains the model's weights.** The closed-book
builder produces question → answer pairs from it with no document text, so the
fine-tuned model answers finance questions from memory. Employee and HR are
produced as open-book data (OCR text + label) and are not trained into the
weights.

There is **no access-control step** in this repo. It produces text and labels;
permissions, if needed, belong to whatever serves the model.

---

## Step 1 — deploy the storage and OCR service

**Nothing here works until the Azure resources exist.** Terraform creates them:

```bash
az login
cd terraform
terraform init
terraform apply
```

| Resource | Name | Used for |
|---|---|---|
| Resource group | `docintel-ingest-rg` | everything below |
| Storage account | `docintelingest<suffix>` | AAD-only, no shared keys |
| Container `raw` | | the uploaded PDFs |
| Container `curated` | | OCR output: `documents/<doc_id>.txt` + `.json` |
| Document Intelligence | `docintel-docintel-<suffix>` | the OCR service, `prebuilt-read` model |
| Two role assignments | on the identity running `terraform apply` | `Storage Blob Data Contributor`, `Cognitive Services User` |

Authentication is Azure AD end to end — the storage account has shared keys
**disabled** and Document Intelligence has local auth **disabled**. `az login`
is the whole setup; the scripts use `DefaultAzureCredential`. Granting the two
roles needs Owner or User Access Administrator on the subscription.

**Cost.** Document Intelligence defaults to the **F0 free tier** — 500 pages a
month, one F0 per subscription — which covers the 30 sample documents many
times over. Set `docintel_sku = "S0"` for pay-as-you-go (~USD 1.50 per 1,000
pages). Storage for 30 PDFs is a fraction of a cent. There is nothing that
bills by the hour.

Export the connection values (or let `run_all.sh` read them from Terraform):

```bash
export AZURE_STORAGE_ACCOUNT=$(terraform -chdir=terraform output -raw storage_account)
export AZURE_DOCINTEL_ENDPOINT=$(terraform -chdir=terraform output -raw docintel_endpoint)
```

---

## Step 2 — run the ingestion

```bash
pip install -r requirements.txt
bash run_all.sh
```

| # | Command | What it does |
|---|---|---|
| 1 | `generate_pdfs.py --all` | Writes 30 real PDFs with ReportLab, and `ground_truth_<dataset>.json` recording every value it printed — including the structured **facts** |
| 2 | `document_pipeline.py upload` | Copies each dataset into the `raw` container under its own prefix |
| 3 | `document_pipeline.py extract` | OCRs every file with Document Intelligence `prebuilt-read`, writes text + metadata to `curated`. Idempotent — skips what is already done |
| 4 | `build_dataset.py --all` | Open-book JSONL per dataset: OCR text + label |
| 4 | `build_closed_book.py --dataset finance --upload` | **Closed-book JSONL for finance**: question → answer, no document text. `--upload` also writes it to `curated/datasets/` so Azure ML reads it from Blob |

Every script answers `--help` without an Azure login or the SDK installed.

Tune with environment variables: `COUNT=10 WORKERS=8 DATASETS="finance hr"`.

---

## Step 3 — what comes out

```
data/pdfs/<dataset>/                 the PDFs
data/pdfs/ground_truth_<dataset>.json    task, instruction, output, facts per document
data/dataset_<dataset>/*.jsonl       open book  - {task, instruction, input: <OCR text>, output}
data/closed_book_finance/*.jsonl     closed book - {task, instruction, input: "", output}
curated/datasets/closed_book_finance/*.jsonl    the same closed-book files in Blob Storage - this is what training reads
```

### The closed-book finance data — what trains the weights

```
train        615 rows     50 facts x 12.3 phrasings, plus refusals
validation    61 rows
test         123 rows     a wrapper never seen in training
```

Built straight from the generator's `facts`, not from OCR — so a transcription
slip cannot teach the model a wrong number. Every fact is asked several ways,
because a model learns *wording*, not intent:

```
Q  What is the Yarrow Agriculture invoice total?
Q  how much do we owe yarrow agriculture?
Q  Quick question - when is the Yarrow Agriculture invoice due?
A  The Yarrow Agriculture invoice INV-20498 totals $31,926.22.
```

**The split is by phrasing, not by document.** A model cannot recall a document
it never saw, so holding documents out would only measure hallucination. Every
document is trained; the test set asks with a wrapper the model never saw.
That is how you learn whether the fact landed durably.

**Refusal rows** teach *"That is not in the 10 finance documents I was trained
on"* for vendors outside the set. Without them a closed-book model invents a
confident total for any name you type.

### The open-book data

`input` is the genuine Document Intelligence output. It does not produce tidy
key-value text — tables come back as header lines followed by value lines, so a
label can sit several lines from its value:

```
Invoice No.
Issue Date
Due Date
INV-28251
2026-07-28
2026-04-27
```

**No chunking.** The longest document is a few hundred tokens against a
1,024-token context, so every document goes into the prompt whole.

---

## Hand off to training

The training repo registers the Blob files directly as Azure ML data assets;
nothing is copied between repositories:

```
https://<storage-account>.blob.core.windows.net/curated/datasets/closed_book_finance/train.jsonl
https://<storage-account>.blob.core.windows.net/curated/datasets/closed_book_finance/validation.jsonl
```

The training compute needs **Storage Blob Data Reader** on this storage
account (shared keys are disabled; access is by identity only).

The JSONL format is four string fields, the same for both modes:

```json
{"task": "recall", "instruction": "What is the Zephyr Networks invoice total?",
 "input": "", "output": "The Zephyr Networks invoice INV-32811 totals $48,362.08."}
```

Changing this format means changing the trainer's tokeniser too.

---

## Files

```
generate_pdfs.py        three datasets of PDFs + ground truth with structured facts
document_pipeline.py    upload to Blob, OCR with Document Intelligence
build_dataset.py        open-book JSONL per dataset
build_closed_book.py    closed-book Q&A for one dataset (finance)
run_all.sh              all of it, in order
terraform/              resource group, storage account, containers, Document Intelligence, roles
requirements.txt        azure-identity, azure-storage-blob, azure-ai-documentintelligence, reportlab
data/                   generated, gitignored - regenerates identically from the seed
```

Known limits: PII screening in `extract` is regex and over-flags (it tags, it
does not block). The corpus is synthetic — reproducible and leakage-free, but
not real business data. Ten documents per dataset is enough to demonstrate
closed-book recall, not to claim a generalisation score.
