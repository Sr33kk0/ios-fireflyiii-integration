"""Replay eval: relabel past labelled taps (or, with --ask, rename past named `ask`s) with only the history before
each, score against what was booked. Read-only: GETs from Firefly, calls Ollama, never writes or posts.
Run: python eval.py [--ask] [--limit N] [--json PATH]"""
import argparse
import asyncio
import json
from datetime import datetime

import aiohttp

import bot

at = lambda t: datetime.fromisoformat(t["date"])


def eval_taps(groups):
    """Single-split labelled taps, oldest first: the booked description and merchant are the truth."""
    singles = [g["attributes"]["transactions"][0] for g in groups if len(g["attributes"]["transactions"]) == 1]
    tags = lambda t: t.get("tags") or []
    return sorted((t for t in singles if bot.TAG in tags(t) and bot.ASK not in tags(t)
                   and t["destination_name"] != bot.PENDING), key=at)


def replay_ctx(tap, splits, categories, budgets):
    """Context as it was just before `tap`: only earlier splits, and only the merchants they name."""
    # ponytail: rebuilt per tap, O(taps x splits); fine for a year of history, index by date if it gets slow
    before = [t for t in splits if at(t) < at(tap)]
    return bot.build_context(list(dict.fromkeys(t["destination_name"] for t in before)), categories, budgets, before)


def rate(n, d):
    return f"{n}/{d} ({n / d:.0%})" if d else f"{n}/0"


def report(records):
    """Per-tap records -> printable summary."""
    ok = [r for r in records if not r["error"]]
    for r in ok:
        r["merchant_ok"] = bot.norm(r["predicted_merchant"]) == bot.norm(r["expected_merchant"])
        r["label_ok"] = bot.norm(r["predicted_label"]) == bot.norm(r["expected_label"])
        r["correct"] = r["merchant_ok"] and r["label_ok"]
    good = lambda rs: sum(r["correct"] for r in rs)
    hist, model = [r for r in ok if r["via"] == "history"], [r for r in ok if r["via"] == "model"]
    flagged, wrong = [r for r in ok if r["reasons"]], [r for r in ok if not r["correct"]]
    lo = [r for r in model if r["confidence"] < bot.CONFIDENCE_MIN]
    hi = [r for r in model if r["confidence"] >= bot.CONFIDENCE_MIN]
    lines = [
        f"taps: {len(records)}, errored: {sum(r['error'] is not None for r in records)}, scored: {len(ok)}",
        f"settled by history: {rate(len(hist), len(ok))}, model calls: {rate(len(model), len(ok))}",
        f"merchant: {rate(sum(r['merchant_ok'] for r in ok), len(ok))}",
        f"label: {rate(sum(r['label_ok'] for r in ok), len(ok))}",
        f"both: {rate(good(ok), len(ok))}",
        f"both via history: {rate(good(hist), len(hist))}, via model: {rate(good(model), len(model))}",
        f"flagged: {rate(len(flagged), len(ok))}",
        f"flag precision (flagged that were wrong): {rate(len(flagged) - good(flagged), len(flagged))}",
        f"flag recall (wrong that were flagged): {rate(sum(bool(r['reasons']) for r in wrong), len(wrong))}",
        f"model error rate, confidence < {bot.CONFIDENCE_MIN}: {rate(len(lo) - good(lo), len(lo))}",
        f"model error rate, confidence >= {bot.CONFIDENCE_MIN}: {rate(len(hi) - good(hi), len(hi))}",
        "misses:",
    ]
    lines += [f'  "{r["raw"]}": {r["expected_label"]} @ {r["expected_merchant"]} -> '
              f'{r["predicted_label"]} @ {r["predicted_merchant"]} [{r["via"]}] {", ".join(r["reasons"])}' for r in wrong]
    lines += [f'  "{r["raw"]}": error {r["error"]}' for r in records if r["error"]]
    return "\n".join(lines)


def ask_replay(splits):
    """Named `ask` splits oldest first, each with only the splits dated strictly before it."""
    # ponytail: O(splits²), fine for hundreds of `ask`s
    return [(t, [e for e in splits if at(e) < at(t)]) for t in sorted(splits, key=at)]


