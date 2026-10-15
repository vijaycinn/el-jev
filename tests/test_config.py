import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eljev import config


class ConfigTests(unittest.TestCase):
    ENV_KEYS = (
        "ELJEV_DIR",
        "ELJEV_LOG_DIR",
        "EL_JEV",
        "ELJEV_ENABLED",
        "ELJEV_HOOK_ENABLED",
        "ELJEV_LOGGING",
        "ELJEV_NO_LOG",
        "ELJEV_COHERE_ENDPOINT",
        "ELJEV_COHERE_DEPLOYMENT",
        "ELJEV_COVERAGE_POLICY",
        "ELJEV_AUTH",
        "ELJEV_HOOK_MODE",
        "ELJEV_HOOK_TIMEOUT_MS",
        "ELJEV_CALIBRATION_PATH",
        "ELJEV_ENGINE",
        "ELJEV_MD1_ENDPOINT",
        "ELJEV_MD1_DEPLOYMENT",
        "ELJEV_MD1_TIMEOUT_MS",
        "ELJEV_TIMEOUT_MS",
        "ELJEV_EXTENSION_MODE",
    )

    def setUp(self):
        self._environment = patch.dict(os.environ, {}, clear=False)
        self._environment.start()
        self._tempdir = tempfile.TemporaryDirectory()
        self._clear_relevant_env()
        os.environ["ELJEV_DIR"] = self._tempdir.name

    def tearDown(self):
        self._environment.stop()
        self._tempdir.cleanup()

    def _clear_relevant_env(self):
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)

    def test_eljev_dir_uses_env_override(self):
        custom = Path(self._tempdir.name) / "custom"
        os.environ["ELJEV_DIR"] = str(custom)
        self.assertEqual(config.eljev_dir(), custom.expanduser())
        self.assertTrue(custom.exists())

    def test_eljev_dir_defaults_under_repo_root_when_env_unset(self):
        os.environ.pop("ELJEV_DIR", None)
        expected = config.repo_root() / ".eljev"
        self.assertEqual(config.eljev_dir(), expected)
        self.assertTrue(expected.exists())

    def test_save_load_round_trip_and_merge(self):
        first = config.save_config({"enabled": False, "auth": "managed_identity"})
        self.assertEqual(first["enabled"], False)
        self.assertEqual(first["auth"], "managed_identity")

        second = config.save_config({"hook_mode": "marker"})
        self.assertEqual(second["enabled"], False)
        self.assertEqual(second["auth"], "managed_identity")
        self.assertEqual(second["hook_mode"], "marker")
        self.assertEqual(config.load_config(), second)

    def test_load_config_invalid_json_returns_empty_mapping(self):
        path = config.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{", encoding="utf-8")
        self.assertEqual(config.load_config(), {})

    def test_parse_switch_table(self):
        cases = [
            (True, True),
            (False, False),
            (" On ", True),
            ("enabled", True),
            ("TRUE", True),
            (" 0 ", False),
            ("disable", False),
            ("No", False),
            (None, None),
            (1, None),
            (object(), None),
            ("perhaps", None),
            ("", None),
            ("   ", None),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(config.parse_switch(value), expected)

    def test_enabled_state_prefers_el_jev(self):
        os.environ["EL_JEV"] = "off"
        os.environ["ELJEV_ENABLED"] = "on"
        config.save_config({"enabled": True})
        with patch("eljev.config.user_env_value", return_value="on"):
            self.assertEqual(config.enabled_state(), (False, "env:EL_JEV"))

    def test_enabled_state_skips_unparseable_el_jev_and_uses_eljev_enabled(self):
        os.environ["EL_JEV"] = "maybe"
        os.environ["ELJEV_ENABLED"] = "true"
        with patch("eljev.config.user_env_value", return_value="off"):
            self.assertEqual(config.enabled_state(), (True, "env:ELJEV_ENABLED"))

    def test_enabled_state_uses_user_env_when_process_env_absent(self):
        os.environ.pop("EL_JEV", None)
        os.environ.pop("ELJEV_ENABLED", None)
        config.save_config({"enabled": True})
        with patch("eljev.config.user_env_value", return_value="off"):
            self.assertEqual(config.enabled_state(), (False, "user-env:EL_JEV"))

    def test_enabled_state_skips_unparseable_higher_levels_and_uses_config(self):
        os.environ["EL_JEV"] = "??"
        os.environ["ELJEV_ENABLED"] = "??"
        config.save_config({"enabled": "off"})
        with patch("eljev.config.user_env_value", return_value="unknown"):
            self.assertEqual(config.enabled_state(), (False, "config"))

    def test_enabled_state_defaults_when_nothing_parseable(self):
        os.environ.pop("EL_JEV", None)
        os.environ.pop("ELJEV_ENABLED", None)
        with patch("eljev.config.user_env_value", return_value=None):
            self.assertEqual(config.enabled_state(), (True, "default"))

    def test_logging_enabled_and_legacy_flag(self):
        os.environ["ELJEV_LOGGING"] = "off"
        os.environ["ELJEV_NO_LOG"] = "0"
        self.assertFalse(config.logging_enabled())

        os.environ["ELJEV_LOGGING"] = "unparseable"
        os.environ["ELJEV_NO_LOG"] = "TRUE"
        self.assertFalse(config.logging_enabled())

        os.environ["ELJEV_NO_LOG"] = "no"
        self.assertTrue(config.logging_enabled())

    def test_cohere_endpoint_trims_trailing_slashes_and_env_overrides_config(self):
        config.save_config({"cohere_endpoint": " https://cfg.example.com/path/// "})
        self.assertEqual(config.cohere_endpoint(), "https://cfg.example.com/path")

        os.environ["ELJEV_COHERE_ENDPOINT"] = " https://env.example.com/route// "
        self.assertEqual(config.cohere_endpoint(), "https://env.example.com/route")

        os.environ["ELJEV_COHERE_ENDPOINT"] = ""
        self.assertEqual(config.cohere_endpoint(), "https://cfg.example.com/path")

    def test_auth_mode_and_hook_mode_unknown_fallbacks(self):
        config.save_config({"auth": "unknown", "hook_mode": "unknown"})
        self.assertEqual(config.auth_mode(), "azcli")
        self.assertEqual(config.hook_mode(), "intent")

        os.environ["ELJEV_AUTH"] = "MANAGED_IDENTITY"
        os.environ["ELJEV_HOOK_MODE"] = "MARKER"
        self.assertEqual(config.auth_mode(), "managed_identity")
        self.assertEqual(config.hook_mode(), "marker")

    def test_hook_timeout_ms(self):
        self.assertEqual(config.hook_timeout_ms(), 750)
        config.save_config({"hook_timeout_ms": 1000})
        self.assertEqual(config.hook_timeout_ms(), 1000)
        os.environ["ELJEV_HOOK_TIMEOUT_MS"] = "800"
        self.assertEqual(config.hook_timeout_ms(), 800)
        os.environ["ELJEV_HOOK_TIMEOUT_MS"] = "invalid"
        self.assertEqual(config.hook_timeout_ms(), 1000)

    def test_calibration_path_override(self):
        os.environ.pop("ELJEV_CALIBRATION_PATH", None)
        self.assertEqual(
            config.calibration_path(),
            config.repo_root() / "eval" / "calibration.json",
        )

        custom = Path(self._tempdir.name) / "calibration.json"
        os.environ["ELJEV_CALIBRATION_PATH"] = str(custom)
        self.assertEqual(config.calibration_path(), custom)

    def test_normalize_md1_endpoint(self):
        self.assertEqual(
            config.normalize_md1_endpoint("https://example.services.ai.azure.com"),
            "https://example.services.ai.azure.com",
        )
        self.assertEqual(
            config.normalize_md1_endpoint(
                "https://example.services.ai.azure.com/providers/microsoft/v1/systemone"
            ),
            "https://example.services.ai.azure.com",
        )
        with self.assertRaisesRegex(ValueError, "must end with"):
            config.normalize_md1_endpoint("https://evil.example.com")
        with self.assertRaisesRegex(ValueError, "unexpected md1 endpoint path"):
            config.normalize_md1_endpoint("https://example.services.ai.azure.com/custom/path")

    def test_engine_resolution(self):
        self.assertEqual(config.engine(), "cohere")
        config.save_config({"engine": "md1"})
        self.assertEqual(config.engine(), "md1")
        os.environ["ELJEV_ENGINE"] = "COHERE"
        self.assertEqual(config.engine(), "cohere")
        os.environ["ELJEV_ENGINE"] = "invalid"
        self.assertEqual(config.engine(), "cohere")

    def test_md1_endpoint_resolution(self):
        self.assertEqual(config.md1_endpoint(), "")
        config.save_config({"md1_endpoint": "https://config.services.ai.azure.com/"})
        self.assertEqual(config.md1_endpoint(), "https://config.services.ai.azure.com")
        config.save_config({"md1_endpoint": "https://evil.example.com"})
        self.assertEqual(config.md1_endpoint(), "https://evil.example.com")
        os.environ["ELJEV_MD1_ENDPOINT"] = " https://env.services.ai.azure.com/ "
        self.assertEqual(config.md1_endpoint(), "https://env.services.ai.azure.com")
        os.environ["ELJEV_MD1_ENDPOINT"] = ""
        self.assertEqual(config.md1_endpoint(), "https://evil.example.com")

    def test_md1_deployment(self):
        self.assertEqual(config.md1_deployment(), "Decision-1")
        config.save_config({"md1_deployment": "Decision-1-Config"})
        self.assertEqual(config.md1_deployment(), "Decision-1-Config")
        os.environ["ELJEV_MD1_DEPLOYMENT"] = "Decision-1-Env"
        self.assertEqual(config.md1_deployment(), "Decision-1-Env")

    def test_md1_timeout_ms(self):
        self.assertEqual(config.md1_timeout_ms(), 2500)
        config.save_config({"md1_timeout_ms": 1800})
        self.assertEqual(config.md1_timeout_ms(), 1800)
        config.save_config({"md1_timeout_ms": "2400"})
        self.assertEqual(config.md1_timeout_ms(), 2400)
        os.environ["ELJEV_TIMEOUT_MS"] = "3100"
        self.assertEqual(config.md1_timeout_ms(), 3100)
        os.environ["ELJEV_MD1_TIMEOUT_MS"] = "2900"
        self.assertEqual(config.md1_timeout_ms(), 2900)
        os.environ["ELJEV_MD1_TIMEOUT_MS"] = "invalid"
        self.assertEqual(config.md1_timeout_ms(), 3100)

    def test_extension_mode(self):
        self.assertEqual(config.extension_mode(), "advisory")
        config.save_config({"extension_mode": "enforce"})
        self.assertEqual(config.extension_mode(), "enforce")
        os.environ["ELJEV_EXTENSION_MODE"] = "ADVISORY"
        self.assertEqual(config.extension_mode(), "advisory")
        os.environ["ELJEV_EXTENSION_MODE"] = "unknown"
        self.assertEqual(config.extension_mode(), "advisory")


if __name__ == "__main__":
    unittest.main()
