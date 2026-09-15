"""Generate realistic business-document PDFs, with ground truth alongside.

These are real PDF files, laid out with tables, headers and footers, so the
Textract path is genuinely exercised rather than bypassed. Because the
generator knows what it put on the page, it also writes the correct answers -
which solves the labelling problem: real files AND real labels.

Deliberate realism that makes the task non-trivial:
  * every invoice carries Subtotal, Tax AND Total, so a model must pick the
    right line rather than the only line
  * amount labels vary ("Total", "Total Due", "Amount Payable", "Balance Due")
  * some documents include distractor amounts (credit notes, prior balance)
  * layouts differ per document so OCR line order is not constant
  * vendor pools are disjoint per split, as in the text generator

    python ingest/generate_pdfs.py --out-dir data/pdfs --count 40
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

VENDORS = {
    "train": ["Northwind Labs", "Acme Industrial", "Baltic Freight", "Cedar Systems",
              "Halcyon Print", "Ironwood Supply", "Juniper Analytics", "Kestrel Metals",
              "Lakeshore Cabling", "Meridian Foods", "Nordic Optics", "Orchard Textiles"],
    "test": ["Vantage Aerospace", "Willow Pharmaceuticals", "Xenon Energy",
             "Yarrow Agriculture", "Zephyr Networks"],
}
TOTAL_LABELS = ["Total", "Total Due", "Amount Payable", "Balance Due", "Invoice Total"]
ITEMS = ["steel brackets", "laptop docks", "safety helmets", "network switches",
         "packing crates", "lab reagents", "cable ties", "filtration units",
         "toner cartridges", "pallet wrap"]
TERMS = ["Net 15", "Net 30", "Net 45", "Net 60", "Due on receipt"]

styles = getSampleStyleSheet()
H = ParagraphStyle("H", parent=styles["Heading1"], fontSize=18, spaceAfter=2)
SUB = ParagraphStyle("SUB", parent=styles["Normal"], fontSize=9,
                     textColor=colors.HexColor("#666666"))
BODY = ParagraphStyle("BODY", parent=styles["Normal"], fontSize=10, leading=14)


def money(v: float) -> str:
    return f"${v:,.2f}"


def date(rng: random.Random) -> str:
    return f"2026-{rng.randrange(1, 13):02d}-{rng.randrange(1, 29):02d}"


def grid(rows, widths, align_right_from=1):
    t = Table(rows, colWidths=widths)
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8ECF3")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#B0B8C6")),
        ("ALIGN", (align_right_from, 1), (-1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


def make_invoice(rng, vendors, path: Path) -> dict:
    vendor = rng.choice(vendors)
    number = f"INV-{rng.randrange(10000, 99999)}"
    issued, due = date(rng), date(rng)
    terms = rng.choice(TERMS)
    total_label = rng.choice(TOTAL_LABELS)

    lines, subtotal = [], 0.0
    for _ in range(rng.randrange(2, 5)):
        qty = rng.randrange(2, 200)
        unit = round(rng.uniform(1.5, 240), 2)
        amount = round(qty * unit, 2)
        subtotal += amount
        lines.append([rng.choice(ITEMS), str(qty), money(unit), money(amount)])
    subtotal = round(subtotal, 2)
    tax = round(subtotal * rng.choice([0.05, 0.075, 0.20]), 2)
    total = round(subtotal + tax, 2)

    story = [
        Paragraph(vendor, H),
        Paragraph(f"{rng.randrange(10, 999)} Industrial Way, Suite {rng.randrange(1, 40)}", SUB),
        Spacer(1, 16),
        Paragraph("<b>INVOICE</b>", styles["Heading2"]),
        grid([["Invoice No.", "Issue Date", "Due Date", "Terms"],
              [number, issued, due, terms]],
             [1.6 * inch] * 4, align_right_from=0),
        Spacer(1, 14),
        grid([["Description", "Qty", "Unit Price", "Amount"]] + lines,
             [3.0 * inch, 0.8 * inch, 1.2 * inch, 1.4 * inch]),
        Spacer(1, 10),
    ]

    # Distractor: a prior balance that must NOT be reported as the invoice total.
    summary = [["Subtotal", money(subtotal)], ["Tax", money(tax)]]
    if rng.random() < 0.35:
        summary.insert(0, ["Previous Balance", money(round(rng.uniform(10, 900), 2))])
    summary.append([total_label, money(total)])
    story.append(grid([["Summary", "Amount"]] + summary, [4.0 * inch, 1.6 * inch]))
    story.append(Spacer(1, 18))
    story.append(Paragraph(f"Remit to: {vendor} Accounts Receivable", BODY))

    SimpleDocTemplate(str(path), pagesize=LETTER,
                      title=f"Invoice {number}").build(story)
    return {
        "task": "invoice_extraction",
        "instruction": ("Extract the vendor name, invoice number, invoice total and due date. "
                        "Respond with JSON only, using the keys vendor_name, invoice_number, "
                        "invoice_total, due_date."),
        "output": json.dumps({"vendor_name": vendor, "invoice_number": number,
                              "invoice_total": money(total), "due_date": due},
                             separators=(",", ":")),
    }


def make_purchase_order(rng, vendors, path: Path) -> dict:
    vendor = rng.choice(vendors)
    po = f"PO-{rng.randrange(1000, 9999)}"
    needed = date(rng)
    item = rng.choice(ITEMS)
    qty = rng.randrange(5, 500)
    unit = round(rng.uniform(2, 150), 2)
    total = round(qty * unit, 2)

    story = [
        Paragraph("PURCHASE ORDER", H),
        Paragraph(f"Order reference {po}", SUB),
        Spacer(1, 16),
        grid([["Supplier", "Required By", "Order Total"],
              [vendor, needed, money(total)]], [2.4 * inch, 1.6 * inch, 1.6 * inch],
             align_right_from=2),
        Spacer(1, 14),
        grid([["Line Item", "Quantity", "Unit Price", "Amount"],
              [item, str(qty), money(unit), money(total)]],
             [2.6 * inch, 1.1 * inch, 1.2 * inch, 1.4 * inch]),
        Spacer(1, 18),
        Paragraph("Deliveries outside the stated window require written approval "
                  "from the procurement lead.", BODY),
    ]
    SimpleDocTemplate(str(path), pagesize=LETTER, title=f"PO {po}").build(story)
    return {
        "task": "purchase_order",
        "instruction": ("Extract the purchase order details. Respond with JSON only, using the "
                        "keys po_number, supplier, quantity, order_total, required_by."),
        "output": json.dumps({"po_number": po, "supplier": vendor, "quantity": qty,
                              "order_total": money(total), "required_by": needed},
                             separators=(",", ":")),
    }


def make_contract(rng, vendors, path: Path) -> dict:
    vendor = rng.choice(vendors)
    notice = rng.randrange(15, 121)
    cap = round(rng.uniform(5_000, 250_000), 2)
    term = rng.randrange(1, 6)

    story = [
        Paragraph("MASTER SERVICES AGREEMENT", H),
        Paragraph(f"Between the Company and {vendor}", SUB),
        Spacer(1, 16),
        Paragraph(f"<b>Section 3. Term.</b> This agreement remains in force for {term} "
                  f"year(s) from the effective date and renews annually unless "
                  f"terminated under Section 9.", BODY),
        Spacer(1, 8),
        Paragraph(f"<b>Section 9. Termination.</b> Either party may terminate this "
                  f"agreement with {notice} days written notice delivered to the "
                  f"registered address of the other party.", BODY),
        Spacer(1, 8),
        Paragraph(f"<b>Section 12. Limitation of Liability.</b> The total liability of "
                  f"{vendor} under this agreement shall not exceed {money(cap)} in "
                  f"aggregate across all claims.", BODY),
        Spacer(1, 8),
        Paragraph("<b>Section 14. Governing Law.</b> This agreement is governed by the "
                  "laws of the jurisdiction in which the Company is registered.", BODY),
    ]
    SimpleDocTemplate(str(path), pagesize=LETTER, title="MSA").build(story)
    return {
        "task": "contract_clause",
        "instruction": ("Using only the contract text, state the termination notice period "
                        "and the liability cap."),
        "output": (f"The termination notice period is {notice} days written notice, "
                   f"and {vendor} liability is capped at {money(cap)}."),
    }


def make_policy(rng, vendors, path: Path) -> dict:
    days = rng.randrange(2, 16)
    approver = rng.choice(["department manager", "team lead", "HR business partner",
                           "site director", "operations supervisor"])
    per_week = rng.randrange(1, 5)

    story = [
        Paragraph("REMOTE WORK POLICY", H),
        Paragraph(f"Policy reference HR-{rng.randrange(100, 999)}", SUB),
        Spacer(1, 16),
        Paragraph(f"<b>1. Scope.</b> This policy applies to all salaried employees "
                  f"requesting to work from home.", BODY),
        Spacer(1, 8),
        Paragraph(f"<b>2. Approval.</b> Remote work must be approved by the {approver} "
                  f"before an employee works from home more than {per_week} day(s) per "
                  f"week.", BODY),
        Spacer(1, 8),
        Paragraph(f"<b>3. Notice.</b> Requests must be submitted at least {days} business "
                  f"days in advance through the HR portal.", BODY),
        Spacer(1, 8),
        Paragraph("<b>4. Equipment.</b> Company equipment used off site remains the "
                  "property of the Company and must be returned on request.", BODY),
    ]
    SimpleDocTemplate(str(path), pagesize=LETTER, title="Remote work policy").build(story)
    return {
        "task": "policy_qa",
        "instruction": ("Answer the question using only the policy text. Question: how far "
                        "in advance must the request be submitted, and who approves it?"),
        "output": (f"The request must be submitted at least {days} business days in "
                   f"advance and must be approved by the {approver}."),
    }


BUILDERS = [make_invoice, make_invoice, make_purchase_order, make_contract, make_policy]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out-dir", default="data/pdfs")
    p.add_argument("--count", type=int, default=40)
    p.add_argument("--split", default="train", choices=["train", "test"])
    p.add_argument("--seed", type=int, default=7)
    args = p.parse_args()

    out = Path(args.out_dir) / args.split
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed if args.split == "train" else args.seed + 1000)
    vendors = VENDORS[args.split]

    truth = {}
    counts: dict[str, int] = {}
    for i in range(1, args.count + 1):
        build = rng.choice(BUILDERS)
        name = f"{args.split}-{i:03d}"
        pdf = out / f"{name}.pdf"
        record = build(rng, vendors, pdf)
        # doc_id must match what document_pipeline.py derives from the filename
        truth[f"doc-{name}"] = record
        counts[record["task"]] = counts.get(record["task"], 0) + 1
        print(f"  {pdf.name}  {record['task']}")

    truth_path = Path(args.out_dir) / f"ground_truth_{args.split}.json"
    truth_path.write_text(json.dumps(truth, indent=2), encoding="utf-8")
    print(f"\n{args.count} PDFs -> {out}")
    print(f"ground truth -> {truth_path}")
    print(f"task mix: {counts}")


if __name__ == "__main__":
    main()
