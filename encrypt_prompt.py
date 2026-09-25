#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["cryptography"]
# ///
"""Encrypts proposal_prompt.md into proposal_prompt.enc, the file the bot reads.

Uses PROMPT_KEY from the environment; without it, generates a new key and prints it.
"""

import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet

BASE_DIR = Path(__file__).parent
SOURCE = BASE_DIR / "proposal_prompt.md"
TARGET = BASE_DIR / "proposal_prompt.enc"


def main() -> None:
    if not SOURCE.exists():
        print(f"Arquivo não encontrado: {SOURCE}", file=sys.stderr)
        sys.exit(1)
    key = os.environ.get("PROMPT_KEY")
    if not key:
        key = Fernet.generate_key().decode()
        print(f"Chave nova (guarde e cadastre como secret PROMPT_KEY):\n{key}")
    TARGET.write_bytes(Fernet(key.encode()).encrypt(SOURCE.read_bytes()))
    print(f"Gerado {TARGET.name}")


if __name__ == "__main__":
    main()
