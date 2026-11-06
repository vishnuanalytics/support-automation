"""Score the reply classifier against hand-labelled messages.

    python -m eval.monitor.reply_labels --gold /path/to/gold.json --tenant <id>

Gold is a JSON list of {"text": ..., "gold": kind}. It holds case text, so keep
it out of the repository. The number that matters most is real updates wrongly
hidden as noise, because those are replies nobody would see.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv(".env")
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--gold", required=True)
    parser.add_argument("--tenant", required=True)
    args = parser.parse_args()
    logging.disable(logging.WARNING)

    from interpreter.monitor import replies

    rows = json.load(open(args.gold))
    confusion: Counter = Counter()
    hidden, kept_noise, right = [], [], 0
    by_rule = 0
    for i, row in enumerate(rows):
        kind = replies.rule_kind(row["text"])
        by_rule += bool(kind)
        if not kind:
            try:
                kind = replies.llm_kind(row["text"], args.tenant)["kind"]
            except Exception as e:  # noqa: BLE001
                print("failed", i, type(e).__name__)
                kind = "update"
        right += kind == row["gold"]
        confusion[(row["gold"], kind)] += 1
        gold_noise, got_noise = row["gold"] in replies.NOISE, kind in replies.NOISE
        if got_noise and not gold_noise:
            hidden.append(i)
        if gold_noise and not got_noise:
            kept_noise.append(i)
    n = len(rows)
    noise = sum(r["gold"] in replies.NOISE for r in rows)
    print(f"exact {right}/{n} | by rule {by_rule} | real hidden {len(hidden)}/{n - noise} {hidden} "
          f"| noise kept {len(kept_noise)}/{noise} {kept_noise}")
    for (gold, got), count in sorted(confusion.items()):
        if gold != got:
            print(f"  {gold} -> {got}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
