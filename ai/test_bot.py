"""Plain-assert checks for bot.py's logic; flows run on a Stub with Firefly and Discord cut off. Run: python test_bot.py"""
import asyncio
import hashlib
import hmac
import logging
import re
import time
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import bot

logging.disable(logging.CRITICAL)  # expected failures below would print tracebacks


def tx(desc, cat=None, bud=None, dest="Shop", tags=(), notes=None, date="2026-10-10T13:10:00+00:00", amount="12.500000000000",
       type="withdrawal"):
    return {"description": desc, "category_name": cat, "budget_name": bud, "destination_name": dest,
            "source_name": "Main Account", "tags": list(tags), "notes": notes, "date": date, "amount": amount,
            "currency_symbol": "€", "transaction_journal_id": "7", "type": type}


def tap(desc, dest, raw, day=10, cat="Food"):
    return tx(desc, cat, cat, dest, tags=["applepay"], notes=f"raw: {raw} | card: My Visa", date=f"2026-10-{day}T12:00:00+00:00")


def group(id, *splits):
    return {"id": str(id), "attributes": {"transactions": list(splits)}}


class Stub(bot.Bot):
    """Bot with Firefly and Discord cut off. `searches` maps a query fragment to the groups it returns, `firefly`
    a path to what any other Firefly call returns; those calls land in `requests`."""
    user = SimpleNamespace(id=1)  # the bot's own Discord account

    def __init__(self, searches=None, firefly=None):
        super().__init__()
        self.searches, self.firefly = searches or {}, firefly or {}
        self.calls, self.requests, self.updates, self.posts = [], [], [], []

    async def ff_all(self, path, params):
        self.calls.append((path, params))
        if "query" not in params:
            return self.firefly.get(path, [])
        return next((g for part, g in self.searches.items() if part in params["query"]), [])

    async def ff(self, method, path, **kw):
        self.requests.append((method, path, kw.get("json")))
        return self.firefly.get(path)

    async def update(self, group_id, journal_id, f):
        self.updates.append((group_id, f))

    async def post(self, text):
        self.posts.append(text)


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


def test_foreign_paid():
    paid = lambda amount: {**tx("X", notes=f"raw: SHOP | card: My Visa | amount: {amount}"), "currency_code": "EUR"}
    assert bot.foreign_paid(paid("¥1,200")) == "¥1,200"
    assert bot.foreign_paid(paid("JPY 1,200 ")) == "JPY 1,200"
    assert bot.foreign_paid(paid("1.299,00 €")) is None  # the account's own currency, by symbol or code
    assert bot.foreign_paid(paid("eur 12.50")) is None
    assert bot.foreign_paid(paid("12.50")) is None  # no unit: nothing to compare
    assert bot.foreign_paid(tx("X", notes="raw: SHOP | card: My Visa")) is None  # Shortcut without `amount:`
    assert bot.parse_raw(paid("¥1,200")) == "SHOP"


