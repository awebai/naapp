"""The Tasks app: a team's tasks, the claims on them and its locks."""

from pathlib import Path

# The committed manifest template; each deployment fills its app id, origin and keys.
MANIFEST_PATH = Path(__file__).resolve().parent / "aweb-app.json"
