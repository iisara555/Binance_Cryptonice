"""Extract actual emoji characters from log_formatter.py and build a replacement map."""
import sys, re
sys.stdout.reconfigure(encoding='utf-8')

data = open('log_formatter.py', encoding='utf-8').read()

# Find all unique non-ASCII sequences that look like emoji (1-4 chars, all > U+007F)
emoji_set = set()
for line in data.splitlines():
    for m in re.finditer(r'[^\x00-\x7f]+', line):
        s = m.group()
        # Skip Thai characters and common punctuation
        if all(0x0E00 <= ord(c) <= 0x0E7F for c in s):
            continue
        if len(s) <= 5:  # emoji are typically 1-4 chars
            emoji_set.add(s)

print("Unique non-ASCII sequences found:")
for e in sorted(emoji_set, key=lambda x: ord(x[0])):
    hex_codes = ' '.join(f'U+{ord(c):04X}' for c in e)
    print(f"  {e!r:25s}  {hex_codes}")
