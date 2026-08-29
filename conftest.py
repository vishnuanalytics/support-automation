import pathlib
import sys

# repo root on sys.path so `import interpreter`, `import api`, `import ingestion`
# resolve when pytest is invoked from anywhere.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
