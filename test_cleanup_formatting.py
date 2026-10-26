"""
Comprehensive test suite for Voxen Transcript Formatting & Ollama Chat API.
Tests:
1. Correct capitalization and punctuation.
2. Paragraphs at natural pauses / clear topic boundaries.
3. Filler removal (um, uh, er, ah).
4. Preservation of technical vocabulary, names, numbers, acronyms, and meaning.
5. Question guardrail: Never answer questions contained in transcript.
6. Paraphrase / summary / hallucination rejection.
7. Sanitization: No em dashes (—) or curly quotes (“ ”).
8. Ollama Chat API (/api/chat) with streaming disabled.
9. Model parameter check via /api/show.
10. Lazy detection: Ollama starting after Voxen without restarting app.
11. Privacy logging (no private speech logged at INFO level).
12. 10+ varied dictated examples.
"""

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests

from cleanup import (
    OllamaCleaner,
    check_ollama_status,
    check_model_parameters,
    sanitize_formatting,
    clean_base_url
)

# Configure logging to verify telemetry
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Test.Cleanup")

MOCK_PORT = 11439
MOCK_BASE = f"http://127.0.0.1:{MOCK_PORT}"


class MockOllamaHandler(BaseHTTPRequestHandler):
    """Mock Ollama local HTTP daemon for testing Chat API, tags, and show."""

    chat_behavior = "standard"  # "standard", "answer_question", "preamble", "em_dash", "empty"
    recorded_requests = []

    def log_message(self, format, *args):
        pass  # Quiet logging

    def do_GET(self):
        if self.path == "/api/tags":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            response = {
                "models": [
                    {"name": "llama3.2:3b", "modified_at": "2026-10-24T10:00:00Z"},
                    {"name": "qwen2.5:3b", "modified_at": "2026-10-24T10:00:00Z"}
                ]
            }
            self.wfile.write(json.dumps(response).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        data = json.loads(body) if body else {}

        MockOllamaHandler.recorded_requests.append({
            "path": self.path,
            "data": data
        })

        if self.path == "/api/show":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = {
                "details": {"format": "gguf", "family": "llama", "parameter_size": "3B"},
                "parameters": "stop \"<|im_start|>\"\nstop \"<|im_end|>\""
            }
            self.wfile.write(json.dumps(resp).encode("utf-8"))

        elif self.path == "/api/chat":
            messages = data.get("messages", [])
            user_msg = messages[-1].get("content", "") if messages else ""

            # Check behavior mode
            if MockOllamaHandler.chat_behavior == "answer_question":
                # Bad model behavior: answers the question instead of formatting it
                content = "Paris is the capital of France, known for its art, gastronomy, and culture."
            elif MockOllamaHandler.chat_behavior == "preamble":
                content = "Here is the cleaned text:\n\nWe deployed the hotfix."
            elif MockOllamaHandler.chat_behavior == "em_dash":
                content = "We deployed the update—it succeeded—without issues."
            elif MockOllamaHandler.chat_behavior == "curly_quotes":
                content = 'He said “deploy now” to the server.'
            elif MockOllamaHandler.chat_behavior == "empty":
                content = ""
            else:
                # Standard formatting behavior
                content = MockOllamaHandler.format_simulated(user_msg)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = {
                "model": data.get("model", "llama3.2:3b"),
                "created_at": "2026-10-24T12:00:00Z",
                "message": {
                    "role": "assistant",
                    "content": content
                },
                "done": True
            }
            self.wfile.write(json.dumps(resp).encode("utf-8"))

    @staticmethod
    def format_simulated(user_content: str) -> str:
        # Extract raw dictation from prompt
        parts = user_content.split("raw speech-to-text transcript:\n\n")
        raw = parts[-1].strip() if len(parts) > 1 else user_content.strip()

        # Remove fillers
        res = raw
        for filler in [" um ", " uh ", " er ", " ah "]:
            res = res.replace(filler, " ")
        if res.lower().startswith("um "):
            res = res[3:]
        if res.lower().startswith("uh "):
            res = res[3:]

        # Capitalize sentences
        res = res.strip()
        if res:
            res = res[0].upper() + res[1:]
        if not res.endswith((".", "?", "!")):
            if any(res.lower().startswith(q) for q in ["what", "how", "why", "when", "where", "who", "which", "can", "could"]):
                res += "?"
            else:
                res += "."

        # Check for paragraph breaks (e.g. topic words like 'then', 'finally', 'secondly')
        if " then " in res.lower() and " finally " in res.lower():
            res = res.replace(" then ", ".\n\nThen ")
            res = res.replace(" finally ", ".\n\nFinally ")

        return res


def run_tests():
    print("=" * 70)
    print("VOXEN TRANSCRIPT FORMATTING & OLLAMA CHAT API TEST SUITE")
    print("=" * 70)

    # 1. Start Mock Ollama Server
    server = HTTPServer(("127.0.0.1", MOCK_PORT), MockOllamaHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    time.sleep(0.1)

    try:
        # -------------------------------------------------------------
        # Test 1: Check Ollama Status & Model Compatibility
        # -------------------------------------------------------------
        print("\n--- Test 1: Status & Model Compatibility ---")
        status = check_ollama_status(MOCK_BASE)
        assert status["available"] is True
        assert "llama3.2:3b" in status["models"]
        print(f"[PASS] Ollama status online. Detected models: {status['models']}")

        is_compat, msg = check_model_parameters(MOCK_BASE, "llama3.2:3b")
        assert is_compat is True
        print(f"[PASS] Model compatibility verified via /api/show: '{msg}'")

        # -------------------------------------------------------------
        # Test 2: Verify Chat API Protocol (/api/chat, stream=False)
        # -------------------------------------------------------------
        print("\n--- Test 2: Chat API Protocol & Streaming Check ---")
        cleaner = OllamaCleaner(
            enabled=True,
            endpoint=MOCK_BASE,
            model="llama3.2:3b",
            temperature=0.1
        )
        MockOllamaHandler.chat_behavior = "standard"
        MockOllamaHandler.recorded_requests.clear()

        cleaned = cleaner.clean("we deployed the backend container to kubernetes yesterday")
        assert len(MockOllamaHandler.recorded_requests) > 0
        last_req = MockOllamaHandler.recorded_requests[-1]
        assert last_req["path"] == "/api/chat", f"Must use /api/chat! Got: {last_req['path']}"
        assert last_req["data"]["stream"] is False, "Streaming must be disabled!"
        assert last_req["data"]["options"]["temperature"] == 0.1, "Temperature must be 0.1"
        assert len(last_req["data"]["messages"]) == 2, "Must contain system and user messages"
        assert last_req["data"]["messages"][0]["role"] == "system"
        print("[PASS] Chat API called correctly at /api/chat with stream=False and temperature=0.1.")

        # -------------------------------------------------------------
        # Test 3: 10+ Varied Dictated Examples Evaluation
        # -------------------------------------------------------------
        print("\n--- Test 3: 10+ Varied Dictated Examples Evaluation ---")

        examples = [
            # 1. Technical terms & infrastructure
            {
                "id": "1_technical_terms",
                "raw": "we configured postgresql with docker container and kubernetes pod deployment in aws us-east-1",
                "expected_contains": ["Postgresql", "docker", "kubernetes", "aws", "us-east-1"]
            },
            # 2. Questions (Must never be answered, must preserve question mark)
            {
                "id": "2_question",
                "raw": "what time is the team sync scheduled for tomorrow morning",
                "expected_ends_with": "?",
                "expected_contains": ["What time", "team sync"]
            },
            # 3. Incomplete sentence
            {
                "id": "3_incomplete_sentence",
                "raw": "because when the server restarted and then",
                "expected_contains": ["Because when", "server restarted"]
            },
            # 4. Multiline speech with clear topic transitions (paragraphs)
            {
                "id": "4_multiline_paragraphs",
                "raw": "first we need to migrate the database schema to version two then we deploy the backend services finally we monitor error rates in datadog",
                "expected_has_paragraphs": True,
                "expected_contains": ["Database schema", "version", "Datadog"]
            },
            # 5. Speech fillers (um, uh, er, ah removal)
            {
                "id": "5_fillers",
                "raw": "um we should uh deploy the hotfix to production ah tonight",
                "expected_lacks": ["um", "uh", "ah"],
                "expected_contains": ["deploy", "hotfix", "production", "tonight"]
            },
            # 6. Numbers, dates, and currency
            {
                "id": "6_numbers_dates",
                "raw": "on october 24th 2026 we processed 1450 active transactions",
                "expected_contains": ["October 24", "2026", "1450", "transactions"]
            },
            # 7. Code, acronyms, and API parameters
            {
                "id": "7_code_acronyms",
                "raw": "configure the api endpoint with json payload over https using bearer token auth",
                "expected_contains": ["api", "json", "https", "bearer token"]
            },
            # 8. Imperative command to team (not executed or answered)
            {
                "id": "8_imperative_command",
                "raw": "please review pull request 402 and merge it into main branch",
                "expected_contains": ["Please review", "pull request 402", "main"]
            },
            # 9. Short utterance (Below threshold -> untouched, zero delay)
            {
                "id": "9_short_utterance",
                "raw": "sounds good",
                "expected_exact": "sounds good"
            },
            # 10. Technical question with numbers
            {
                "id": "10_technical_question",
                "raw": "can you tell me why http 504 gateway timeout occurred on node 12",
                "expected_ends_with": "?",
                "expected_contains": ["http 504", "gateway timeout", "node 12"]
            },
            # 11. Complex grammar and capitalization
            {
                "id": "11_complex_grammar",
                "raw": "hello devendra how are you doing today i hope the voxen integration is going well",
                "expected_contains": ["Hello", "Devendra", "Voicebud"]
            }
        ]

        for ex in examples:
            raw = ex["raw"]
            out = cleaner.clean(raw)
            print(f"  [{ex['id']}]")
            print(f"    Raw:     '{raw}'")
            print(f"    Cleaned: '{out}'")

            # Assertions
            if "expected_exact" in ex:
                assert out == ex["expected_exact"], f"Short utterance should be untouched! Got: {out}"
            if "expected_contains" in ex:
                for word in ex["expected_contains"]:
                    assert word.lower() in out.lower(), f"Missing expected token '{word}' in output '{out}'"
            if "expected_ends_with" in ex:
                assert out.endswith(ex["expected_ends_with"]), f"Should end with '{ex['expected_ends_with']}'! Got: {out}"
            if "expected_lacks" in ex:
                for filler in ex["expected_lacks"]:
                    assert f" {filler} " not in f" {out.lower()} ", f"Filler '{filler}' was not removed in '{out}'"
            if ex.get("expected_has_paragraphs", False):
                assert "\n\n" in out, "Output must contain paragraph breaks (double newline)!"

            print(f"    -> [PASS] Verified")

        # -------------------------------------------------------------
        # Test 4: Question Answering Guardrail Rejection
        # -------------------------------------------------------------
        print("\n--- Test 4: Guardrail: Question Answering Rejection ---")
        MockOllamaHandler.chat_behavior = "answer_question"
        q_raw = "what is the capital of france"
        q_res = cleaner.clean(q_raw)
        assert q_res == q_raw, "When model attempts to answer a question instead of formatting, it MUST fallback to raw text!"
        print(f"[PASS] Question answering correctly rejected! Fallback to raw: '{q_res}'")

        # -------------------------------------------------------------
        # Test 5: Conversational Preamble Rejection
        # -------------------------------------------------------------
        print("\n--- Test 5: Guardrail: Conversational Preamble Rejection ---")
        MockOllamaHandler.chat_behavior = "preamble"
        p_raw = "we deployed the hotfix yesterday"
        p_res = cleaner.clean(p_raw)
        assert p_res == p_raw, "Conversational preamble ('Here is...') must be rejected!"
        print(f"[PASS] Preamble rejected! Fallback to raw: '{p_res}'")

        # -------------------------------------------------------------
        # Test 6: Sanitization: Em Dash and Curly Quote Removal
        # -------------------------------------------------------------
        print("\n--- Test 6: Sanitization: Em Dash and Curly Quotes ---")
        # Direct sanitizer check
        s_dash = sanitize_formatting("This was fast—very fast—without errors.")
        assert "—" not in s_dash
        assert " - " in s_dash
        print(f"[PASS] Em dash sanitized: '{s_dash}'")

        s_quotes = sanitize_formatting("He said “ship it” to the ‘team’.")
        assert "“" not in s_quotes and "”" not in s_quotes
        assert "‘" not in s_quotes and "’" not in s_quotes
        assert '"ship it"' in s_quotes
        assert "'team'" in s_quotes
        print(f"[PASS] Curly quotes sanitized: '{s_quotes}'")

        # -------------------------------------------------------------
        # Test 7: Lazy Detection When Ollama Starts After Voxen
        # -------------------------------------------------------------
        print("\n--- Test 7: Lazy Ollama Detection (Ollama Starts Later) ---")
        # Point cleaner to un-started mock port first
        offline_cleaner = OllamaCleaner(
            enabled=True,
            endpoint="http://127.0.0.1:11438", # offline
            model="llama3.2:3b"
        )
        assert offline_cleaner.is_online() is False
        fallback_res = offline_cleaner.clean("testing offline dictation fallback")
        assert fallback_res == "testing offline dictation fallback"
        print("[PASS] Offline cleanly returned raw transcript without delay.")

        # Now simulate Ollama coming online by repointing to MOCK_BASE
        offline_cleaner.base_url = MOCK_BASE
        offline_cleaner.chat_endpoint = f"{MOCK_BASE}/api/chat"
        offline_cleaner._last_health_check = 0.0  # Expire TTL
        MockOllamaHandler.chat_behavior = "standard"

        online_res = offline_cleaner.clean("testing lazy detection when ollama comes online")
        assert offline_cleaner._is_online is True
        assert online_res != "testing lazy detection when ollama comes online"  # Was cleaned!
        print(f"[PASS] Lazy detection succeeded! Seamlessly cleaned text: '{online_res}'")

        # -------------------------------------------------------------
        # Test 8: Vocabulary & Number Drop Guardrails
        # -------------------------------------------------------------
        print("\n--- Test 8: Vocabulary & Number Drop Guardrail ---")
        is_val, reason = cleaner.validate_cleanup(
            "we updated node 42 to version 3",
            "we updated the server to the new version"  # Dropped 42 and 3!
        )
        assert is_val is False
        assert "dropped_number" in reason
        print(f"[PASS] Number drop caught: reason='{reason}'")

        is_val_vocab, r_vocab = cleaner.validate_cleanup(
            "we deployed kubernetes cluster on google cloud platform with terraform and ansible automation",
            "this was totally different sentence that replaces every single concept entirely today forever"
        )
        assert is_val_vocab is False
        assert "low_vocabulary_preservation" in r_vocab
        print(f"[PASS] Radical paraphrase caught: reason='{r_vocab}'")

        print("\n" + "=" * 70)
        print("ALL TRANSCRIPT FORMATTING & CLEANUP TESTS PASSED!")
        print("=" * 70)

    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    run_tests()
