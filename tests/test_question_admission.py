import asyncio
import threading
import unittest
from contextlib import AbstractAsyncContextManager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import bot
import config
import rag
from bot_common import ConversationManager, InFlightTaskLimiter


class _TypingContext(AbstractAsyncContextManager):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Target:
    def __init__(self, channel_id: int):
        guild = SimpleNamespace(id=100)
        self.guild = guild
        self.channel = SimpleNamespace(
            id=channel_id,
            parent_id=None,
            guild=guild,
            typing=lambda: _TypingContext(),
        )
        self.replies: list[str] = []

    async def reply(self, message=None, **kwargs):
        self.replies.append(message or kwargs)


class TestQuestionAdmission(unittest.IsolatedAsyncioTestCase):
    async def test_same_conversation_rejects_excess_without_queue(self):
        manager = ConversationManager(max_conversations=10)
        first_entered = asyncio.Event()
        release_first = asyncio.Event()

        async def hold_first(*_args, **_kwargs):
            first_entered.set()
            await release_first.wait()

        first_target = _Target(10)
        second_target = _Target(10)
        with (
            patch.object(bot, "_conv", manager),
            patch.object(config, "COOLDOWN_SECONDS", 10.0),
            patch.object(
                bot,
                "_handle_serialized_question",
                side_effect=hold_first,
            ) as handler,
        ):
            first = asyncio.create_task(
                bot.handle_question(first_target, 1, "primeira pergunta")
            )
            await first_entered.wait()
            try:
                await asyncio.wait_for(
                    bot.handle_question(second_target, 1, "segunda pergunta"),
                    timeout=0.1,
                )
            finally:
                release_first.set()
                await first

        self.assertEqual(second_target.replies, [bot._CONVERSATION_BUSY_MESSAGE])
        self.assertEqual(handler.await_count, 1)

    async def test_deadline_is_derived_from_question_arrival(self):
        manager = ConversationManager(max_conversations=10)
        captured = {}

        async def capture_deadline(*_args, **kwargs):
            captured.update(kwargs)

        with (
            patch.object(bot, "_conv", manager),
            patch.object(manager, "check_cooldown", return_value=None),
            patch.object(config, "ASK_TIMEOUT_SECONDS", 5.0),
            patch.object(bot.time, "monotonic", return_value=100.0),
            patch.object(
                bot,
                "_handle_serialized_question",
                side_effect=capture_deadline,
            ),
        ):
            await bot.handle_question(_Target(10), 1, "pergunta")

        self.assertEqual(captured["arrived_at"], 100.0)
        self.assertEqual(captured["deadline"], 105.0)

    async def test_expired_request_does_not_call_rag(self):
        manager = ConversationManager(max_conversations=10)
        target = _Target(10)

        with (
            patch.object(bot, "_conv", manager),
            patch.object(manager, "check_cooldown", return_value=None),
            patch.object(config, "ASK_TIMEOUT_SECONDS", 1.0),
            patch.object(bot.time, "monotonic", side_effect=(100.0, 102.0)),
            patch.object(bot.rag, "ask") as ask,
        ):
            await bot.handle_question(target, 1, "pergunta expirada")

        ask.assert_not_called()
        self.assertEqual(target.replies, [bot._ASK_TIMEOUT_MESSAGE])

    async def test_thread_rechecks_deadline_before_calling_rag(self):
        ask = Mock()
        with (
            patch.object(bot.time, "monotonic", return_value=10.0),
            patch.object(bot.rag, "ask", ask),
            self.assertRaises(rag.RequestDeadlineExceeded),
        ):
            bot._ask_before_deadline(
                question="pergunta",
                history=[],
                images=None,
                conversation_scope={"level": "conversation"},
                deadline=10.0,
            )

        ask.assert_not_called()

    async def test_different_conversations_progress_within_global_limit(self):
        manager = ConversationManager(max_conversations=10)
        limiter = InFlightTaskLimiter(2)
        started = threading.Event()
        release = threading.Event()
        running_questions: set[str] = set()
        running_lock = threading.Lock()

        def ask(question, *_args, **_kwargs):
            with running_lock:
                running_questions.add(question)
                if len(running_questions) == 2:
                    started.set()
            release.wait(timeout=2)
            return f"resposta para {question}", [], {"top_similarity": 1.0}

        send_response = AsyncMock()
        with (
            patch.object(bot, "_conv", manager),
            patch.object(bot, "_rag_tasks", limiter),
            patch.object(manager, "check_cooldown", return_value=None),
            patch.object(config, "ASK_TIMEOUT_SECONDS", 2.0),
            patch.object(bot.rag, "ask", side_effect=ask),
            patch.object(bot, "send_split_response", send_response),
        ):
            first = asyncio.create_task(
                bot.handle_question(_Target(10), 1, "pergunta um")
            )
            second = asyncio.create_task(
                bot.handle_question(_Target(20), 2, "pergunta dois")
            )
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                self.assertEqual(limiter.active_count, 2)
            finally:
                release.set()
                await asyncio.gather(first, second)

        self.assertEqual(running_questions, {"pergunta um", "pergunta dois"})
        self.assertEqual(send_response.await_count, 2)

    async def test_timeout_releases_conversation_but_keeps_worker_counted(self):
        manager = ConversationManager(max_conversations=10)
        limiter = InFlightTaskLimiter(1)
        started = threading.Event()
        release = threading.Event()

        def slow_ask(*_args, **_kwargs):
            started.set()
            release.wait(timeout=2)
            return "resposta tardia", [], {"top_similarity": 1.0}

        first_target = _Target(10)
        second_target = _Target(10)
        with (
            patch.object(bot, "_conv", manager),
            patch.object(bot, "_rag_tasks", limiter),
            patch.object(manager, "check_cooldown", return_value=None),
            patch.object(config, "ASK_TIMEOUT_SECONDS", 0.05),
            patch.object(bot.rag, "ask", side_effect=slow_ask),
        ):
            try:
                await bot.handle_question(first_target, 1, "pergunta lenta")
                self.assertTrue(started.is_set())
                self.assertEqual(first_target.replies, [bot._ASK_TIMEOUT_MESSAGE])
                self.assertEqual(limiter.active_count, 1)

                state = manager._states[bot._discord_conversation_key(first_target)]
                self.assertEqual(state.users, 0)
                self.assertFalse(state.lock.locked())

                await bot.handle_question(second_target, 2, "nova pergunta")
                self.assertEqual(len(second_target.replies), 1)
                self.assertIn("limite de consultas simultaneas", second_target.replies[0])
                self.assertEqual(limiter.active_count, 1)
            finally:
                release.set()
                for _ in range(100):
                    if limiter.active_count == 0:
                        break
                    await asyncio.sleep(0.01)

        self.assertEqual(limiter.active_count, 0)

    async def test_cancellation_releases_conversation_once(self):
        manager = ConversationManager(max_conversations=10)
        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        calls = 0

        async def cancellable_handler(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_entered.set()
                await asyncio.Future()
            second_entered.set()

        first_target = _Target(10)
        with (
            patch.object(bot, "_conv", manager),
            patch.object(manager, "check_cooldown", return_value=None),
            patch.object(
                bot,
                "_handle_serialized_question",
                side_effect=cancellable_handler,
            ),
        ):
            first = asyncio.create_task(
                bot.handle_question(first_target, 1, "primeira pergunta")
            )
            await first_entered.wait()
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first

            await bot.handle_question(_Target(10), 2, "segunda pergunta")

        self.assertTrue(second_entered.is_set())
        state = manager._states[bot._discord_conversation_key(first_target)]
        self.assertEqual(state.users, 0)
        self.assertFalse(state.lock.locked())

    async def test_error_releases_conversation_once(self):
        manager = ConversationManager(max_conversations=10)
        handler = AsyncMock(side_effect=(RuntimeError("falha"), None))
        target = _Target(10)

        with (
            patch.object(bot, "_conv", manager),
            patch.object(manager, "check_cooldown", return_value=None),
            patch.object(bot, "_handle_serialized_question", handler),
        ):
            with self.assertRaisesRegex(RuntimeError, "falha"):
                await bot.handle_question(target, 1, "primeira pergunta")
            await bot.handle_question(_Target(10), 2, "segunda pergunta")

        self.assertEqual(handler.await_count, 2)
        state = manager._states[bot._discord_conversation_key(target)]
        self.assertEqual(state.users, 0)
        self.assertFalse(state.lock.locked())


if __name__ == "__main__":
    unittest.main()
