import asyncio
import os
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import bot
import config
import db
import rag
from bot_common import ConversationManager


def _context(*, user_id: int, guild_id: int | None, channel_id: int, parent_id=None, admin=False):
    guild = SimpleNamespace(id=guild_id) if guild_id is not None else None
    channel = SimpleNamespace(
        id=channel_id,
        parent_id=parent_id,
        guild=guild,
    )

    class FakeContext:
        def __init__(self):
            self.author = SimpleNamespace(
                id=user_id,
                guild_permissions=SimpleNamespace(administrator=admin),
            )
            self.guild = guild
            self.channel = channel
            self.message = SimpleNamespace(id=999)
            self.replies = []

        async def reply(self, message=None, **kwargs):
            self.replies.append(message or kwargs)

    return FakeContext()


class TestConversationManager(unittest.IsolatedAsyncioTestCase):
    async def test_same_conversation_keeps_question_answer_pairs_in_order(self):
        manager = ConversationManager(max_conversations=10)
        key = ("discord", "guild", "1", "10", "")
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        second_entered = asyncio.Event()

        async def run(question, answer, *, wait=False):
            async with manager.serialized(key):
                if wait:
                    first_entered.set()
                    await release_first.wait()
                else:
                    second_entered.set()
                manager.append_exchange(key, question, answer)

        first = asyncio.create_task(run("q1", "a1", wait=True))
        await first_entered.wait()
        second = asyncio.create_task(run("q2", "a2"))
        await asyncio.sleep(0)
        self.assertFalse(second_entered.is_set())

        release_first.set()
        await asyncio.gather(first, second)

        async with manager.serialized(key):
            history = manager.get_history_snapshot(key)
        self.assertEqual(
            [(message["role"], message["content"]) for message in history],
            [
                ("user", "q1"),
                ("assistant", "a1"),
                ("user", "q2"),
                ("assistant", "a2"),
            ],
        )

    async def test_different_conversations_progress_independently(self):
        manager = ConversationManager(max_conversations=10)
        release_first = asyncio.Event()
        second_entered = asyncio.Event()

        async def first_operation():
            async with manager.serialized(("guild", "1", "10")):
                await release_first.wait()

        async def second_operation():
            async with manager.serialized(("guild", "1", "20")):
                second_entered.set()

        first = asyncio.create_task(first_operation())
        await asyncio.sleep(0)
        second = asyncio.create_task(second_operation())
        await asyncio.wait_for(second_entered.wait(), timeout=1)
        await second
        release_first.set()
        await first

    async def test_trim_uses_snapshots_and_lru_removes_state_with_lock(self):
        manager = ConversationManager(max_conversations=1)
        key = ("conversation", "one")
        with patch.object(config, "MAX_HISTORY_PAIRS", 1):
            async with manager.serialized(key):
                manager.append_exchange(key, "q1", "a1")
                old_snapshot = manager.get_history_snapshot(key)
                manager.append_exchange(key, "q2", "a2")
                current = manager.get_history_snapshot(key)

        self.assertEqual([message["content"] for message in old_snapshot], ["q1", "a1"])
        self.assertEqual([message["content"] for message in current], ["q2", "a2"])

        for index in range(5):
            async with manager.serialized(("conversation", index)):
                manager.append_exchange(("conversation", index), "q", "a")
        self.assertEqual(manager.conversation_count, 1)


class TestDiscordConversationScope(unittest.TestCase):
    def test_server_channel_thread_and_dm_have_distinct_keys(self):
        channel = _context(user_id=1, guild_id=100, channel_id=200)
        thread = _context(user_id=1, guild_id=100, channel_id=300, parent_id=200)
        dm = _context(user_id=1, guild_id=None, channel_id=400)

        self.assertEqual(
            bot._discord_conversation_key(channel),
            ("discord", "100", "200", ""),
        )
        self.assertEqual(
            bot._discord_conversation_key(thread),
            ("discord", "100", "200", "300"),
        )
        self.assertEqual(
            bot._discord_conversation_key(dm),
            ("discord", "", "400", ""),
        )

    def test_submission_defaults_to_current_conversation(self):
        thread = _context(user_id=1, guild_id=100, channel_id=300, parent_id=200)

        scope = rag._normalize_scope(bot._feedback_submission_scope(thread, {}))

        self.assertEqual(
            scope,
            {
                "level": "conversation",
                "platform": "discord",
                "guild_id": "100",
                "channel_id": "200",
                "thread_id": "300",
            },
        )


