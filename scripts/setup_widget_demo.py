"""
Widget demo setup - gets tenant ID and creates a ready-to-open demo page.

Usage: python scripts/setup_widget_demo.py
Then open: widget/dist/demo.html in your browser
"""

import requests
import os

API = "http://localhost:8000"

print("Setting up widget demo...")

# Get/create tenant
r = requests.post(f"{API}/api/v1/auth/test-setup")
data = r.json()
tenant_id = data["tenant_id"]
print(f"✓ Tenant ID: {tenant_id}")

# Update demo.html with actual tenant ID
demo_path = os.path.join(os.path.dirname(__file__), "..", "widget", "dist", "demo.html")
demo_path = os.path.normpath(demo_path)

with open(demo_path, "r", encoding="utf-8") as f:
    content = f.read()

content = content.replace("REPLACE_WITH_TENANT_ID", tenant_id)

with open(demo_path, "w", encoding="utf-8") as f:
    f.write(content)

print(f"✓ Updated {demo_path}")
print(f"\nOpen this file in your browser:")
print(f"  {os.path.abspath(demo_path)}")
print(f"\nTest credentials: POL-2024-HO-001 / Smith")