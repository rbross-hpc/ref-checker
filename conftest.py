"""Pytest bootstrap: load .env so live tests see the same env vars the CLI does.

pytest imports conftest.py at collection time, before any test module is
imported. Calling load_dotenv() here mirrors what cli/main.py does at CLI
startup, so live tests gated on real env vars (e.g. tests/test_primo.py's
primo_live tests) run automatically when .env is present.

override=False: real environment variables always win over .env values,
matching the CLI's behaviour and keeping CI/production deterministic.
"""
from dotenv import load_dotenv

load_dotenv(override=False)
