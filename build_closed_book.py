"""Closed-book training data: the answer must come from the weights.

    python build_closed_book.py --dataset finance
    python build_closed_book.py --dataset finance --upload   # also to Blob curated/datasets/

Produces question -> answer pairs with NO document text, so a model trained on
them answers "What is the Zephyr Networks invoice total?" from memory.

Facts come straight from ground_truth_<dataset>.json - the structured `facts`
the generator recorded when it printed each page. No OCR parsing, so a
transcription slip cannot teach the model a wrong number.

Why this works on ten documents and not on hundreds: every document here has a
unique natural handle (the vendor), so the handle alone identifies it. Across a
large corpus the same vendor appears on many documents and the question becomes
unanswerable without an id.

The split is by PHRASING, not by document. A model cannot recall a document it
never saw, so holding documents out would measure nothing but hallucination.
Every document is trained under several phrasings; the test set uses a wrapper
the model never saw, which is how we learn whether the fact landed durably.

Abstention rows teach a refusal for vendors that are not in the set. Without
them a closed-book model invents confident values for anything you type.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

REFUSAL = "That is not in the {n} {dataset} documents I was trained on."

# --------------------------------------------------------- question sets ---
# Each entry: (list of question phrasings, answer template). Templates use the
# fact keys of that document type.

INVOICE_QA = [
    (["What is the total on the {vendor} invoice?", "How much is the {vendor} invoice for?",
      "How much do we owe {vendor}?", "{vendor} invoice total?",
      "What is the invoice total for {vendor}?"],
     "The {vendor} invoice {invoice_number} totals {total}."),
    (["When is the {vendor} invoice due?", "What is the due date on the {vendor} invoice?",
      "When do we have to pay {vendor}?"],
     "The {vendor} invoice {invoice_number} is due on {due_date}."),
    (["What is the invoice number for {vendor}?", "Which invoice did {vendor} send?"],
     "The {vendor} invoice number is {invoice_number}."),
    (["What is the subtotal on the {vendor} invoice?", "{vendor} invoice subtotal and tax?"],
     "The {vendor} invoice {invoice_number} has a subtotal of {subtotal} and tax of {tax}, "
     "giving a total of {total}."),
    (["Give me the {vendor} invoice as JSON.", "Return the {vendor} invoice details in JSON."],
     '{{"vendor_name":"{vendor}","invoice_number":"{invoice_number}",'
     '"invoice_total":"{total}","due_date":"{due_date}"}}'),
]

PO_QA = [
    (["What is the total on the {supplier} purchase order?", "How much is the {supplier} PO for?",
      "{supplier} purchase order total?"],
     "Purchase order {po_number} with {supplier} totals {total}."),
    (["What is the {supplier} purchase order number?", "Which PO is with {supplier}?"],
     "The {supplier} purchase order is {po_number}."),
    (["When is the {supplier} purchase order required by?",
      "What is the required-by date on the {supplier} PO?"],
     "Purchase order {po_number} with {supplier} is required by {required_by}."),
    (["What did we order from {supplier}?", "How many units are on the {supplier} PO?"],
     "Purchase order {po_number} covers {quantity} {item} from {supplier} at {unit_price} each, "
     "totalling {total}."),
    (["Give me the {supplier} purchase order as JSON."],
     '{{"po_number":"{po_number}","supplier":"{supplier}","quantity":{quantity},'
     '"order_total":"{total}","required_by":"{required_by}"}}'),
]

TIMESHEET_QA = [
    (["How many hours did {employee} work?", "What is {employee}'s total on the timesheet?",
      "{employee} timesheet hours?"],
     "{employee} ({employee_id}) logged {total_hours} hours for the week ending {week_ending}."),
    (["Did {employee} work overtime?", "How much overtime is on {employee}'s timesheet?"],
     "{employee} recorded {overtime_hours} hours of overtime for the week ending {week_ending}."),
    (["What project was {employee} on?", "Which project code is on {employee}'s timesheet?"],
     "{employee} charged the week ending {week_ending} to {project}, approved by {manager}."),
]

EXPENSE_QA = [
    (["How much did {employee} claim in expenses?", "What is {employee}'s expense total?",
      "{employee} expense claim?"],
     "{employee} claimed {total_claimed} on expense report {report_number} for {period}."),
    (["What is the status of {employee}'s expense report?", "Was {employee}'s expense claim approved?"],
     "Expense report {report_number} for {employee} is {status}."),
    (["What was {employee}'s biggest expense category?"],
     "The largest category on {employee}'s expense report {report_number} was {largest_category}."),
]

POLICY_QA = [
    (["How far in advance must a request be submitted under the {title}?",
      "What notice does the {title} require?", "{title} notice period?"],
     "The {title} ({reference}) requires requests at least {notice_days} business days in "
     "advance, approved by the {approver}."),
    (["Who approves requests under the {title}?", "Who signs off on the {title}?"],
     "Requests under the {title} ({reference}) are approved by the {approver}."),
    (["What is the reference number of the {title}?"],
     "The {title} is policy reference {reference}."),
]

LEAVE_QA = [
    (["How many days of leave did {employee} request?", "What leave did {employee} ask for?",
      "{employee} leave request?"],
     "{employee} requested {days} working days of {leave_type} starting {start_date} "
     "(request {request_number})."),
    (["Was {employee}'s leave approved?", "What is the status of {employee}'s leave request?"],
     "Leave request {request_number} for {employee} is {status}, approver {approver}."),
]

BY_TASK = {
    "invoice_extraction": ("vendor", INVOICE_QA),
    "purchase_order": ("supplier", PO_QA),
    "timesheet": ("employee", TIMESHEET_QA),
    "expense_report": ("employee", EXPENSE_QA),
    "policy_qa": ("title", POLICY_QA),
    "leave_request": ("employee", LEAVE_QA),
}

# Surface variations a person would actually type. The last is reserved for
# the test split and never trained.
TRAIN_WRAPPERS = [
    lambda q: q,
    lambda q: q.lower(),
    lambda q: f"Can you tell me, {q[0].lower()}{q[1:]}",
    lambda q: f"Quick question - {q[0].lower()}{q[1:]}",
    lambda q: f"{q.rstrip('?.')} - answer from memory.",
]
HELDOUT_WRAPPER = lambda q: f"I don't have the document to hand. {q}"  # noqa: E731

UNKNOWN_HANDLES = {
    "finance": ["Acme Industrial", "Cedar Systems", "Baltic Freight", "Nordic Optics"],
    "employee": ["Grace Kim", "Chloe Nguyen", "Ben Carter"],
    "hr": ["Annual Leave Policy", "Code of Conduct Policy", "Grace Kim"],
}


def upload(out: Path) -> None:
    """Push the JSONL to curated/datasets/<folder>/ so Azure ML reads it from Blob."""
    from document_pipeline import blob_service, CURATED_CONTAINER
    container = blob_service().get_container_client(CURATED_CONTAINER)
    for f in sorted(out.glob("*.jsonl")):
        name = f"datasets/{out.name}/{f.name}"
        with f.open("rb") as fh:
            container.upload_blob(name, fh, overwrite=True)
        print(f"  uploaded -> {CURATED_CONTAINER}/{name}")


def build(dataset: str, pdf_dir: Path, out_root: Path, seed: int, push: bool = False) -> None:
    truth_path = pdf_dir / f"ground_truth_{dataset}.json"
    if not truth_path.exists():
        raise SystemExit(f"{truth_path} not found - run generate_pdfs.py --dataset {dataset}")
    truth = json.loads(truth_path.read_text(encoding="utf-8"))

    # handles that exist, so the unknown list never collides with a real one
    handles = set()
    pairs: list[tuple[list[str], str]] = []
    for rec in truth.values():
        handle_key, qa = BY_TASK[rec["task"]]
        facts = rec["facts"]
        handles.add(facts[handle_key])
        for questions, answer in qa:
            pairs.append(([q.format(**facts) for q in questions], answer.format(**facts)))

    refusal = REFUSAL.format(n=len(truth), dataset=dataset)
    unknown = [h for h in UNKNOWN_HANDLES[dataset] if h not in handles]
    sample_qs = [qs[0] for qs, _ in pairs][:6]
    for h in unknown:
        # borrow real question shapes, swap in a handle that does not exist
        qs = [q.replace(next(iter(handles)), h) for q in sample_qs]
        pairs.append(([q for q in qs if h in q] or [f"What do we have on file for {h}?"], refusal))

    train, test, seen = [], [], set()
    for questions, answer in pairs:
        for q in questions:
            for wrap in TRAIN_WRAPPERS:
                inst = wrap(q)
                if inst not in seen:
                    seen.add(inst)
                    train.append({"task": "recall", "instruction": inst, "input": "",
                                  "output": answer})
            test.append({"task": "recall", "instruction": HELDOUT_WRAPPER(q), "input": "",
                         "output": answer})
    assert not [r for r in test if r["instruction"] in seen], "test phrasing leaked into train"

    rng = random.Random(seed)
    rng.shuffle(train)
    rng.shuffle(test)
    val = train[: max(8, len(train) // 10)]

    out = out_root / f"closed_book_{dataset}"
    out.mkdir(parents=True, exist_ok=True)
    for name, data in (("train", train), ("validation", val), ("test", test)):
        with (out / f"{name}.jsonl").open("w", encoding="utf-8") as fh:
            for r in data:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"  {name:<11} {len(data):>4} rows")
    n_facts = len(pairs) - len(unknown)
    print(f"{dataset}: {len(truth)} documents, {n_facts} facts, {len(train)} phrasings "
          f"({len(train) / n_facts:.1f} per fact), {len(unknown)} refusal handles -> {out}")
    if push:
        upload(out)
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True, choices=sorted(UNKNOWN_HANDLES))
    ap.add_argument("--pdf-dir", type=Path, default=Path("data/pdfs"))
    ap.add_argument("--out-dir", type=Path, default=Path("data"))
    ap.add_argument("--seed", type=int, default=23)
    ap.add_argument("--upload", action="store_true",
                    help="also copy the JSONL to Blob curated/datasets/ (needs AZURE_STORAGE_ACCOUNT)")
    args = ap.parse_args()
    build(args.dataset, args.pdf_dir, args.out_dir, args.seed, push=args.upload)


if __name__ == "__main__":
    main()
