"""Command line: python cli.py example.com [--pitch "..."] [--json]"""

import argparse
import json
import sys
from dataclasses import asdict

from dotenv import load_dotenv

load_dotenv()

from opener import OpenerError, generate  # noqa: E402  (load .env first)


def main() -> int:
    ap = argparse.ArgumentParser(description="Draft a 3-line cold email opener from a company's website.")
    ap.add_argument("url", help="company website, e.g. example.com")
    ap.add_argument("--pitch", default="", help="optional: one line on what you're offering")
    ap.add_argument("--json", action="store_true", help="print full result as JSON")
    args = ap.parse_args()

    try:
        r = generate(args.url, pitch=args.pitch)
    except OpenerError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(asdict(r), indent=2))
        return 0

    greeting = f"Hi {r.founder.split()[0]}," if r.founder else "Hi there,"
    print(greeting)
    print(r.as_text())
    print("\n--- based on ---")
    for line, ev in zip(r.lines, r.evidence):
        if ev:
            print(f'  "{ev}"')
    print(f"  pages read: {', '.join(r.pages)}")
    for w in r.warnings:
        print(f"  ! {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
