"""Fix log_formatter.py: replace all non-ASCII emoji with ASCII-safe text markers.

This works at the byte level to handle the encoding mismatch properly.
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

# Read as raw bytes
raw = open('log_formatter.py', 'rb').read()

# Remove BOM if present
if raw.startswith(b'\xef\xbb\xbf'):
    raw = raw[3:]
    print("Removed BOM")

# Build byte-level replacements: find each emoji's actual bytes
# Strategy: replace the return-value strings line by line
text = raw.decode('utf-8', errors='surrogateescape')
lines = text.split('\n')

# Map: line patterns -> ASCII replacement for the emoji portion
# We'll detect lines with pick_emoji returns and shorten_message returns
import re

def has_nonascii(s):
    return any(ord(c) > 127 for c in s)

replaced = 0
new_lines = []
for line in lines:
    if not has_nonascii(line):
        new_lines.append(line)
        continue
    
    orig = line
    
    # --- pick_emoji() returns ---
    # return "EMOJI"  -> return "ASCII"
    # Match: return "X" where X contains non-ASCII
    m = re.match(r'^(\s*return\s+")[^"]*(".*)', line)
    if m and 'pick_emoji' not in line:
        content = line[len(m.group(1)):line.index('"', len(m.group(1)))]
        if has_nonascii(content):
            # Determine what ASCII to use based on context (look at nearby lines)
            # For now, collect and we'll map manually
            pass

    new_lines.append(line)

# Instead of complex detection, let's just do targeted string replacements
# on the decoded text, matching the actual character sequences

print(f"Total lines: {len(lines)}")
print("Non-ASCII lines:")
for i, line in enumerate(lines, 1):
    if has_nonascii(line):
        # Show only emoji-containing lines (skip comments, Thai text)
        stripped = line.strip()
        if 'return' in stripped or '=' in stripped:
            ascii_chars = ''.join(c if ord(c) < 128 else f'[U+{ord(c):04X}]' for c in stripped)
            print(f"  L{i}: {ascii_chars[:120]}")
