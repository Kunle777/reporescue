
import json
import urllib.request
import urllib.error

# TEMPORARY LOCAL TEST ONLY.
# Paste your complete API key between the quotes.
API_KEY = "LLM_1473779774620188_Cb-B-J8nDWzfEY9EOn8frcw5t3c"

URL = "https://api.meta.ai/v1/chat/completions"


payload = {
    "model": "muse-spark-1.3",
    "messages": [
        {
            "role": "user",
            "content": "Reply with exactly one word: READY"
        }
    ],
    "reasoning_effort": "minimal",
    "max_completion_tokens": 256,
}


def main():
    if API_KEY == "PASTE_YOUR_META_API_KEY_HERE":
        print("ERROR: Paste your API key into API_KEY first.")
        return

    if any(ord(char) < 32 or ord(char) == 127 for char in API_KEY):
        print("ERROR: API key contains a control character.")
        return

    request = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)

        print("SUCCESS: Meta API responded.")
        print("Model:", result.get("model"))
        print("Reply:", result["choices"][0]["message"]["content"])
        print("Usage:", result.get("usage"))
        print("Response:", json.dumps(result, indent=2))

    except urllib.error.HTTPError as error:
        print("HTTP ERROR:", error.code)
        print(error.read().decode("utf-8", errors="replace")[:1500])

    except Exception as error:
        print("ERROR TYPE:", type(error).__name__)
        print("ERROR:", str(error))


if __name__ == "__main__":
    main()
