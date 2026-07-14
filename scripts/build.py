#!/usr/bin/env python3
"""
ERM Daily Brief - Build Script
Injects data/data.json into template/index.template.html → index.html
"""

import json
import os
import sys

ROOT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA     = os.path.join(ROOT, "data", "data.json")
TEMPLATE = os.path.join(ROOT, "template", "index.template.html")
OUTPUT   = os.path.join(ROOT, "index.html")

MARKER   = "<!-- __DASHBOARD_DATA__ -->"

def main():
    if not os.path.exists(DATA):
        print(f"ERROR: {DATA} not found. Run generate.py first.")
        sys.exit(1)
    if not os.path.exists(TEMPLATE):
        print(f"ERROR: {TEMPLATE} not found.")
        sys.exit(1)

    with open(DATA) as f:
        payload = f.read().strip()

    with open(TEMPLATE) as f:
        html = f.read()

    inject = f"<script>window.__D = {payload};</script>"
    if MARKER not in html:
        print(f"ERROR: injection marker '{MARKER}' not found in template.")
        sys.exit(1)

    out = html.replace(MARKER, inject, 1)

    with open(OUTPUT, "w") as f:
        f.write(out)

    size_kb = os.path.getsize(OUTPUT) / 1024
    print(f"Built: {OUTPUT} ({size_kb:.1f} KB)")

if __name__ == "__main__":
    main()
