"""Write the API's OpenAPI document (used to generate the frontend's TypeScript types).

Usage: .venv/bin/python scripts/export_openapi.py ../web/openapi.json
"""
import json
import sys

from opa.api.app import create_app
from opa.core.settings import Settings

doc = create_app(Settings(database_url="postgresql+asyncpg://unused@localhost/unused")).openapi()
doc["info"]["version"] = "0.1.0"  # stable across dev builds so the generated file only changes with the API
with open(sys.argv[1], "w") as f:
    json.dump(doc, f, indent=2, sort_keys=True)
    f.write("\n")
print(f"{len(doc['paths'])} paths written to {sys.argv[1]}")
