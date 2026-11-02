"""Stable MCP launcher usable without pip installation or PATH changes."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ebook_translate.mcp import main

raise SystemExit(main())
