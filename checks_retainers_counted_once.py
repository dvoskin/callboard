""""Retainers paid" counts RETAINERS, not rows, and says when it is a floor.

A retainer settled in two instalments is one retainer. It used to count payment
records and reported Rothmel 4 for 3 -- over by however many people paid in
parts, which is why it was not a clean doubling and did not look like a
duplication bug. It now counts retainers CLOSED in the window (2026-08-25), and
the same invariant has to hold for the new source: one retainer, counted once,
however many times the scan sees it.

The second claim is newer and is the one that bites quietly. Books cannot filter
on last_payment_date, so this is a newest-first SCAN of every retainer, capped
at a record budget. A scan that never reached the start of the window returns a
SHORT list that looks exactly like a real one -- so when it is truncated the
board has to say so rather than print a confident undercount.

Run with no arguments. Reads nothing from the network.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import app as appmod  # noqa: E402
import books_client   # noqa: E402


def _pay(who, inv_ids, amount):
    return {"salesperson_name": who, "invoice_ids": list(inv_ids),
            "amount": amount, "payment_id": "p-%s-%s" % (who, amount)}


def run():
    fails = 0
    real_est = appmod._books.list_sent_estimates
    real_ret = appmod._books.list_sent_retainer_invoices
    real_pay = appmod._books.list_retainer_payments

    # Retainers CLOSED in the window, as list_retainers_paid_on returns them.
    # R3 appears twice: the scan can reach one retainer by more than one route,
    # and that must not become two retainers.
    retainers = [
        {"retainerinvoice_id": "R1", "salesperson_name": "Rothmel Foncham", "total": 500},
        {"retainerinvoice_id": "R2", "salesperson_name": "Rothmel Foncham", "total": 500},
        {"retainerinvoice_id": "R3", "salesperson_name": "Rothmel Foncham", "total": 500},
        {"retainerinvoice_id": "R3", "salesperson_name": "Rothmel Foncham", "total": 500},
        {"retainerinvoice_id": "R4", "salesperson_name": "Adelita Flowers", "total": 800},
        {"retainerinvoice_id": "R5", "salesperson_name": "Adelita Flowers", "total": 800},
        # No id at all must still count once, not vanish.
        {"salesperson_name": "Alicia Reyes", "total": 750},
    ]
    real_paid = appmod._books.list_retainers_paid_on
    appmod._books.list_sent_estimates = lambda *a, **k: []
    appmod._books.list_retainers_sent = lambda *a, **k: []
    appmod._books.list_retainers_paid_on = lambda *a, **k: retainers
    appmod._v5_books_cache.clear()
    try:
        by_agent, meta = appmod._v5_books_fetch("2026-08-26", "2026-08-26")
    finally:
        appmod._books.list_sent_estimates = real_est
        appmod._books.list_sent_retainer_invoices = real_ret
        appmod._books.list_retainer_payments = real_pay
        appmod._books.list_retainers_paid_on = real_paid
        appmod._v5_books_cache.clear()

    def g(name, field):
        return (by_agent.get(appmod._norm_name(name)) or {}).get(field)

    cases = [
        ("Rothmel: 3 retainers", g("Rothmel Foncham", "retainers_paid"), 3),
        ("...not 4 scan rows", g("Rothmel Foncham", "retainers_paid") == 4, False),
        ("Adelita: 2 retainers", g("Adelita Flowers", "retainers_paid"), 2),
        ("a retainer with no id counts", g("Alicia Reyes", "retainers_paid"), 1),
        # The money is unaffected by the dedup: 3 x 500, 2 x 800.
        ("Rothmel amount is the retainers", g("Rothmel Foncham", "paid_amount"), 1500.0),
        ("Adelita amount is the retainers", g("Adelita Flowers", "paid_amount"), 1600.0),
    ]

    # A truncated scan must be declared, not printed as a count. Without this a
    # window the scan never reached back to reports a confident low number and
    # nothing anywhere says it is partial.
    def _truncated(date_start, date_end, *a, **k):
        st = k.get("stats")
        if st is not None:
            st.update({"truncated": True, "reached_back": False,
                       "scanned": 1600, "matched": 1, "inferred_day": 0})
        return [{"retainerinvoice_id": "R9", "salesperson_name": "Rothmel Foncham",
                 "total": 100}]

    appmod._books.list_sent_estimates = lambda *a, **k: []
    appmod._books.list_retainers_sent = lambda *a, **k: []
    appmod._books.list_retainers_paid_on = _truncated
    appmod._v5_books_cache.clear()
    try:
        _ba, meta_t = appmod._v5_books_fetch("2026-06-01", "2026-08-26")
    finally:
        appmod._books.list_sent_estimates = real_est
        appmod._books.list_retainers_paid_on = real_paid
        appmod._v5_books_cache.clear()
    cases.append(("truncated scan is flagged",
                  (meta_t.get("retainers_paid_scan") or {}).get("truncated"), True))
    cases.append(("truncated scan raises an error for the board",
                  any(e.get("metric") == "retainers_paid"
                      for e in meta_t.get("errors", [])), True))
    # Negative half: a scan that DID reach back must not cry wolf.
    cases.append(("a complete scan is not flagged",
                  (meta.get("retainers_paid_scan") or {}).get("truncated"), False))

    # The SAME invoice referred to two different ways across two payments must
    # collapse to one. This is what left Adelita at 3 instead of 2: payment
    # shapes vary row to row, so one carried the id and the other the number.
    c2 = books_client.BooksClient()
    c2.client_id = c2.client_secret = c2.refresh_token = c2.org_id = "x"
    c2.list_customer_payments = lambda *a, **k: [
        {"invoices": [{"invoice_id": "77", "invoice_number": "INV-77"}],
         "amount": 300, "payment_id": "a"},
        {"invoice_numbers": "INV-77", "amount": 300, "payment_id": "b"},
    ]
    c2._list_documents = lambda *a, **k: [
        {"invoice_id": "77", "invoice_number": "INV-77",
         "salesperson_name": "Adelita Flowers"}]
    both = c2.list_retainer_payments("2026-08-26", "2026-08-26")
    idents = set()
    for r in both:
        idents.update(r["invoice_ids"])
    cases.append(("id and number are one invoice", len(idents), 1))

    # And the client must hand over ONE id per invoice. `keys` used for the
    # owner lookup holds both the number AND the id of each invoice, so reusing
    # it here would count a single invoice twice.
    raw = [{"invoices": [{"invoice_id": "9", "invoice_number": "INV-9"}],
            "amount": 100, "payment_id": "x"}]
    c = books_client.BooksClient()
    c.client_id = c.client_secret = c.refresh_token = c.org_id = "x"
    c.list_customer_payments = lambda *a, **k: raw
    c._list_documents = lambda *a, **k: []
    out = c.list_retainer_payments("2026-08-26", "2026-08-26")
    cases.append(("one id per invoice", len(out[0]["invoice_ids"]), 1))

    for label, got, want in cases:
        ok = got == want
        print("  %-34s want %-8s got %-8s %s" % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    print("\n%d mismatched" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
