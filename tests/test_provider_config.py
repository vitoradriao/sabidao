import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import config
import rag


ROOT = Path(__file__).resolve().parents[1]


def _run_isolated_config(script: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Importa config.py sem permitir que o .env local afete o cenario."""
    with tempfile.TemporaryDirectory() as temp_dir:
        shutil.copy2(ROOT / "config.py", Path(temp_dir) / "config.py")
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=temp_dir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )


class TestEnvironmentPrecedence(unittest.TestCase):
    def test_openai_key_from_process_environment_is_recognized(self):
        fake_key = "credencial-ficticia-apenas-no-processo"
        env = os.environ.copy()
        for name in (
            "GENERATION_API_KEY",
            "EMBEDDING_API_KEY",
            "GENERATION_MODEL",
            "EMBEDDING_MODEL",
            "OPENAI_MODEL",
            "OPENAI_EMBEDDING_MODEL",
        ):
            env.pop(name, None)
        env.update(
            {
                "GENERATION_PROVIDER": "openai",
                "EMBEDDING_PROVIDER": "openai",
                "OPENAI_API_KEY": fake_key,
            }
        )

        result = _run_isolated_config(
            (
                "import config; "
                "assert config.GENERATION_API_KEY == "
                "'credencial-ficticia-apenas-no-processo'; "
                "assert config.EMBEDDING_API_KEY == "
                "'credencial-ficticia-apenas-no-processo'"
            ),
            env,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(fake_key, result.stdout)
        self.assertNotIn(fake_key, result.stderr)

    def test_legacy_provider_config_is_preserved_with_safe_warning(self):
        fake_key = "segredo-legado-ficticio"
        env = os.environ.copy()
        for name in (
            "GENERATION_API_KEY",
            "EMBEDDING_API_KEY",
            "GENERATION_MODEL",
            "GENERATION_BASE_URL",
            "EMBEDDING_BASE_URL",
        ):
            env.pop(name, None)
        env.update(
            {
                "GENERATION_PROVIDER": "",
                "EMBEDDING_PROVIDER": "",
                "LLM_PROVIDER": "openai",
                "OPENAI_API_KEY": fake_key,
                "OPENAI_MODEL": "modelo-geracao-legado",
                "EMBEDDING_MODEL": "gemini-embedding-001",
                "OPENAI_EMBEDDING_MODEL": "modelo-embedding-legado",
            }
        )

        result = _run_isolated_config(
            (
                "import config; "
                "assert config.GENERATION_PROVIDER == 'openai'; "
                "assert config.EMBEDDING_PROVIDER == 'openai'; "
                "assert config.GENERATION_MODEL == 'modelo-geracao-legado'; "
                "assert config.EMBEDDING_MODEL == 'modelo-embedding-legado'"
            ),
            env,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("LLM_PROVIDER", result.stderr)
        self.assertNotIn(fake_key, result.stdout)
        self.assertNotIn(fake_key, result.stderr)


class TestProviderIsolation(unittest.TestCase):
    def test_generation_endpoint_and_model_do_not_change_embeddings(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]
        }
        response.raise_for_status.return_value = None
        client = Mock()
        client.post.return_value = response

        with patch.multiple(
            config,
            GENERATION_BASE_URL="https://generation.example/v1",
            GENERATION_MODEL="generation-model",
            GENERATION_API_KEY="generation-secret",
            EMBEDDING_PROVIDER="openai",
            EMBEDDING_BASE_URL="https://embeddings.example/v1",
            EMBEDDING_MODEL="embedding-model",
            EMBEDDING_API_KEY="embedding-secret",
            EMBEDDING_DIMENSIONS=3,
        ), patch("rag._get_http_client", return_value=client):
            vectors = rag.create_embeddings(["conteudo"])

        self.assertEqual(vectors, [[0.1, 0.2, 0.3]])
        request = client.post.call_args
        self.assertEqual(request.args[0], "https://embeddings.example/v1/embeddings")
        self.assertEqual(request.kwargs["json"]["model"], "embedding-model")
        self.assertEqual(
            request.kwargs["headers"]["Authorization"],
            "Bearer embedding-secret",
        )
        self.assertNotIn("generation-secret", str(request))


class TestProviderValidation(unittest.TestCase):
    def test_rejects_unsupported_3072_dimension_profile(self):
        env = os.environ.copy()
        env["EMBEDDING_DIMENSIONS"] = "3072"

        result = _run_isolated_config("import config", env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("deve ser 1536 nesta versao", result.stderr)
        self.assertIn("memoria de feedback permanece em 1536", result.stderr)

    def test_invalid_endpoint_fails_without_revealing_credentials(self):
        generation_secret = "segredo-geracao"
        embedding_secret = "segredo-embedding"
        with patch.multiple(
            config,
            DISCORD_TOKEN="discord-ficticio",
            GENERATION_PROVIDER="openai",
            GENERATION_API_KEY=generation_secret,
            GENERATION_MODEL="gpt-test",
            GENERATION_BASE_URL="endpoint-invalido",
            EMBEDDING_PROVIDER="gemini",
            EMBEDDING_API_KEY=embedding_secret,
            EMBEDDING_MODEL="embedding-test",
            RAG_ENABLE_BUSINESS_RULES=False,
        ):
            with self.assertRaises(EnvironmentError) as raised:
                config.validate()

        message = str(raised.exception)
        self.assertIn("GENERATION_BASE_URL", message)
        self.assertIn("URL HTTP(S) absoluta", message)
        self.assertNotIn(generation_secret, message)
        self.assertNotIn(embedding_secret, message)


if __name__ == "__main__":
    unittest.main()