def ctx():
    splits = [
        tx("Lunch", "Food", "Food", "McDonald’s"),
        tx("Lunch", "Food", "Food", "Subway", date="2026-10-09T12:00:00+00:00"),
        tx("Lunch", "Food", None, "Subway", date="2026-10-08T12:00:00+00:00"),
        tx("Fuel", "Transport", "Transport", "Shell", tags=["applepay"], notes="raw: SHELL 24 | card: My Visa",
           date="2026-10-10T18:00:00+00:00"),
        # not yet enriched: raw merchant as description, must not become a label or example
        tx("DOWNTOWN BURGER BAR", dest=bot.PENDING, tags=["applepay"], notes="raw: DOWNTOWN BURGER BAR | card: My Visa",
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


def test_usual_name():
    paid = lambda name, payer="ALEX TAN": {**tx(name, "Family", dest="My eWallet", tags=["ask"], type="deposit"), "source_name": payer}
    new = paid("ALEX TAN")
    assert bot.usual_name(new, [paid("Allowance")] * 3) == "Allowance"
    assert bot.usual_name(new, [paid("Allowance")] * 2) is None  # too few uses to trust
    assert bot.usual_name(new, [paid("Allowance")] * 3 + [paid("Refund")]) is None  # 75%: this payer varies
    assert bot.usual_name(new, [paid("Allowance")] * 4 + [paid("Refund")]) == "Allowance"  # 80%
    assert bot.usual_name(paid("alex  tan"), [paid("Allowance")] * 3) == "Allowance"  # banks vary the case
    assert bot.usual_name(new, [paid("Allowance", "SAM LEE")] * 3) is None  # someone else's history
    assert bot.usual_name(new, [tx("Allowance", dest="ALEX TAN", tags=["ask"])] * 3) is None  # money sent is separate


def test_is_unanswered():
    assert bot.is_unanswered(tx("ALEX TAN", dest="alex tan", tags=["ask"]))
    got = {**tx("ALEX TAN", dest="My eWallet", tags=["ask"], type="deposit"), "source_name": "ALEX TAN"}
    assert bot.is_unanswered(got)
    assert not bot.is_unanswered(tx("Dinner split", dest="ALEX TAN", tags=["ask"]))  # named, category left blank
    assert not bot.is_unanswered(tx("ALEX TAN", "Friends", dest="ALEX TAN", tags=["ask"]))  # named after the person
    assert not bot.is_unanswered(tx("Shop", dest="Shop"))  # not an `ask`


def test_remembered():
    c = ctx()
    assert c["raw_merchant"] == {"shell 24": "Shell"}  # the unlabelled tap teaches nothing
    assert bot.remembered("SHELL  24", c, 9) == {
        "description": "Fuel", "destination_name": "Shell", "category_name": "Transport", "budget_name": "Transport"}
    assert bot.remembered("NEVER SEEN", c, 9) is None
    # newest wins whatever order Firefly lists them in: a correction replaces the model's first guess
    old, fixed = tap("Dessert", "Sweat Scoop", "SS LTD", 10), tap("Dessert", "Sweet Scoop", "SS LTD", 11)
    assert bot.build_context([], [], [], [old, fixed])["raw_merchant"] == {"ss ltd": "Sweet Scoop"}
    assert bot.build_context([], [], [], [fixed, old])["raw_merchant"] == {"ss ltd": "Sweet Scoop"}
    # mixed merchant: its label is not settled, the model still decides
    mixed = [tap("Parking", "City Mall", "CM", 10), tap("Lunch", "City Mall", "CM", 11)]
    assert bot.remembered("CM", bot.build_context([], ["Food"], ["Food"], mixed), 12) is None
    # a meal label this history has never seen has no category yet: the model must supply one
    meals = [tap("Lunch", "Subway", "SUB", 10)]
    c = bot.build_context([], ["Food"], ["Food"], meals)
    assert bot.remembered("SUB", c, 12)["description"] == "Lunch"
    assert bot.remembered("SUB", c, 20) is None


def test_ask_books_usual_name():
    paid = lambda name: {**tx(name, "Family", dest="My eWallet", tags=["ask"], type="deposit"), "source_name": "ALEX TAN"}
    history = {"has_any_category": [group(i, paid("Allowance")) for i in (1, 2, 3)]}
    b = Stub(history)
    asyncio.run(b.ask(9, paid("ALEX TAN")))
    assert b.updates == [(9, {"description": "Allowance", "category_name": "Family"})]
    assert "<@" not in b.posts[0] and bot.txn_id(b.posts[0]) == 9  # no ping, but replying still corrects it


def test_ask_pings_when_unsure():
    new = {**tx("ALEX TAN", dest="My eWallet", tags=["ask"], type="deposit"), "source_name": "ALEX TAN"}
    b = Stub()
    asyncio.run(b.ask(9, new))
    assert b.updates == [] and "<@" in b.posts[0] and bot.txn_id(b.posts[0]) == 9


def test_ask_pings_when_firefly_fails():
    paid = lambda name: {**tx(name, "Family", dest="My eWallet", tags=["ask"], type="deposit"), "source_name": "ALEX TAN"}
    b = Stub({"has_any_category": [group(i, paid("Allowance")) for i in (1, 2, 3)]})

    async def down(*a):
        raise RuntimeError("Firefly 500")
    b.update = down
    asyncio.run(b.ask(9, paid("ALEX TAN")))
    assert len(b.posts) == 1 and "<@" in b.posts[0]  # an unnamed transaction must never go quiet


def test_sweep_catches_unanswered_asks():
    lone, named = tx("ALEX TAN", dest="ALEX TAN", tags=["ask"]), tx("Dinner split", dest="ALEX TAN", tags=["ask"])
    half = tx("ME", dest="ME", tags=["ask"])
    b = Stub({"tag_is:ask has_no_category": [group(1, lone), group(2, named), group(3, half), group(4, lone, lone)]})
    seen = []

    async def ask(group_id, t):
        seen.append(("ask", group_id))

    async def pair(group_id, t):
        seen.append(("pair", group_id))
    b.ask, b.pair = ask, pair

    async def run():
        await b.sweep()
        await asyncio.gather(*b.tasks)
    with patch.object(bot, "OWNER_NAME", "ME"):
        asyncio.run(run())
    assert sorted(seen) == [("ask", 1), ("pair", 3)]


def test_sweep_retries_unlabelled_taps():
    pending = lambda **kw: tx("SHOP 1", dest=bot.PENDING, tags=["applepay"], **kw)
    b = Stub({"tag_is:applepay": [
        group(1, pending()),
        group(2, tx("Lunch", dest="Subway", tags=["applepay"])),  # labelled; its label has no category
        group(3, pending(date=datetime.now(timezone.utc).isoformat())),  # just tapped: its webhook has it
        group(4, pending(cat="Food")),  # a reply named the label but not the merchant
        group(5, pending(), pending())]})  # split
    seen = []

    async def enrich(group_id, t, loud):
        seen.append((group_id, loud))
    b.enrich = enrich
    asyncio.run(b.sweep(asks=False, loud=False))
    assert seen == [(1, False)]
    assert not any("tag_is:ask" in p.get("query", "") for _, p in b.calls)


def test_sweep_loop_schedule():
    b, seen, waits = Stub(), [], []

    async def sweep(asks, loud):
        seen.append((asks, loud))
        if len(seen) == 1:
            raise RuntimeError("Firefly 502")  # not up yet: retried, and it is still the startup sweep
        if len(seen) == 4:
            raise asyncio.CancelledError

    async def wait(seconds):
        waits.append(seconds)
    b.sweep = sweep
    with patch.object(bot, "ASK_EVERY", 2), patch.object(bot.asyncio, "sleep", wait):
        try:
            asyncio.run(b.sweep_loop())
        except asyncio.CancelledError:
            pass
    assert seen == [(True, True), (True, True), (False, False), (True, False)]
    assert waits == [60, bot.SWEEP_EVERY, bot.SWEEP_EVERY]


def test_enrich_quiet_on_retry():
    b = Stub()
    b.ctx, b.ctx_at = ctx(), time.monotonic()

    async def down(*a):
        raise RuntimeError("Ollama down")
    b.ollama = down
    t = tx("NEW PLACE", dest=bot.PENDING, tags=["applepay"], notes="raw: NEW PLACE | card: My Visa")
    asyncio.run(b.enrich(5, t, loud=False))
    assert b.posts == []  # the next sweep retries it
    asyncio.run(b.enrich(5, t))
    assert "<@" in b.posts[0] and bot.txn_id(b.posts[0]) == 5


def test_enrich_skips_model_for_known_raw():
    b = Stub()
    b.ctx, b.ctx_at = ctx(), time.monotonic()

    async def no_model(*a):
        raise AssertionError("model called")
    b.ollama = no_model
    asyncio.run(b.enrich(5, tx("SHELL 24", tags=["applepay"], notes="raw: SHELL 24 | card: My Visa")))
    assert b.updates == [(5, {"description": "Fuel", "destination_name": "Shell", "category_name": "Transport",
                              "budget_name": "Transport"})]
    assert b.posts == []


def test_enrich_flags_foreign_amount():
    b = Stub()
    b.ctx, b.ctx_at = ctx(), time.monotonic()
    asyncio.run(b.enrich(5, tx("SHELL 24", tags=["applepay"], amount="1200",
                               notes="raw: SHELL 24 | card: My Visa | amount: ¥1,200")))
    assert b.updates[0][1]["description"] == "Fuel"  # labelled from history as usual, no model
    assert "<@" in b.posts[0] and "paid ¥1,200, booked €1200.00" in b.posts[0] and "100%" in b.posts[0]


def test_enrich_keeps_remembered_merchant():
    b = Stub()
    history = [tap("Parking", "City Mall", "CM"), tap("Lunch", "City Mall", "CM", 11)]
    b.ctx, b.ctx_at = bot.build_context(["City Mall"], ["Food"], ["Food"], history), time.monotonic()

    async def model(*a):
        return result("Lunch", "Cm Plaza")
    b.ollama = model
    asyncio.run(b.enrich(5, tx("CM", tags=["applepay"], notes="raw: CM | card: My Visa")))
    assert b.updates[0][1]["destination_name"] == "City Mall"


def test_context_limits_history():
    b = Stub()
    asyncio.run(b.context())
    params = next(p for path, p in b.calls if path == "/transactions")
    assert date.fromisoformat(params["start"]) < date.today() <= date.fromisoformat(params["end"])


def test_own_partner():
    sent = tx("HO SHOU YEE", dest="HO SHOU YEE", tags=["ask"], amount="0.020000000000", date="2026-10-11T14:07:30+08:00")
    got = lambda **kw: {**tx("HO SHOU YEE", dest="Ryt Bank", tags=["ask"], amount="0.02", type="deposit",
                            date="2026-10-11T14:08:00+08:00"), "source_name": "HO SHOU YEE", **kw}
    with patch.object(bot, "OWNER_NAME", "HO SHOU YEE"):
        assert bot.is_own(sent) and bot.is_own(got()) and not bot.is_own(tx("X", dest="ONG CHUN HAO", tags=["ask"]))
        assert bot.own_partner(sent, {5: got()}) == (5, got())
        assert bot.own_partner(got(), {4: sent}) == (4, sent)  # either half finds the other
        assert bot.own_partner(sent, {5: got(amount="0.03")}) is None
        assert bot.own_partner(sent, {5: got(date="2026-10-11T15:07:30+08:00")}) is None  # an hour apart
        assert bot.own_partner(sent, {5: got(source_name="ONG CHUN HAO")}) is None  # money from someone else
        assert bot.own_partner(sent, {5: got(tags=[])}) is None
        assert bot.own_partner(sent, {6: got(date="2026-10-11T14:15:00+08:00"), 5: got()})[0] == 5  # closest wins
    assert not bot.is_own(sent)  # unset: no pairing


def own_halves():
    """Money sent from Main Account (id 1) to ME, and received from ME in Ryt Bank (id 9), 40s later."""
    sent = {**tx("ME", dest="ME", tags=["ask"]), "source_id": "1"}
    got = {**tx("ME", dest="Ryt Bank", tags=["ask"], type="deposit", date="2026-10-10T13:10:40+00:00"),
           "source_name": "ME", "destination_id": "9"}
    return sent, got


def transfer_post(notes):
    """The POST that books Main Account -> Ryt Bank for own_halves()."""
    return ("POST", "/transactions", {"apply_rules": False, "fire_webhooks": False, "transactions": [{
        "type": "transfer", "date": "2026-10-10T13:10:00+00:00", "amount": "12.500000000000",
        "description": "Main Account to Ryt Bank", "source_id": "1", "destination_id": "9", "notes": notes}]})


def test_pair_merges_halves():
    sent, got = own_halves()
    b = Stub(firefly={"/transactions": [group(5, got)]})
    with patch.object(bot, "OWNER_NAME", "ME"):
        asyncio.run(b.pair(4, sent))
    # transfer first, then the halves: a failed delete leaves a duplicate, never a gap
    assert b.requests == [transfer_post("sent: ME\nreceived: ME"),
                          ("DELETE", "/transactions/4", None), ("DELETE", "/transactions/5", None)]
    assert b.merged == {4, 5} and b.posts == []


def test_pair_pings_lone_half():
    sent, _ = own_halves()
    with patch.object(bot, "OWNER_NAME", "ME"), patch.object(bot, "PAIR_WAIT", 0):
        b = Stub()
        asyncio.run(b.pair(4, sent))
        assert b.requests == [] and "<@" in b.posts[0] and bot.txn_id(b.posts[0]) == 4
        b = Stub()
        b.merged.add(4)  # its other half merged it while it waited
        asyncio.run(b.pair(4, sent))
        assert b.posts == []


def owner_reply(text, bot_text):
    """Fake Discord: the owner replies `text` to the bot's message `bot_text` (not cached, so it is fetched).
    -> (message, done): done collects the bot's edits, reactions and error replies."""
    done = []

    async def record(*a, **kw):
        done.extend([*a, *kw.values()])

    async def fetch(_):
        return target
    target = SimpleNamespace(content=bot_text, author=Stub.user, edit=record)
    channel = SimpleNamespace(id=bot.DISCORD_CHANNEL_ID, fetch_message=fetch)
    msg = SimpleNamespace(content=text, author=SimpleNamespace(id=bot.DISCORD_USER_ID), channel=channel,
                          reference=SimpleNamespace(message_id=1, resolved=None), add_reaction=record, reply=record)
    return msg, done


def test_reply_routes():
    """A reply to a ping: lone own-transfer half -> transfer, `ask` -> name, tap -> correction."""
    half, _ = own_halves()
    paid = {**tx("ALEX TAN", dest="My eWallet", tags=["ask"], type="deposit"), "source_name": "ALEX TAN"}
    tapped = tx("Lunch", "Food", "Food", "McDonald’s", tags=["applepay"], notes="raw: MCD 01 | card: My Visa")
    got = lambda t: {"data": {"attributes": {"transactions": [t]}}}
    b = Stub(firefly={"/transactions/4": got(half), "/transactions/5": got(paid), "/transactions/6": got(tapped),
                      "/accounts": [{"id": "9", "attributes": {"name": "Ryt Bank"}}]})
    b.ctx, b.ctx_at = ctx(), time.monotonic()

    async def model(*a):
        return {"description": "fuel"}
    b.ollama = model

    with patch.object(bot, "OWNER_NAME", "ME"):
        msg, done = owner_reply(" ryt bank ", bot.summary(4, half, None, "ME"))
        asyncio.run(b.on_message(msg))
    assert b.requests == [("GET", "/transactions/4", None), transfer_post("sent: ME"), ("DELETE", "/transactions/4", None)]
    assert done == ["✅ transfer Main Account to Ryt Bank · €12.50 · Sat 13:10", "✅"]

    msg, done = owner_reply("Allowance / Family", bot.summary(5, paid, None, "ALEX TAN"))
    asyncio.run(b.on_message(msg))
    assert b.updates == [(5, {"description": "Allowance", "category_name": "Family"})] and done[-1] == "✅"

    msg, done = owner_reply("it was fuel", bot.summary(6, tapped, None, "MCD 01"))
    asyncio.run(b.on_message(msg))
    assert b.updates[-1] == (6, {"description": "Fuel", "destination_name": "McDonald’s", "category_name": "Transport",
                                 "budget_name": "Transport"}) and done[-1] == "✅"


def test_own_ends():
    # the replied account takes OWNER_NAME's side: destination of money sent, source of money received
    sent = {**tx("HO SHOU YEE", dest="HO SHOU YEE", tags=["ask"]), "source_id": "1"}
    assert bot.own_ends(sent, ("9", "Ryt Bank")) == (("1", "Main Account"), ("9", "Ryt Bank"))
    got = {**tx("HO SHOU YEE", dest="Ryt Bank", tags=["ask"], type="deposit"), "destination_id": "9"}
    assert bot.own_ends(got, ("1", "Main Account")) == (("1", "Main Account"), ("9", "Ryt Bank"))


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
