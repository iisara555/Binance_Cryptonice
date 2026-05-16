#!/usr/bin/env python3
import json, subprocess, os

cfg_path = os.path.expanduser("~/.openclaw/openclaw.json")
with open(cfg_path) as f:
    d = json.load(f)

gw = d.get("gateway", {})
print("gateway.mode:", gw.get("mode"))
print("gateway.port:", gw.get("port"))

providers = d.get("providers", {})
print("providers configured:", list(providers.keys())[:8])

channels = d.get("channels", {})
print("channels configured:", list(channels.keys())[:8])

# Check if gateway is reachable
result = subprocess.run(["openclaw", "gateway", "--help"], capture_output=True, text=True, timeout=5)
print("\nopenclaw gateway help exit code:", result.returncode)
print(result.stdout[:300] if result.stdout else result.stderr[:300])
