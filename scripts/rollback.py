"""
rollback.py
Placeholder. Full spec in propelld-blog-internal-linking-strategy-v3.docx §9.

This script will be implemented during Phase rollbac of the rollout.
It's stubbed here so the repo skeleton matches the strategy doc and so
GitHub Actions workflows can reference it without errors.

USAGE: see strategy doc §9 for the input and output spec.
"""

import argparse
import sys

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print("rollback: not yet implemented")
    print("See propelld-blog-internal-linking-strategy-v3.docx §9 for spec.")
    sys.exit(0)

if __name__ == "__main__":
    main()
