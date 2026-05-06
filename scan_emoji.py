"""Count non-ASCII lines in each file and show only functional code lines (not comments)."""
import sys, os, re
sys.stdout.reconfigure(encoding='utf-8')

FILES = ['alerts.py', 'telegram_bot.py', 'cli_ui.py']

for fname in FILES:
    if not os.path.exists(fname):
        continue
    raw = open(fname, 'rb').read()
    lines = raw.split(b'\r\n')
    found = []
    for i, line in enumerate(lines):
        if not any(b > 127 for b in line):
            continue
        text = line.decode('utf-8', errors='replace').strip()
        # Skip pure comment lines and docstrings that are just decoration
        if text.startswith('#') and not any(c in text for c in ['"', "'"]):
            continue
        found.append((i+1, text[:100]))
    
    print(f"\n=== {fname}: {len(found)} functional lines with non-ASCII ===")
    for ln, text in found[:40]:
        print(f"  L{ln}: {text}")
    if len(found) > 40:
        print(f"  ... and {len(found)-40} more")
