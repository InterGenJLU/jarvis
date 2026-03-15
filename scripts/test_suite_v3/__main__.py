"""Allow running as: python3 -m scripts.test_suite_v3.runner"""
import os
os.environ["PYTHONUNBUFFERED"] = "1"

from .runner import main

main()
