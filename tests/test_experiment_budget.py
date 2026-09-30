import json
from pathlib import Path
import tempfile
import unittest

import httpx

from evaluation.experiment_budget import ExperimentBudget, ExperimentBudgetExceeded


class TestExperimentBudget(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "ledger.json"
        self.prices = {"deepseek:deepseek-flash": {"input": 0.3, "output": 1.2}, "typesafe:jev-1.13.0": {"input": 0.042, "output": 0.0}, "gemini:gemini-embedding-001": {"input": 0.15, "output": 0.0}}

    def budget(self, limit=1.25):
        return ExperimentBudget(self.path, limit_usd=limit, prices=self.prices)

    def request(self, **changes):
        body = {"model": "deepseek-flash", "messages": [{"role": "user", "content": "private text"}], "max_tokens": 10, **changes}
        return httpx.Request("POST", "https://api.deepseek.com/chat/completions", headers={"Authorization": "Bearer private-secret"}, json=body)

    def test_refuses_call_before_dispatch_and_persists_stop(self):
        budget = self.budget(0.000001)
        dispatched = []
        with self.assertRaises(ExperimentBudgetExceeded):
            budget.send(lambda *args, **kwargs: dispatched.append(True), None, self.request())
        self.assertEqual(dispatched, [])
        self.assertEqual(budget.summary()["calls"], 0)
        self.assertTrue(self.budget(0.000001).summary()["blocked"])

    def test_releases_reservation_after_observed_usage_without_exposing_content(self):
        budget = self.budget()
        response = httpx.Response(200, json={"usage": {"prompt_tokens": 5, "completion_tokens": 3}, "secret": "private reply"})
        budget.send(lambda *args, **kwargs: response, None, self.request())
        self.assertAlmostEqual(budget.committed_usd, 0.0000051)
        persisted = self.path.read_text()
        for secret in ("private-secret", "private text", "private reply"):
            self.assertNotIn(secret, persisted)

    def test_unknown_usage_retains_reservation_after_restart(self):
        budget = self.budget()
        budget.send(lambda *args, **kwargs: httpx.Response(200, json={}), None, self.request())
        self.assertGreater(budget.committed_usd, 0)
        self.assertEqual(self.budget().committed_usd, budget.committed_usd)
        self.assertEqual(budget.summary()["usage_unknown_calls"], 1)

    def test_transport_failure_retains_reservation(self):
        budget = self.budget()
        def fail(*args, **kwargs):
            raise httpx.ReadTimeout("timeout")
        with self.assertRaises(httpx.ReadTimeout):
            budget.send(fail, None, self.request())
        self.assertGreater(budget.committed_usd, 0)
        self.assertEqual(budget.data["calls"][0]["status"], "transport_error_usage_unknown")

    def test_rejects_unknown_endpoint_and_unbounded_output(self):
        budget = self.budget()
        for request in (httpx.Request("POST", "https://example.com", json={}), self.request(max_tokens=None)):
            with self.assertRaises(ExperimentBudgetExceeded):
                budget.send(lambda *args, **kwargs: None, None, request)
        self.assertEqual(budget.summary()["calls"], 0)

    def test_bound_violation_stops_further_calls(self):
        budget = self.budget()
        budget.send(lambda *args, **kwargs: httpx.Response(200, json={"usage": {"prompt_tokens": 5, "completion_tokens": 20}}), None, self.request())
        self.assertTrue(budget.summary()["blocked"])
        with self.assertRaises(ExperimentBudgetExceeded):
            budget.send(lambda *args, **kwargs: None, None, self.request())

    def test_prices_and_limit_cannot_change_after_start(self):
        self.budget()
        with self.assertRaises(ValueError):
            self.budget(2.0)

    def test_embedding_without_usage_does_not_count_as_free(self):
        budget = self.budget()
        request = httpx.Request("POST", "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent", json={"content": {"parts": [{"text": "test"}]}})
        budget.send(lambda *args, **kwargs: httpx.Response(200, json={"embedding": {"values": [0.1]}}), None, request)
        self.assertEqual(budget.summary()["usage_unknown_calls"], 1)
        self.assertGreater(budget.committed_usd, 0)


if __name__ == "__main__":
    unittest.main()
