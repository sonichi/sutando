# Secret vault — usage recipes

Extended reference for `CLAUDE.md` § Vault. The rule — always use vault for
API keys, tokens, and passwords — lives inline in `CLAUDE.md`; this file
carries the copy-paste recipes.

## Python import boilerplate

Works from any script stored inside this checkout, at any nesting depth:

```python
import sys
from pathlib import Path

# Make the repo's src/ importable from any script stored inside this checkout.
repo = next(p for p in Path(__file__).resolve().parents
            if (p / "src" / "vault_intercept.py").is_file())
sys.path.insert(0, str(repo / "src"))

from vault_intercept import get_vault_key, list_vault_keys

keys = list_vault_keys()  # returns list of stored key names
api_key = get_vault_key("OPENAI_API_KEY")  # raises KeyError if not found
```

## CLI (for subprocesses)

```bash
python3 skills/secret-vault/secret-vault.py list                           # list stored key names
python3 skills/secret-vault/secret-vault.py get KEY                        # print value
python3 skills/secret-vault/secret-vault.py env KEY1 KEY2 -- python3 x.py  # inject as env vars
```

If an integration needs a key that isn't in the vault yet, ask the user to
send `vault set KEY value` via Slack or Discord — the bridge intercepts it
securely before it touches disk.
