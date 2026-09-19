from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401 — installs the engine module path

from adw_modules import agent_pi  # noqa: E402


class ContextWindowTest(unittest.TestCase):

    def setUp(self) -> None:
        self.models_json = agent_pi.MODELS_JSON
        self.catalog = agent_pi._pi_catalog
        self.addCleanup(setattr, agent_pi, "MODELS_JSON", self.models_json)
        self.addCleanup(setattr, agent_pi, "_pi_catalog", self.catalog)

    def test_builtin_model_works_without_a_custom_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent_pi.MODELS_JSON = str(Path(tmp) / "missing.json")
            agent_pi._pi_catalog = lambda: [("openai-codex", "gpt-5.6-terra", 272_000)]
            self.assertEqual(
                agent_pi.context_window("openai-codex", "gpt-5.6-terra"), 272_000)

    def test_custom_registry_value_overrides_the_catalogue(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "models.json"
            registry.write_text(json.dumps({"providers": {"custom": {"models": [
                {"id": "local-model", "contextWindow": 65_536}
            ]}}}))
            agent_pi.MODELS_JSON = str(registry)
            agent_pi._pi_catalog = lambda: [("custom", "local-model", 32_000)]
            self.assertEqual(agent_pi.context_window("custom", "local-model"), 65_536)


if __name__ == "__main__":
    unittest.main()