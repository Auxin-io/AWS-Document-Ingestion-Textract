"""Generate three small business-document datasets as real PDFs, with labels.

    employee   timesheets + expense reports        about named employees
    finance    invoices + purchase orders          about named vendors
    hr         policies + leave requests           about named policies / employees

Ten documents per dataset by default. Every document has a unique natural
handle - the vendor, the employee, the policy title - so a plain question
("What is the Zephyr Networks invoice total?") identifies exactly one document.
That is what makes closed-book fine-tuning answerable without a document id.

Because the generator knows what it printed, it writes the correct answers
alongside: `ground_truth_<dataset>.json` carries, per document, the task, the
instruction, the expected output AND the structured facts it was built from.
Downstream builders read the facts directly - no OCR parsing, no transcription
risk.

Deliberate realism so OCR is genuinely exercised: tables, varied labels for the
same field, distractor amounts, and layouts that differ per document.

    python generate_pdfs.py --dataset finance --count 10
    python generate_pdfs.py --all
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

VENDORS = ["Northwind Labs", "Acme Industrial", "Baltic Freight", "Cedar Systems",
           "Halcyon Print", "Ironwood Supply", "Juniper Analytics", "Kestrel Metals",
           "Lakeshore Cabling", "Meridian Foods", "Nordic Optics", "Orchard Textiles",
           "Vantage Aerospace", "Willow Pharmaceuticals", "Xenon Energy",
           "Yarrow Agriculture", "Zephyr Networks"]
EMPLOYEES = ["Aisha Rahman", "Ben Carter", "Chloe Nguyen", "Daniel Okafor", "Elena Petrova",
             "Farhan Malik", "Grace Kim", "Hugo Lindqvist", "Isabel Moreno", "Jonas Weber",
             "Keiko Tanaka", "Liam O'Brien", "Maya Patel", "Noah Fischer", "Olivia Santos"]
PROJECTS = ["PRJ-1042", "PRJ-2210", "PRJ-3307", "PRJ-4185", "PRJ-5520"]
MANAGERS = ["R. Ahmed", "S. Brooks", "T. Costa", "V. Dubois", "W. Evans"]
ITEMS = ["steel brackets", "laptop docks", "safety helmets", "network switches",
         "packing crates", "lab reagents", "cable ties", "filtration units",
         "toner cartridges", "pallet wrap"]
TOTAL_LABELS = ["Total", "Total Due", "Amount Payable", "Balance Due", "Invoice Total"]
TERMS = ["Net 15", "Net 30", "Net 45", "Net 60", "Due on receipt"]
EXPENSE_CATS = ["Travel", "Accommodation", "Meals", "Software", "Training", "Equipment"]
LEAVE_TYPES = ["Annual leave", "Sick leave", "Parental leave", "Unpaid leave", "Study leave"]
POLICY_TITLES = ["Remote Work Policy", "Annual Leave Policy", "Expense Reimbursement Policy",
                 "Business Travel Policy", "Code of Conduct Policy", "Overtime Policy",
                 "Equipment Use Policy", "Data Protection Policy", "Flexible Hours Policy",
                 "Training and Development Policy"]
APPROVERS = ["department manager", "team lead", "HR business partner",
             "site director", "operations supervisor"]

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


def render(path: Path, title: str, story: list) -> None:
    SimpleDocTemplate(str(path), pagesize=LETTER, title=title).build(story)


# =============================================================== finance ===
def make_invoice(rng, key: str, path: Path) -> dict:
    vendor = key
    inv_no = f"INV-{rng.randrange(10000, 99999)}"
    issued, due = date(rng), date(rng)
    n = rng.randrange(2, 5)
    lines, subtotal = [], 0.0
    for item in rng.sample(ITEMS, n):
        qty, unit = rng.randrange(1, 200), round(rng.uniform(4, 250), 2)
        lines.append([item, str(qty), money(unit), money(qty * unit)])
        subtotal += qty * unit
    tax = round(subtotal * 0.05, 2)
    total = round(subtotal + tax, 2)
    label = rng.choice(TOTAL_LABELS)
    summary = [["Summary", "Amount"]]
    if rng.random() < 0.35:
        summary.append(["Previous Balance", money(round(rng.uniform(50, 900), 2))])
    summary += [["Subtotal", money(subtotal)], ["Tax", money(tax)], [label, money(total)]]

    render(path, "Invoice", [
        Paragraph(vendor, H),
        Paragraph(f"{rng.randrange(100, 999)} Industrial Way, Suite {rng.randrange(1, 40)}", SUB),
        Spacer(1, 10), Paragraph("INVOICE", H), Spacer(1, 6),
        grid([["Invoice No.", "Issue Date", "Due Date", "Terms"],
              [inv_no, issued, due, rng.choice(TERMS)]], [120, 110, 110, 100], 99),
        Spacer(1, 10),
        grid([["Description", "Qty", "Unit Price", "Amount"]] + lines, [200, 60, 90, 100]),
        Spacer(1, 10), grid(summary, [300, 150]),
        Spacer(1, 12), Paragraph(f"Remit to: {vendor} Accounts Receivable", SUB),
    ])
    return {
        "task": "invoice_extraction",
        "instruction": ("Extract the vendor name, invoice number, invoice total and due date. "
                        "Respond with JSON only, using the keys vendor_name, invoice_number, "
                        "invoice_total, due_date."),
        "output": json.dumps({"vendor_name": vendor, "invoice_number": inv_no,
                              "invoice_total": money(total), "due_date": due},
                             separators=(",", ":")),
        "facts": {"vendor": vendor, "invoice_number": inv_no, "issue_date": issued,
                  "due_date": due, "subtotal": money(subtotal), "tax": money(tax),
                  "total": money(total)},
    }


def make_purchase_order(rng, key: str, path: Path) -> dict:
    supplier = key
    po = f"PO-{rng.randrange(1000, 9999)}"
    item = rng.choice(ITEMS)
    qty, unit = rng.randrange(10, 500), round(rng.uniform(10, 400), 2)
    total = round(qty * unit, 2)
    required = date(rng)
    render(path, "Purchase order", [
        Paragraph("PURCHASE ORDER", H), Paragraph(f"Order reference {po}", SUB), Spacer(1, 12),
        grid([["Supplier", "Required By", "Order Total"], [supplier, required, money(total)]],
             [180, 120, 120], 1),
        Spacer(1, 10),
        grid([["Line Item", "Quantity", "Unit Price", "Amount"],
              [item, str(qty), money(unit), money(total)]], [200, 70, 90, 100]),
        Spacer(1, 12),
        Paragraph("Deliveries outside the stated window require written approval "
                  "from the procurement lead.", BODY),
    ])
    return {
        "task": "purchase_order",
        "instruction": ("Extract the purchase order details. Respond with JSON only, using "
                        "the keys po_number, supplier, quantity, order_total, required_by."),
        "output": json.dumps({"po_number": po, "supplier": supplier, "quantity": qty,
                              "order_total": money(total), "required_by": required},
                             separators=(",", ":")),
        "facts": {"po_number": po, "supplier": supplier, "item": item, "quantity": str(qty),
                  "unit_price": money(unit), "total": money(total), "required_by": required},
    }


# ============================================================== employee ===
def make_timesheet(rng, key: str, path: Path) -> dict:
    name = key
    emp_id = f"EMP-{rng.randrange(1000, 9999)}"
    week_ending = date(rng)
    project = rng.choice(PROJECTS)
    manager = rng.choice(MANAGERS)
    days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    hours = [round(rng.uniform(6, 10), 1) for _ in days]
    total = round(sum(hours), 1)
    overtime = round(max(0.0, total - 40.0), 1)
    render(path, "Timesheet", [
        Paragraph("WEEKLY TIMESHEET", H),
        Paragraph(f"Employee {name} ({emp_id}) - week ending {week_ending}", SUB), Spacer(1, 12),
        grid([["Day", "Project", "Hours"]] + [[d, project, f"{h:.1f}"] for d, h in zip(days, hours)],
             [120, 160, 100], 2),
        Spacer(1, 10),
        grid([["Summary", "Hours"], ["Regular", f"{min(total, 40.0):.1f}"],
              ["Overtime", f"{overtime:.1f}"], ["Total", f"{total:.1f}"]], [300, 120]),
        Spacer(1, 12), Paragraph(f"Approved by {manager}.", BODY),
    ])
    return {
        "task": "timesheet",
        "instruction": ("Extract the timesheet details. Respond with JSON only, using the "
                        "keys employee, employee_id, week_ending, total_hours, overtime_hours."),
        "output": json.dumps({"employee": name, "employee_id": emp_id, "week_ending": week_ending,
                              "total_hours": total, "overtime_hours": overtime},
                             separators=(",", ":")),
        "facts": {"employee": name, "employee_id": emp_id, "week_ending": week_ending,
                  "project": project, "total_hours": f"{total:.1f}",
                  "overtime_hours": f"{overtime:.1f}", "manager": manager},
    }


def make_expense_report(rng, key: str, path: Path) -> dict:
    name = key
    emp_id = f"EMP-{rng.randrange(1000, 9999)}"
    report = f"EXP-{rng.randrange(10000, 99999)}"
    period = f"2026-{rng.randrange(1, 13):02d}"
    cats = rng.sample(EXPENSE_CATS, rng.randrange(2, 5))
    lines, total, largest, largest_amt = [], 0.0, "", -1.0
    for c in cats:
        amt = round(rng.uniform(20, 900), 2)
        lines.append([c, date(rng), money(amt)])
        total += amt
        if amt > largest_amt:
            largest, largest_amt = c, amt
    total = round(total, 2)
    status = rng.choice(["Approved", "Pending", "Approved", "Reimbursed"])
    render(path, "Expense report", [
        Paragraph("EXPENSE REPORT", H),
        Paragraph(f"Report {report} - {name} ({emp_id}) - period {period}", SUB), Spacer(1, 12),
        grid([["Category", "Date", "Amount"]] + lines, [180, 120, 120], 2),
        Spacer(1, 10),
        grid([["Summary", "Amount"], ["Total claimed", money(total)], ["Status", status]],
             [300, 150]),
        Spacer(1, 12), Paragraph("Receipts must be attached for any item over $25.", BODY),
    ])
    return {
        "task": "expense_report",
        "instruction": ("Extract the expense report details. Respond with JSON only, using "
                        "the keys employee, report_number, period, total_claimed, status."),
        "output": json.dumps({"employee": name, "report_number": report, "period": period,
                              "total_claimed": money(total), "status": status},
                             separators=(",", ":")),
        "facts": {"employee": name, "employee_id": emp_id, "report_number": report,
                  "period": period, "total_claimed": money(total),
                  "largest_category": largest, "status": status},
    }


# ==================================================================== hr ===
def make_policy(rng, key: str, path: Path) -> dict:
    title = key
    ref = f"HR-{rng.randrange(100, 999)}"
    days = rng.randrange(2, 16)
    approver = rng.choice(APPROVERS)
    per_week = rng.randrange(1, 5)
    render(path, title, [
        Paragraph(title.upper(), H), Paragraph(f"Policy reference {ref}", SUB), Spacer(1, 16),
        Paragraph("<b>1. Scope.</b> This policy applies to all salaried employees.", BODY),
        Spacer(1, 8),
        Paragraph(f"<b>2. Approval.</b> Requests under this policy must be approved by the "
                  f"{approver} before taking effect, and no more than {per_week} time(s) per "
                  f"week without escalation.", BODY),
        Spacer(1, 8),
        Paragraph(f"<b>3. Notice.</b> Requests must be submitted at least {days} business "
                  f"days in advance through the HR portal.", BODY),
        Spacer(1, 8),
        Paragraph("<b>4. Review.</b> This policy is reviewed annually by Human Resources.", BODY),
    ])
    return {
        "task": "policy_qa",
        "instruction": ("Answer the question using only the policy text. Question: how far "
                        "in advance must the request be submitted, and who approves it?"),
        "output": (f"The request must be submitted at least {days} business days in "
                   f"advance and must be approved by the {approver}."),
        "facts": {"title": title, "reference": ref, "notice_days": str(days),
                  "approver": approver, "per_week": str(per_week)},
    }


def make_leave_request(rng, key: str, path: Path) -> dict:
    name = key
    emp_id = f"EMP-{rng.randrange(1000, 9999)}"
    req = f"LR-{rng.randrange(10000, 99999)}"
    kind = rng.choice(LEAVE_TYPES)
    start = date(rng)
    days = rng.randrange(1, 15)
    approver = rng.choice(MANAGERS)
    status = rng.choice(["Approved", "Pending", "Approved", "Rejected"])
    render(path, "Leave request", [
        Paragraph("LEAVE REQUEST", H), Paragraph(f"Request {req}", SUB), Spacer(1, 12),
        grid([["Employee", "Employee ID", "Leave Type"], [name, emp_id, kind]], [180, 120, 140], 99),
        Spacer(1, 10),
        grid([["Start Date", "Working Days", "Approver", "Status"],
              [start, str(days), approver, status]], [110, 100, 120, 100], 99),
        Spacer(1, 12),
        Paragraph("Leave balances are updated on approval. Contact HR for questions.", BODY),
    ])
    return {
        "task": "leave_request",
        "instruction": ("Extract the leave request details. Respond with JSON only, using "
                        "the keys employee, request_number, leave_type, start_date, days, status."),
        "output": json.dumps({"employee": name, "request_number": req, "leave_type": kind,
                              "start_date": start, "days": days, "status": status},
                             separators=(",", ":")),
        "facts": {"employee": name, "employee_id": emp_id, "request_number": req,
                  "leave_type": kind, "start_date": start, "days": str(days),
                  "approver": approver, "status": status},
    }


# Each dataset alternates two builders. Every builder draws its key from a pool
# WITHOUT replacement, so each document has a unique natural handle.
DATASETS = {
    "finance":  {make_invoice: VENDORS, make_purchase_order: VENDORS},
    "employee": {make_timesheet: EMPLOYEES, make_expense_report: EMPLOYEES},
    "hr":       {make_policy: POLICY_TITLES, make_leave_request: EMPLOYEES},
}


def generate(dataset: str, count: int, out_root: Path, seed: int) -> None:
    builders = DATASETS[dataset]
    out = out_root / dataset
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(f"{seed}-{dataset}")
    # One shuffled pool per SOURCE LIST, shared by every builder that uses it -
    # otherwise an invoice and a purchase order could both land on "Ironwood
    # Supply" and the vendor would no longer identify a single document.
    shared = {id(pool): rng.sample(pool, len(pool)) for pool in builders.values()}
    pools = {b: shared[id(pool)] for b, pool in builders.items()}
    order = list(builders)

    truth, counts = {}, {}
    for i in range(1, count + 1):
        build = order[(i - 1) % len(order)]
        key = pools[build].pop()
        name = f"{dataset}-{i:03d}"
        record = build(rng, key, out / f"{name}.pdf")
        record["dataset"] = dataset
        truth[f"doc-{name}"] = record
        counts[record["task"]] = counts.get(record["task"], 0) + 1
        print(f"  {name}.pdf  {record['task']:<18} {key}")

    (out_root / f"ground_truth_{dataset}.json").write_text(
        json.dumps(truth, indent=2), encoding="utf-8")
    print(f"{dataset}: {count} PDFs -> {out}   {counts}\n")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dataset", choices=sorted(DATASETS))
    p.add_argument("--all", action="store_true", help="generate every dataset")
    p.add_argument("--count", type=int, default=10)
    p.add_argument("--out-dir", default="data/pdfs")
    p.add_argument("--seed", type=int, default=7)
    args = p.parse_args()
    if not args.dataset and not args.all:
        p.error("choose --dataset <name> or --all")

    for ds in (sorted(DATASETS) if args.all else [args.dataset]):
        generate(ds, args.count, Path(args.out_dir), args.seed)


if __name__ == "__main__":
    main()