class TestFeedbackAuthorization(unittest.IsolatedAsyncioTestCase):
    async def test_guild_admin_cannot_approve_global_feedback(self):
        ctx = _context(user_id=10, guild_id=100, channel_id=200, admin=True)
        approve = Mock()

        with patch.object(config, "GLOBAL_FEEDBACK_REVIEWER_IDS", frozenset()), patch(
            "bot.rag.get_feedback_item",
            return_value={"id": "feedback-1", "scope": {"level": "global"}},
        ), patch("bot.rag.approve_feedback_item", approve):
            await bot.cmd_aprovar_correcao.callback(ctx, "feedback-1", note="")

        approve.assert_not_called()
        self.assertIn("permissao", ctx.replies[0].lower())

    async def test_authorized_global_reviewer_can_approve_from_dm(self):
        ctx = _context(user_id=99, guild_id=None, channel_id=400)
        approve = Mock()

        with patch.object(
            config,
            "GLOBAL_FEEDBACK_REVIEWER_IDS",
            frozenset({"99"}),
        ), patch(
            "bot.rag.get_feedback_item",
            return_value={"id": "feedback-1", "scope": {"level": "global"}},
        ), patch("bot.rag.approve_feedback_item", approve):
            await bot.cmd_aprovar_correcao.callback(ctx, "feedback-1", note="ok")

        approve.assert_called_once_with("feedback-1", "discord:99", "ok")

    async def test_dm_does_not_grant_global_publish_permission(self):
        ctx = _context(user_id=10, guild_id=None, channel_id=400)
        publish = Mock()

        with patch.object(config, "GLOBAL_FEEDBACK_REVIEWER_IDS", frozenset()), patch(
            "bot.rag.get_feedback_item",
            return_value={"id": "feedback-1", "scope": {"level": "global"}},
        ), patch("bot.rag.publish_feedback_item", publish):
            await bot.cmd_publicar_correcao.callback(ctx, "feedback-1")

        publish.assert_not_called()

    async def test_guild_admin_reviews_only_matching_conversation(self):
        ctx = _context(user_id=10, guild_id=100, channel_id=200, admin=True)
        own_scope = rag._normalize_scope(bot._discord_conversation_scope(ctx))
        other_scope = dict(own_scope, channel_id="201")

        with patch.object(config, "GLOBAL_FEEDBACK_REVIEWER_IDS", frozenset()):
            self.assertTrue(bot._can_review_feedback_scope(ctx, own_scope))
            self.assertFalse(bot._can_review_feedback_scope(ctx, other_scope))


class TestScopedFeedbackRetrieval(unittest.TestCase):
    @unittest.skipUnless(
        os.getenv("RUN_DB_INTEGRATION_TESTS") == "1",
        "Teste requer PostgreSQL real.",
    )
    def test_postgres_feedback_scope_fixture(self):
        fixture = Path(__file__).parent / "postgres" / "feedback_scope_fixture.sql"
        subprocess.run(
            [
                "psql",
                os.environ["DATABASE_URL"],
                "-v",
                "ON_ERROR_STOP=1",
                "-f",
                str(fixture),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_json_scope_filter_is_adapted_for_postgres(self):
        with patch("db._prepare_value", return_value="adapted") as prepare:
            clause, params = db._parse_filter("scope", {"level": "global"})

        self.assertEqual(clause, '"scope" = %s')
        self.assertEqual(params, ["adapted"])
        prepare.assert_called_once_with("scope", {"level": "global"})

    def test_conversation_search_uses_exact_scope_rpc(self):
        scope = {
            "level": "conversation",
            "platform": "discord",
            "guild_id": "100",
            "channel_id": "200",
            "thread_id": "300",
        }

        with patch("rag.ensure_embedding_index_identity"), patch(
            "rag._get_cached_query_embedding",
            return_value=[0.0] * config.EMBEDDING_DIMENSIONS,
        ), patch("rag.supabase_rpc", return_value=[]) as rpc:
            rag._search_feedback_memory_chunks(
                "pergunta",
                scope=scope,
                scope_level="conversation",
            )

        function_name, params = rpc.call_args.args
        self.assertEqual(function_name, "search_feedback_chunks_scoped")
        self.assertEqual(params["scope_filter"], scope)
        self.assertNotIn("scope_level", params)

    def test_pending_list_filters_by_exact_scope(self):
        scope = {
            "level": "conversation",
            "platform": "discord",
            "guild_id": "100",
            "channel_id": "200",
        }

        with patch("rag.supabase_select", return_value=[]) as select:
            rag.list_pending_feedback_items(10, scope=scope)

        self.assertEqual(select.call_args.kwargs["filters"]["scope"], scope)
