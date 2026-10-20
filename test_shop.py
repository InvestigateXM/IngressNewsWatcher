"""Offline check for the merch shop monitor: python test_shop.py"""
import checker

sent = []
checker.send_message = sent.append


def prod(pid, title, price="9.99", available=True, body=""):
    return {"id": pid, "title": title, "handle": f"p{pid}", "body_html": body,
            "variants": [{"price": price, "available": available}]}


def run(*products):
    class Resp:
        status_code = 200
        def json(self): return {"products": list(products)}
    checker.fetch = lambda url: Resp()
    sent.clear()
    checker.check_shop(state)
    return list(sent)


state = {}
a, b = prod(1, "A"), prod(2, "B")
assert run(a, b) == []                                            # silent baseline
assert "New product" in run(a, b, prod(3, "C"))[0]                # new
msgs = run(a, prod(2, "B", price="12.00", available=False), prod(3, "C"))
assert "$9.99 → $12.00" in msgs[0] and "In stock → Sold out" in msgs[0], msgs
assert "📦" in run(a, prod(2, "B", price="12.00", available=True), prod(3, "C"))[0]  # stock-only icon
assert "removed" in run(a, prod(2, "B", price="12.00"))[0]        # C removed
assert run(a, prod(2, "B", price="12.00")) == []                  # steady state
print("ok")
