from pathlib import Path
import sys

# The portable app intentionally resolves its assets relative to sys.argv[0].
sys.argv[0] = str(Path(__file__).resolve().parents[1] / "main.py")
