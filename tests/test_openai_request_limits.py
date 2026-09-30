import unittest
from unittest.mock import Mock, patch

import httpx

import config
import rag


class TestChatRequestLimits(unittest.TestCase):
    def response(self, status=200, **body):
        return httpx.Response(
            status,
            json=body or {"choices": [{"message": {"content": "ok"}}]},
            request=httpx.Request("POST", "https://example.com/chat/completions"),
        )

    def generate(self, base_url, client):
        with patch.multiple(
            config, GENERATION_BASE_URL=base_url, GENERATION_API_KEY="fake-key"
        ), patch.object(rag, "_get_http_client", return_value=client):
            return rag._openai_chat_generate_request(
                model="fixture", messages=[{"role": "user", "content": "fixture"}],
                max_tokens=200,
            )

    def test_deepseek_uses_documented_limit_on_first_dispatch(self):
        for base_url in ("https://api.deepseek.com", "https://api.deepseek.com/v1"):
            with self.subTest(base_url=base_url):
                client = Mock()
                client.post.return_value = self.response()
                self.generate(base_url, client)
                self.assertEqual(client.post.call_count, 1)
                payload = client.post.call_args.kwargs["json"]
                self.assertEqual(payload["max_tokens"], 200)
                self.assertNotIn("max_completion_tokens", payload)

    def test_other_hosts_preserve_max_completion_tokens(self):
        for base_url in ("https://api.openai.com/v1", "https://api.deepseek.com.example/v1"):
            with self.subTest(base_url=base_url):
                client = Mock()
                client.post.return_value = self.response()
                self.generate(base_url, client)
                payload = client.post.call_args.kwargs["json"]
                self.assertEqual(payload["max_completion_tokens"], 200)
                self.assertNotIn("max_tokens", payload)

    def test_generic_compatibility_fallback_keeps_requested_limit(self):
        payloads = []
        def post(*args, **kwargs):
            payloads.append(dict(kwargs["json"]))
            if len(payloads) == 1:
                return self.response(400, error="unsupported max_completion_tokens")
            return self.response()
        client = Mock()
        client.post.side_effect = post
        self.generate("https://compatible.example/v1", client)
        self.assertEqual(payloads[0]["max_completion_tokens"], 200)
        self.assertEqual(payloads[1]["max_tokens"], 200)
        self.assertNotIn("max_completion_tokens", payloads[1])

    def test_deepseek_error_does_not_repeat_same_payload(self):
        client = Mock()
        client.post.return_value = self.response(400, error="max_completion_tokens unsupported")
        with self.assertRaises(httpx.HTTPStatusError):
            self.generate("https://api.deepseek.com", client)
        self.assertEqual(client.post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
