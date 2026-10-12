"""Plain-assert checks for bot.py's pure logic. Run: python test_bot.py"""
import hashlib
import hmac
import re

import bot


def tx(desc, cat=None, bud=None, dest="Shop", tags=(), notes=None, date="2026-10-10T13:10:00+00:00", amount="12.500000000000",
       type="withdrawal"):
    return {"description": desc, "category_name": cat, "budget_name": bud, "destination_name": dest,
            "source_name": "Main Account", "tags": list(tags), "notes": notes, "date": date, "amount": amount,
            "currency_symbol": "€", "transaction_journal_id": "7", "type": type}


def test_signature():
    body = b'{"content":{"id":1}}'
    sig = hmac.new(b"s3cret", b"1700000000." + body, hashlib.sha3_256).hexdigest()
    assert bot.verify_signature("s3cret", f"t=1700000000,v1={sig}", body)
    assert not bot.verify_signature("wrong", f"t=1700000000,v1={sig}", body)
    assert not bot.verify_signature("s3cret", f"t=1700000000,v1={sig}", body + b" ")
    assert not bot.verify_signature("s3cret", "", body)
    assert not bot.verify_signature("", f"t=1700000000,v1={sig}", body)


def test_match_name():
    names = ["McDonald’s", "Corner Burger Bar"]
    assert bot.match_name("mcdonald's", names) == "McDonald’s"
    assert bot.match_name("  corner  BURGER bar ", names) == "Corner Burger Bar"
    assert bot.match_name("KFC", names) is None
    assert bot.match_name("", names) is None


def test_parse_raw():
    assert bot.parse_raw(tx("X", notes="raw: MCD MAIN ST 0423 | card: My Visa")) == "MCD MAIN ST 0423"
    assert bot.parse_raw(tx("SHELL 24")) == "SHELL 24"
    # real eWallet note: Shortcuts put two spaces after "raw:" and none before "|"
    assert bot.parse_raw(tx("X", notes="raw:  A has transferred $0.01 to you.| card: My eWallet")) == \
        "A has transferred $0.01 to you."


def ctx():
    splits = [
        tx("Lunch", "Food", "Food", "McDonald’s"),
        tx("Lunch", "Food", "Food", "Subway", date="2026-10-09T12:00:00+00:00"),
        tx("Lunch", "Food", None, "Subway", date="2026-10-08T12:00:00+00:00"),
        tx("Fuel", "Transport", "Transport", "Shell", tags=["applepay"], notes="raw: SHELL 24 | card: My Visa",
           date="2026-10-10T18:00:00+00:00"),
        # not yet enriched: raw merchant as description, must not become a label or example
        tx("DOWNTOWN BURGER BAR", tags=["applepay"], notes="raw: DOWNTOWN BURGER BAR | card: My Visa",
           date="2026-10-10T20:00:00+00:00"),
        # money sent to a person, waiting for a name / already named: its own flow, not Apple Pay history
        tx("ONG CHUN HAO", dest="ONG CHUN HAO", tags=["ask"], date="2026-10-10T21:00:00+00:00"),
        tx("Dinner split", "Food", dest="ONG CHUN HAO", tags=["ask"], date="2026-10-10T22:00:00+00:00"),
    ]
    return bot.build_context(["McDonald’s", "Subway", "Shell", bot.PENDING], ["Food", "Transport"], ["Food", "Transport"], splits)


def test_build_context():
    c = ctx()
    assert c["label_map"] == {"Lunch": ("Food", "Food"), "Fuel": ("Transport", "Transport")}
    assert c["counts"] == {"Lunch": 3, "Fuel": 1}
    assert c["merchant_kind"] == {"McDonald’s": "Meal", "Subway": "Meal", "Shell": "Fuel"}
    assert c["budgets"][-1] == bot.NO_BUDGET
    assert bot.PENDING not in c["merchants"]
    assert not any("DOWNTOWN BURGER" in e for e in c["examples"])
    assert c["examples"][0] == 'Sat 18:00 €12.50 -> Fuel @ Shell (raw "SHELL 24")'
    # a merchant with mixed history (car park + canteen) has no dominant kind
    mixed = [tx("Parking", dest="City Mall"), tx("Parking", dest="City Mall"), tx("Lunch", dest="City Mall"), tx("Dinner", dest="City Mall")]
    assert "City Mall" not in bot.build_context([], [], [], mixed)["merchant_kind"]


def test_meal_for():
    assert [bot.meal_for(h) for h in (2, 5, 10, 11, 16, 17, 23)] == \
        ["Dinner", "Breakfast", "Breakfast", "Lunch", "Lunch", "Dinner", "Dinner"]


def result(desc, merchant, category="Food", budget="Food", confidence=0.95):
    return {"shop": "x", "description": desc, "merchant": merchant, "category": category, "budget": budget,
            "confidence": confidence}


