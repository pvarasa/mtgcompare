import argparse
import datetime
import json
import logging
import logging.config
from pathlib import Path

from .shops import build_scrapers
from .utils import get_fx

LOGGING_CONF = Path(__file__).resolve().parent.parent / "logging.conf"
logger = logging.getLogger("compare")


def main():
    args = parse_args()
    if args.file is None:
        cards = [args.card]
    else:
        with open(args.file, encoding="utf-8") as file:
            cards = [line.strip() for line in file if line.strip()]

    fx = get_fx("jpy")
    scrappers = build_scrapers(fx)

    all_prices = []
    for card_name in cards:
        logger.info(f"Processing {card_name}")
        prices = []
        for scp in scrappers:
            prices.extend(scp.get_prices(card_name))

        if not prices:
            print(f"No prices found for {card_name}")
            continue

        cheapest = min(prices, key=lambda c: c["price_jpy"])
        print(f"For card {card_name} the cheapest is {json.dumps(cheapest, indent=2)}")
        all_prices.extend(prices)

    if args.export and all_prices:
        now = datetime.datetime.now().strftime("%Y/%m/%d, %H:%M:%S")
        for p in all_prices:
            p["timestamp"] = now
        with open(args.export, "w", encoding="utf-8") as ef:
            json.dump(all_prices, ef)


def parse_args():
    parser = argparse.ArgumentParser(
        prog="compare",
        description="Compares prices for Magic The Gathering cards",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-c", "--card", help="name of the card to search for")
    group.add_argument("-f", "--file", help="file with one card name per line")
    parser.add_argument("-e", "--export", help="file name to export all prices found in json")
    return parser.parse_args()


if __name__ == "__main__":
    logging.config.fileConfig(LOGGING_CONF)
    main()
