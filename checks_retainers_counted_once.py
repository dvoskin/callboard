""""Retainers paid" counts RETAINERS, not payment records.

A retainer settled in two instalments is one retainer. Counting payment rows
reported Rothmel 4 for 3 and Adelita 4 for 2 -- over by exactly however many
people paid in parts, which is why it was not a clean doubling and did not look
like a duplication bug.

paid_amount still sums every payment. That is money received and all of it
arrived; only the COUNT is per retainer.

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

    payments = [
        # Rothmel: 3 retainers, one of them paid in two instalments -> 4 rows
        _pay("Rothmel Foncham", ["INV-1"], 500),
        _pay("Rothmel Foncham", ["INV-2"], 500),
        _pay("Rothmel Foncham", ["INV-3"], 250),
        _pay("Rothmel Foncham", ["INV-3"], 250),
        # Adelita: 2 retainers, both split -> 4 rows
        _pay("Adelita Flowers", ["INV-4"], 300),
        _pay("Adelita Flowers", ["INV-4"], 300),
        _pay("Adelita Flowers", ["INV-5"], 100),
        _pay("Adelita Flowers", ["INV-5"], 900),
        # A payment carrying no invoice must still count once, not vanish
        _pay("Alicia Reyes", [], 750),
    ]
    appmod._books.list_sent_estimates = lambda *a, **k: []
    appmod._books.list_sent_retainer_invoices = lambda *a, **k: []
    appmod._books.list_retainer_payments = lambda *a, **k: payments
    appmod._v5_books_cache.clear()
    try:
        by_agent, meta = appmod._v5_books_fetch("2026-08-26", "2026-08-26")
    finally:
        appmod._books.list_sent_estimates = real_est
        appmod._books.list_sent_retainer_invoices = real_ret
        appmod._books.list_retainer_payments = real_pay
        appmod._v5_books_cache.clear()

    def g(name, field):
        return (by_agent.get(appmod._norm_name(name)) or {}).get(field)

    cases = [
        ("Rothmel: 3 retainers", g("Rothmel Foncham", "retainers_paid"), 3),
        ("...not 4 payment rows", g("Rothmel Foncham", "retainers_paid") == 4, False),
        ("Adelita: 2 retainers", g("Adelita Flowers", "retainers_paid"), 2),
        ("payment with no invoice counts", g("Alicia Reyes", "retainers_paid"), 1),
        # The money is unaffected: every instalment arrived.
        ("Rothmel amount is every payment", g("Rothmel Foncham", "paid_amount"), 1500.0),
        ("Adelita amount is every payment", g("Adelita Flowers", "paid_amount"), 1600.0),
    ]

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