async def ask_record(b, tx, earlier):
    """What Bot.ask would have booked for `tx` given only `earlier`, next to what was booked. Never writes."""
    r = {"type": tx["type"], "expected_name": tx["description"], "expected_category": tx.get("category_name"),
         "predicted_name": None, "predicted_category": None, "via": None, "error": None}
    try:
        name = bot.usual_name(tx, earlier)
        if name:
            r.update(predicted_name=name, predicted_category=bot.named_labels(earlier, tx["type"])[name], via="usual")
        elif tx["type"] == "withdrawal":
            via = "payee history" if bot.payee_kind(tx, earlier) else "model"
            r.update(predicted_name=await b.guess(tx, earlier), via=via)
        else:
            r["via"] = "asked"
    except Exception as e:  # one bad split is recorded, the run goes on
        r["error"] = str(e)[:200]
    return r


def ask_report(records):
    """Per-split `ask` records -> printable summary. Prints labels only, never the party."""
    ok = [r for r in records if not r["error"]]
    for r in ok:
        r["name_ok"] = r["predicted_name"] is not None and bot.norm(r["predicted_name"]) == bot.norm(r["expected_name"])
        r["category_ok"] = bot.norm(r["predicted_category"]) == bot.norm(r["expected_category"])
    via = lambda v: [r for r in ok if r["via"] == v]
    named, usual = [r for r in ok if r["via"] != "asked"], via("usual")
    lines = [f"splits: {len(records)}, errored: {len(records) - len(ok)}",
             "via: " + ", ".join(f"{v} {len(via(v))}" for v in ("usual", "payee history", "model", "asked")),
             f"name: {rate(sum(r['name_ok'] for r in named), len(named))}",
             "name " + ", ".join(f"via {v}: {rate(sum(r['name_ok'] for r in via(v)), len(via(v)))}"
                                 for v in ("usual", "payee history", "model")),
             f"category via usual: {rate(sum(r['category_ok'] for r in usual), len(usual))}",
             f"would have asked: {rate(len(via('asked')), len(ok))}",
             "misses:"]
    lines += [f"  {r['expected_name']} -> {r['predicted_name']} [{r['via']}] {r['type']}"
              + ("" if r["category_ok"] or r["via"] != "usual" else
                 f", category {r['expected_category']} -> {r['predicted_category']}")
              for r in named if not (r["name_ok"] and (r["category_ok"] or r["via"] != "usual"))]
    lines += [f"  error {r['error']} {r['type']}" for r in records if r["error"]]
    return "\n".join(lines)


async def ask_main(b, limit):
    pairs = ask_replay(await b.ask_history())
    pairs = pairs[-limit:] if limit else pairs
    records = []
    for i, (t, earlier) in enumerate(pairs, 1):
        records.append(await ask_record(b, t, earlier))
        print(f"\r{i}/{len(pairs)}", end="", flush=True)
    print()
    print(ask_report(records))
    return records


async def main(limit, json_path, ask=False):
    b = bot.Bot()  # never started: no Discord, no webhook server
    async with aiohttp.ClientSession() as b.web:
        if ask:
            return dump(await ask_main(b, limit), json_path)
        names = lambda items: [i["attributes"]["name"] for i in items]
        categories = names(await b.ff_all("/categories", {}))
        budgets = names([x for x in await b.ff_all("/budgets", {}) if x["attributes"].get("active", True)])
        groups = await b.withdrawals()
        splits = [t for g in groups for t in g["attributes"]["transactions"]]
        taps = eval_taps(groups)[-limit:] if limit else eval_taps(groups)
        records = []
        for i, tap in enumerate(taps, 1):
            raw = bot.parse_raw(tap)
            r = {"raw": raw, "expected_label": tap["description"], "expected_merchant": tap["destination_name"],
                 "predicted_label": None, "predicted_merchant": None, "via": None, "confidence": None, "reasons": [],
                 "error": None}
            try:
                f, r["reasons"], r["confidence"], called = await b.label(tap, raw, replay_ctx(tap, splits, categories, budgets))
                r.update(predicted_label=f["description"], predicted_merchant=f["destination_name"],
                         via="model" if called else "history")
            except Exception as e:  # one bad tap is recorded, the run goes on
                r["error"] = str(e)[:200]
            records.append(r)
            print(f"\r{i}/{len(taps)}", end="", flush=True)
    print()
    print(report(records))
    dump(records, json_path)


def dump(records, json_path):
    if json_path:
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(records, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ask", action="store_true", help="replay named `ask` transactions instead of taps")
    p.add_argument("--limit", type=int, default=0, help="evaluate only the most recent N taps (or `ask`s)")
    p.add_argument("--json", help="write per-tap (or per-`ask`) records to this path")
    a = p.parse_args()
    asyncio.run(main(a.limit, a.json, a.ask))