def test_finalize():
    c = ctx()
    # known label: category/budget from history, not the model
    f, reasons = bot.finalize(result("lunch", "mcdonald's", "Transport", "(none)"), c, 13)
    assert f == {"description": "Lunch", "destination_name": "McDonald’s", "category_name": "Food", "budget_name": "Food"}
    assert reasons == []
    # meal label comes from the hour, whatever the model said
    assert bot.finalize(result("Lunch", "Corner Burger Bar"), c, 20)[0]["description"] == "Dinner"
    # known merchant keeps its usual kind even if the model disagrees
    assert bot.finalize(result("Drinks", "McDonald’s"), c, 20)[0]["description"] == "Dinner"
    f, reasons = bot.finalize(result("Dinner", "shell"), c, 19)
    assert (f["description"], f["category_name"], reasons) == ("Fuel", "Transport", [])

    f, reasons = bot.finalize(result("Dessert", "Sweet Scoop", budget="(none)", confidence=62), c, 21)  # 62% -> 0.62
    assert f == {"description": "Dessert", "destination_name": "Sweet Scoop", "category_name": "Food", "budget_name": None}
    assert reasons == ["new merchant", "new label", "low confidence"]

    # expense account exists but has no history (e.g. its transactions were deleted): still flagged
    c["merchants"].append("Sweet Scoop")
    f, reasons = bot.finalize(result("Drinks", "sweet scoop"), c, 15)
    assert (f["destination_name"], reasons) == ("Sweet Scoop", ["new merchant", "new label"])

    try:
        bot.finalize(result(" ", "X"), c, 12)
        assert False, "empty description must raise"
    except ValueError:
        pass


def test_apply_correction():
    c = ctx()
    current = tx("Lunch", "Food", "Food", "McDonald’s")
    # label changed: category/budget follow the label map
    assert bot.apply_correction(current, {"description": "fuel"}, c) == {
        "description": "Fuel", "destination_name": "McDonald’s", "category_name": "Transport", "budget_name": "Transport"}
    # model echoes unchanged current values: they must not block the label map
    assert bot.apply_correction(current, {"description": "Fuel", "category": "Food", "merchant": "mcdonald’s"}, c)[
        "category_name"] == "Transport"
    # explicit budget wins over the label map; "(none)" clears it
    assert bot.apply_correction(current, {"description": "Fuel", "budget": "(none)"}, c)["budget_name"] is None
    # merchant only, existing spelling reused
    assert bot.apply_correction(current, {"merchant": "subway"}, c) == {
        "description": "Lunch", "destination_name": "Subway", "category_name": "Food", "budget_name": "Food"}


def test_summary_roundtrip():
    f = {"description": "Lunch", "destination_name": "Subway", "category_name": "Food", "budget_name": None}
    text = bot.summary(812, tx("SUBWAY 01"), f, "SUBWAY 01")
    assert "€12.50 · Sat 13:10 · Main Account" in text
    assert "Lunch @ Subway (Food / -)" in text
    assert bot.txn_id("✅ fixed\n" + text) == 812
    assert bot.txn_id("no link here") is None


def test_named_labels():
    splits = [tx("Allowance", "Family", type="deposit"), tx("Allowance", "Family", type="deposit"),
              tx("Allowance", "Gifts", type="deposit"), tx("Refund", "Food", type="deposit"),
              tx("Dinner split", "Food")]  # money sent keeps its own history
    assert bot.named_labels(splits, "deposit") == {"Allowance": "Family", "Refund": "Food"}
    assert bot.named_labels(splits, "withdrawal") == {"Dinner split": "Food"}


def test_split_reply():
    assert bot.split_reply(" Allowance from Parents / Family ") == ("Allowance from Parents", "Family")
    assert bot.split_reply("Allowance from Parents") == ("Allowance from Parents", None)
    assert bot.split_reply("Bank/eWallet top up / Banking") == ("Bank/eWallet top up", "Banking")  # last slash splits
    assert bot.split_reply("Refund /") == ("Refund /", None)  # nothing after the slash: all name
    assert bot.split_reply("x / (none)") == ("x", "(none)")


def test_prompts_fill():
    c = ctx()
    t = tx("SUBWAY 01", notes="raw: SUBWAY 01 | card: My Visa")
    assert not re.search(r"\$\w", bot.classify_prompt(t, "SUBWAY 01", c))
    assert not re.search(r"\$\w", bot.correct_prompt(t, "SUBWAY 01", "dinner", c))
    p = bot.named_prompt(tx("X", dest="My eWallet", type="deposit"), "Allowance", {"Refund": "Food"})
    assert not re.search(r"\$\w", p) and "- Refund -> Food" in p and "=== " not in bot.CORRECT
    assert "money received" in p and "from: Main Account" in p
    p = bot.named_prompt(tx("X", dest="ONG CHUN HAO"), "Dinner split", {})
    assert not re.search(r"\$\w", p) and "money sent" in p and "to: ONG CHUN HAO" in p and "received" not in p


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
