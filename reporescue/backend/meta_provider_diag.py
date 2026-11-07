"""Print local provider/deployment diagnostics (no secrets).

Run from reporescue/backend with the local environment loaded, then
compare `key_fingerprint` with the worker/backend startup line in the
Railway logs. Equal fingerprints prove both sides use the same key;
different fingerprints prove a credential mismatch without revealing
either key.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.meta_client import deployment_info, provider_info


def main():
    print(json.dumps({
        "deployment": deployment_info(),
        "provider": provider_info(),
    }, indent=2))


if __name__ == "__main__":
    main()
