import json
import re
import unittest
from pathlib import Path

from discord.ext import commands

import bot
import rag


_SECRET_ERROR = (
    "falha em postgresql://admin:senha-super-secreta@db.interno:5432/base "
    "token=credencial-nao-publicavel"
)


class _ReplyContext:
    def __init__(self):
        self.replies = []

    async def reply(self, message):
        self.replies.append(message)


class TestOperationalErrorRedaction(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_command_error_returns_only_reference(self):
        ctx = _ReplyContext()
        error = commands.CommandInvokeError(RuntimeError(_SECRET_ERROR))

        with self.assertLogs(bot.logger, level="ERROR") as captured:
            await bot.on_command_error(ctx, error)

        response = ctx.replies[0]
        logs = "\n".join(captured.output)
        request_id = re.search(r"Referencia: `([0-9a-f]{32})`", response).group(1)

        self.assertNotIn("senha-super-secreta", response)
        self.assertNotIn("db.interno", response)
        self.assertNotIn("credencial-nao-publicavel", response)
        self.assertIn(f"request_id={request_id}", logs)
        self.assertIn("stage=comando", logs)
        self.assertIn("error_type=RuntimeError", logs)
        self.assertNotIn("senha-super-secreta", logs)
        self.assertNotIn("db.interno", logs)
        self.assertNotIn("credencial-nao-publicavel", logs)

    def test_ask_trace_uses_counts_instead_of_source_identifiers(self):
        trace = {
            "request_id": "req-seguro",
            "response_state": "grounded_answer",
            "retrieved_sources": ["cliente-interno.md"],
            "citations": ["cliente-interno.md"],
            "cited_files": ["cliente-interno.md"],
            "grounding_errors": ["fonte cliente-interno.md invalida"],
            "model_calls": [],
        }

        with self.assertLogs(rag.logger, level="INFO") as captured:
            rag._log_ask_trace(trace)

        payload = json.loads(captured.output[0].split("ASK_TRACE ", 1)[1])
        self.assertEqual(payload["request_id"], "req-seguro")
        self.assertEqual(payload["retrieved_source_count"], 1)
        self.assertEqual(payload["citation_count"], 1)
        self.assertNotIn("retrieved_sources", payload)
        self.assertNotIn("citations", payload)
        self.assertNotIn("cited_files", payload)
        self.assertNotIn("grounding_errors", payload)
        self.assertNotIn("cliente-interno.md", captured.output[0])


class TestComposeSecurityDefaults(unittest.TestCase):
    def test_postgres_requires_password_and_binds_to_loopback(self):
        compose = (Path(__file__).parents[1] / "docker-compose.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("${POSTGRES_PASSWORD:?", compose)
        self.assertIn("${POSTGRES_BIND_ADDRESS:-127.0.0.1}", compose)
        self.assertNotIn("POSTGRES_PASSWORD:-bot_maxima", compose)
        self.assertNotIn('"5432:5432"', compose)


if __name__ == "__main__":
    unittest.main()
