import os
import sys

# Make agent modules importable whether tests run in the container (/app) or from the repo root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools_service"))
