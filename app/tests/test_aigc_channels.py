"""AIGC channel secrecy, health routing and failover tests."""

from __future__ import annotations

import base64
import io
import json
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from PIL import Image

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import aigc_client
from aigc_channels import AIGCChannels, ChannelConfigError, DEFAULT_BASE_URL, ARK_BASE_URL, channel_api_key


class AIGCChannelsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aigc-channels-")
        self.addCleanup(self.temp.cleanup)
        self.channels = AIGCChannels(self.temp.name, monitor_interval=20)

    def save_channels(self, items):
        with patch.object(self.channels, "_ensure_monitor"):
            return self.channels.save({"channels": items})

    @staticmethod
    def channel(channel_id, *, priority=100, models=None, key="secret-" + "key"):
        return {
            "id": channel_id,
            "name": channel_id,
            "base_url": "https://%s.example/v1" % channel_id,
            "models": models or ["model-a"],
            "api_key": key,
            "priority": priority,
            "enabled": True,
        }

    def test_secrets_are_not_returned_and_blank_key_keeps_saved_value(self):
        self.save_channels([self.channel("primary")])
        public = self.channels.public_status()
        self.assertTrue(public["channels"][0]["key_configured"])
        self.assertNotIn("api_key", public["channels"][0])
        self.assertNotIn("secret-key", json.dumps(public))

        replacement = self.channel("primary", key="")
        self.save_channels([replacement])
        self.assertEqual(self.channels.configs()[0]["api_key"], "secret-key")

    def test_public_status_exposes_example_presets_without_credentials(self):
        public = self.channels.public_status()
        presets = public["model_presets"]
        self.assertEqual(len(presets), 3)
        self.assertEqual({item["base_url"] for item in presets}, {DEFAULT_BASE_URL})
        self.assertEqual([item["model"] for item in presets], [
            "gpt-image-1", "gemini-2.5-flash-image", "seedream-4-0-250828",
        ])
        self.assertNotIn("api_key", json.dumps(presets))

    def test_ark_environment_key_is_used_without_exposing_it(self):
        item = dict(self.channel("ark", key=""), base_url=ARK_BASE_URL,
                    model="seedream-4-0-250828")
        with patch.dict("os.environ", {"ARK_API_KEY": "environment-secret"}):
            self.assertEqual(channel_api_key(item), "environment-secret")
            self.assertEqual(channel_api_key(dict(item, api_key="saved-secret")), "saved-secret")
            public = self.save_channels([item])
            self.assertTrue(public["channels"][0]["key_configured"])
            self.assertNotIn("environment-secret", json.dumps(public))
            self.assertEqual(self.channels.candidates()[0][0]["id"], "ark")

    def test_ark_image_edit_uses_generation_endpoint_and_input_image(self):
        output = io.BytesIO()
        Image.new("RGB", (12, 16), "teal").save(output, "PNG")
        response_body = json.dumps({"data": [{
            "b64_json": base64.b64encode(output.getvalue()).decode("ascii")
        }]}).encode()

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return response_body

        with patch("aigc_client.urllib.request.urlopen", return_value=Response()) as transport:
            image = aigc_client._request_edit(
                b"jpeg-input", prompt="portrait", model="seedream-4-0-250828",
                size="1024x1536", endpoint_base=ARK_BASE_URL, api_key="ark-secret", timeout=300)
        request = transport.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, ARK_BASE_URL + "/images/generations")
        self.assertEqual(request.get_header("Authorization"), "Bearer ark-secret")
        self.assertEqual(payload["model"], "seedream-4-0-250828")
        self.assertEqual(payload["size"], "2K")
        self.assertIs(payload["watermark"], True)
        self.assertEqual(payload["image"], "data:image/jpeg;base64," +
                         base64.b64encode(b"jpeg-input").decode("ascii"))
        self.assertEqual(image.size, (12, 16))

    def test_validation_rejects_invalid_selected_model_and_credential_urls(self):
        for model in ("", "bad model", "model\nother", "a" * 129):
            with self.subTest(model=model), self.assertRaises(ChannelConfigError):
                self.save_channels([dict(self.channel("primary"), model=model)])
        invalid = self.channel("primary")
        invalid["base_url"] = "https://user:password@example.com/v1"
        with self.assertRaises(ChannelConfigError):
            self.save_channels([invalid])

    def test_legacy_models_use_first_entry_and_save_canonical_model(self):
        item = self.channel("primary", models=["model-a", "model-b", "model-a"])
        with patch.object(Path, "read_text", return_value=json.dumps({"channels": [item]})):
            self.assertEqual(self.channels.get("primary")["model"], "model-a")
            self.assertEqual([model for _, model in self.channels.candidates()], ["model-a"])
        public = self.save_channels([item])["channels"][0]
        saved = json.loads(self.channels.path.read_text(encoding="utf-8"))["channels"][0]
        self.assertEqual(saved["model"], "model-a")
        self.assertNotIn("models", saved)
        self.assertEqual(public["model"], "model-a")
        self.assertEqual(public["models"], ["model-a"])

    def test_canonical_model_wins_and_legacy_string_uses_first_entry(self):
        public = self.save_channels([
            dict(self.channel("primary"), model="selected"),
            dict(self.channel("backup"), models="legacy-first,legacy-second"),
        ])
        self.assertEqual({item["id"]: item["model"] for item in public["channels"]},
                         {"primary": "selected", "backup": "legacy-first"})

    def test_priority_always_wins_over_style_model_and_each_channel_appears_once(self):
        self.save_channels([
            self.channel("backup", priority=20, models=["style-model", "extra"]),
            self.channel("primary", priority=1, models=["selected-model", "style-model"]),
        ])
        expected = [("primary", "selected-model"), ("backup", "style-model")]
        for preferred in (None, "style-model", "extra", "", "unrelated"):
            with self.subTest(preferred=preferred):
                self.assertEqual([(item["id"], model) for item, model in
                                  self.channels.candidates(preferred)], expected)

    def test_public_routing_matches_candidates_for_all_states(self):
        self.save_channels([
            dict(self.channel("disabled", priority=1), enabled=False),
            self.channel("unconfigured", priority=2, key=""),
            self.channel("cooling", priority=3),
            self.channel("primary", priority=4),
            self.channel("standby", priority=5),
        ])
        self.channels.record_result("cooling", False)
        public = self.channels.public_status()
        self.assertEqual(public["active_channel_id"], "primary")
        self.assertEqual({item["id"]: item["routing"] for item in public["channels"]}, {
            "disabled": "disabled", "unconfigured": "unconfigured", "cooling": "cooldown",
            "primary": "primary", "standby": "standby",
        })
        self.assertEqual([item["id"] for item, _ in self.channels.candidates()],
                         ["primary", "standby"])

    def test_no_active_channel_when_none_are_configured_or_available(self):
        self.assertIsNone(self.channels.public_status()["active_channel_id"])
        self.save_channels([self.channel("primary")])
        self.channels.record_result("primary", False)
        self.assertIsNone(self.channels.public_status()["active_channel_id"])

    def test_changed_configuration_clears_health_and_cooldown(self):
        changes = ({"model": "replacement"}, {"api_key": "replacement-key"},
                   {"base_url": "https://new.example/v1"}, {"timeout": 240},
                   {"priority": 2}, {"enabled": False},
                   {"health_url": "https://primary.example/health"})
        for change in changes:
            with self.subTest(change=change):
                original = self.channel("primary")
                self.save_channels([original])
                self.channels.record_result("primary", False)
                public = self.save_channels([dict(original, **change)])["channels"][0]
                self.assertEqual(public["health"], "unknown")
                self.assertIsNone(public["retry_after"])
                self.assertNotIn("primary", self.channels._generation_cooldown)
                self.assertNotIn("primary", self.channels._health)

    def test_unchanged_save_and_blank_key_preserve_health_and_cooldown(self):
        self.save_channels([self.channel("primary")])
        self.channels.record_result("primary", False)
        public = self.save_channels([self.channel("primary", key="")])["channels"][0]
        self.assertEqual(public["routing"], "cooldown")
        self.assertEqual(public["health"], "unavailable")
        self.assertEqual(self.channels.get("primary")["api_key"], "secret-key")

    def test_inflight_probe_and_generation_cannot_restore_old_configuration_health(self):
        self.save_channels([self.channel("primary")])
        previous = self.channels.get("primary")

        def change_during_probe(_channel):
            self.save_channels([dict(self.channel("primary"), model="replacement")])
            return {"health": "unavailable", "detail": "old failure"}

        with patch.object(self.channels, "_probe", side_effect=change_during_probe):
            public = self.channels.check("primary")
        self.channels.record_result("primary", False, channel=previous)
        self.assertEqual(public["channels"][0]["health"], "unknown")
        self.assertEqual(self.channels.public_status()["channels"][0]["routing"], "primary")

    def test_generation_error_details_redact_secrets(self):
        self.save_channels([self.channel("primary")])
        self.channels.record_result("primary", False, "request with secret-key failed")
        self.assertNotIn("secret-key", json.dumps(self.channels.public_status()))

    def test_probe_with_old_url_or_key_cannot_overwrite_new_health(self):
        for change in ({"base_url": "https://replacement.example/v1"},
                       {"api_key": "replacement-key"}):
            with self.subTest(change=change):
                self.save_channels([self.channel("primary")])

                def change_during_probe(_channel):
                    self.save_channels([dict(self.channel("primary"), **change)])
                    self.channels.record_result("primary", True)
                    return {"health": "unavailable", "detail": "stale probe failure"}

                with patch.object(self.channels, "_probe", side_effect=change_during_probe):
                    public = self.channels.check("primary")
                self.assertEqual(public["channels"][0]["health"], "available")
                self.assertEqual(public["channels"][0]["detail"], "连接与生成正常")
                self.assertIsNone(public["channels"][0]["retry_after"])
                self.assertEqual(public["active_channel_id"], "primary")

    def test_unavailable_channels_are_temporarily_removed_and_recover(self):
        self.save_channels([
            self.channel("primary", priority=1),
            self.channel("backup", priority=2),
        ])
        self.channels.record_result("primary", False, "temporarily unavailable")
        self.assertEqual([item[0]["id"] for item in self.channels.candidates("model-a")], ["backup"])
        self.channels.record_result("primary", True)
        self.assertEqual([item[0]["id"] for item in self.channels.candidates("model-a")],
                         ["primary", "backup"])

    def test_private_get_returns_one_channel_and_rejects_unknown(self):
        self.save_channels([self.channel("primary")])
        self.assertEqual(self.channels.get("primary")["api_key"], "secret-key")
        with self.assertRaises(ChannelConfigError):
            self.channels.get("missing")

    def test_generation_timeout_is_public_and_preserved_when_omitted(self):
        item = self.channel("primary")
        item["timeout"] = 240
        self.save_channels([item])
        self.assertEqual(self.channels.public_status()["channels"][0]["timeout"], 240)
        self.save_channels([self.channel("primary", key="")])
        self.assertEqual(self.channels.get("primary")["timeout"], 240)

    def test_unsupported_models_endpoint_is_unknown_not_unavailable(self):
        self.save_channels([self.channel("primary")])
        error = urllib.error.HTTPError("https://primary.example/v1/models", 404, "missing", {}, None)
        with patch("aigc_channels.urllib.request.urlopen", side_effect=error):
            result = self.channels.check("primary")
        self.assertEqual(result["channels"][0]["health"], "unknown")
        self.assertEqual([item[0]["id"] for item in self.channels.candidates("model-a")], ["primary"])

    def test_probe_uses_application_user_agent(self):
        self.save_channels([self.channel("primary")])

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return b'{"object":"list","data":[]}'

        with patch("aigc_channels.urllib.request.urlopen", return_value=Response()) as transport:
            result = self.channels.check("primary")
        request = transport.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), "SpacemiT-DevCon/2026")
        self.assertEqual(result["channels"][0]["health"], "available")

    def test_successful_probe_does_not_cancel_generation_failure_cooldown(self):
        self.save_channels([self.channel("primary")])
        with patch("aigc_channels.time.time", return_value=100):
            self.channels.record_result("primary", False, "generation unavailable")
        with patch.object(self.channels, "_probe", return_value={
            "health": "available", "detail": "connection okay", "latency_ms": 4,
        }), patch("aigc_channels.time.time", return_value=120):
            self.channels.check("primary")
            self.assertEqual(self.channels.candidates("model-a"), [])
            public = self.channels.public_status()
            self.assertIsNone(public["active_channel_id"])
            self.assertEqual(public["channels"][0]["routing"], "cooldown")
            self.assertEqual(public["channels"][0]["health"], "unavailable")
            self.assertEqual(public["channels"][0]["retry_after"], 160)
        with patch("aigc_channels.time.time", return_value=161):
            self.assertEqual([item[0]["id"] for item in self.channels.candidates("model-a")],
                             ["primary"])
            self.assertEqual(self.channels.public_status()["active_channel_id"], "primary")

    def test_failed_probe_cooldown_expires_and_restores_primary(self):
        self.save_channels([self.channel("primary", priority=1),
                            self.channel("backup", priority=2)])
        with patch.object(self.channels, "_probe", return_value={
            "health": "unavailable", "detail": "connection failed", "latency_ms": 3,
        }), patch("aigc_channels.time.time", return_value=100):
            public = self.channels.check("primary")
            self.assertEqual(public["active_channel_id"], "backup")
            self.assertEqual(public["channels"][0]["retry_after"], 140)
        with patch("aigc_channels.time.time", return_value=141):
            public = self.channels.public_status()
            self.assertEqual(public["active_channel_id"], "primary")
            self.assertEqual(public["channels"][0]["health"], "unknown")
            self.assertIsNone(public["channels"][0]["retry_after"])
            self.assertEqual(self.channels.candidates()[0][0]["id"], "primary")

    def test_generation_races_channels_with_primary_model_and_uses_first_success(self):
        self.save_channels([
            self.channel("primary", priority=1, models=["model-a", "unused-model"]),
            self.channel("backup", priority=2, models=["model-a"]),
            self.channel("other-model", priority=3, models=["model-b"]),
        ])
        output = io.BytesIO()
        Image.new("RGB", (12, 16), (80, 120, 160)).save(output, format="PNG")
        response_body = json.dumps({"data": [{
            "b64_json": base64.b64encode(output.getvalue()).decode("ascii")
        }]}).encode("utf-8")

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return response_body

        def request(_payload, **kwargs):
            if kwargs["endpoint_base"].startswith("https://primary"):
                time.sleep(0.05)
            return Image.new("RGB", (12, 16), "teal")

        metadata = {}
        with patch.object(aigc_client, "_upload_image", return_value=b"jpeg"), patch(
            "aigc_client._request_edit", side_effect=request
        ) as request_mock:
            generated = aigc_client.edit_portrait(
                Image.new("RGB", (12, 16)), Image.new("L", (12, 16), 255),
                prompt="portrait", model="model-b", size="12x16", channels=self.channels,
                result_meta=metadata)

        self.assertEqual(generated.size, (12, 16))
        self.assertEqual(request_mock.call_count, 2)
        self.assertEqual({call.kwargs["model"] for call in request_mock.call_args_list}, {"model-a"})
        self.assertEqual(metadata["channel_id"], "backup")
        self.assertEqual(metadata["model"], "model-a")
        deadline = time.time() + 1
        while time.time() < deadline:
            health = {item["id"]: item for item in self.channels.public_status()["channels"]}
            if health["primary"]["health"] == "available":
                break
            time.sleep(0.01)
        health = {item["id"]: item for item in self.channels.public_status()["channels"]}
        self.assertEqual(health["primary"]["health"], "available")
        self.assertEqual(health["backup"]["health"], "available")
        self.assertEqual(health["other-model"]["health"], "unknown")

    def test_json_images_fallback_supports_custom_edit_gateways(self):
        output = io.BytesIO()
        Image.new("RGB", (12, 16), "teal").save(output, "PNG")
        response_body = json.dumps({"data": [{
            "b64_json": base64.b64encode(output.getvalue()).decode("ascii")
        }]}).encode()

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return response_body

        unsupported = urllib.error.HTTPError(
            "https://custom.example/v1/images/edits", 415, "unsupported", {}, None)
        with patch("aigc_client.urllib.request.urlopen", side_effect=[unsupported, Response()]) as transport:
            generated = aigc_client._request_edit(
                b"jpeg", prompt="portrait", model="model-a", size="12x16",
                endpoint_base="https://custom.example/v1", api_key="secret-key", timeout=30)
        self.assertEqual(generated.size, (12, 16))
        self.assertEqual(transport.call_count, 2)
        first, second = [call.args[0] for call in transport.call_args_list]
        self.assertTrue(first.get_header("Content-type").startswith("multipart/form-data"))
        self.assertEqual(second.get_header("Content-type"), "application/json")
        self.assertEqual(second.get_header("User-agent"), "SpacemiT-DevCon/2026")
        body = json.loads(second.data.decode())
        self.assertEqual(body["model"], "model-a")
        self.assertTrue(body["images"][0].startswith("data:image/jpeg;base64,"))

    def test_url_image_response_is_downloaded_with_application_user_agent(self):
        output = io.BytesIO()
        Image.new("RGB", (12, 16), "navy").save(output, "PNG")
        response_body = json.dumps({"data": [{"url": "https://images.example/result.png"}]}).encode()

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return self.payload

        with patch("aigc_client.urllib.request.urlopen", side_effect=[
            Response(response_body), Response(output.getvalue()),
        ]) as transport:
            generated = aigc_client._request_edit(
                b"jpeg", prompt="portrait", model="model-a", size="12x16",
                endpoint_base="https://custom.example/v1", api_key="secret-key", timeout=30)
        self.assertEqual(generated.size, (12, 16))
        download = transport.call_args_list[1].args[0]
        self.assertEqual(download.full_url, "https://images.example/result.png")
        self.assertEqual(download.get_header("User-agent"), "SpacemiT-DevCon/2026")

    def test_every_style_uses_primary_selected_model_even_without_style_model(self):
        self.save_channels([
            dict(self.channel("primary", priority=1), model="primary-selected"),
            self.channel("backup", priority=2, models=["backup-selected"]),
        ])
        generated = Image.new("RGB", (12, 16))
        for style_model in ("backup-selected", "unrelated", ""):
            with self.subTest(style_model=style_model), patch.object(
                aigc_client, "_upload_image", return_value=b"jpeg"
            ), patch.object(aigc_client, "_request_edit", return_value=generated) as request:
                result = aigc_client.edit_portrait(
                    generated, None, prompt="portrait", model=style_model,
                    size="12x16", channels=self.channels)
                self.assertIs(result, generated)
                self.assertEqual(request.call_count, 1)
                self.assertEqual({call.kwargs["model"] for call in request.call_args_list},
                                 {"primary-selected"})
                self.assertEqual({call.kwargs["endpoint_base"] for call in request.call_args_list},
                                 {"https://primary.example/v1"})

    def test_nonretryable_error_still_fails_over_and_all_failed_channels_tried_once(self):
        self.save_channels([
            self.channel("primary", priority=1, models=["selected", "never-tried"]),
            self.channel("backup", priority=2, models=["selected", "never-tried"]),
        ])
        def fail(_payload, **kwargs):
            if kwargs["endpoint_base"].startswith("https://primary"):
                raise aigc_client.AIGCError("invalid model input", retryable=False)
            raise aigc_client.AIGCError("backup unavailable")

        with patch.object(aigc_client, "_upload_image", return_value=b"jpeg"), patch.object(
            aigc_client, "_request_edit", side_effect=fail
        ) as request:
            with self.assertRaises(aigc_client.AIGCError):
                aigc_client.edit_portrait(
                    Image.new("RGB", (12, 16)), None, prompt="portrait", model="fallback",
                    size="12x16", channels=self.channels)
        self.assertEqual(request.call_count, 2)
        self.assertEqual({call.kwargs["model"] for call in request.call_args_list}, {"selected"})
        public = {item["id"]: item for item in self.channels.public_status()["channels"]}
        self.assertEqual(public["primary"]["health"], "unknown")
        self.assertEqual(public["backup"]["routing"], "cooldown")

    def test_exact_channel_generation_never_falls_back(self):
        self.save_channels([
            self.channel("primary", priority=1, models=["model-a"]),
            self.channel("backup", priority=2, models=["model-b"]),
        ])
        failure = urllib.error.HTTPError("https://primary.example/v1/images/edits",
                                         503, "unavailable", {}, None)
        with patch.object(aigc_client, "_upload_image", return_value=b"jpeg"), patch(
            "aigc_client.urllib.request.urlopen", side_effect=failure
        ) as open_request:
            with self.assertRaisesRegex(aigc_client.AIGCError, "HTTP 503"):
                aigc_client.edit_portrait_with_channel(
                    Image.new("RGB", (12, 16)), Image.new("L", (12, 16), 255),
                    prompt="portrait", model="model-a", size="12x16",
                    channel=self.channels.get("primary"), channels=self.channels)
        self.assertEqual(open_request.call_count, 1)
        self.assertEqual(open_request.call_args.args[0].full_url,
                         "https://primary.example/v1/images/edits")
        health = {item["id"]: item for item in self.channels.public_status()["channels"]}
        self.assertEqual(health["primary"]["health"], "unavailable")
        self.assertEqual(health["backup"]["health"], "unknown")

    def test_race_candidates_only_include_available_channels_with_primary_model(self):
        self.save_channels([
            self.channel("primary", priority=1, models=["model-a"]),
            self.channel("same-model", priority=2, models=["model-a"]),
            self.channel("other-model", priority=3, models=["model-b"]),
        ])
        model, candidates = self.channels.race_candidates()
        self.assertEqual(model, "model-a")
        self.assertEqual([item[0]["id"] for item in candidates], ["primary", "same-model"])
        self.assertEqual({item[1] for item in candidates}, {"model-a"})
        status = self.channels.public_status()
        self.assertEqual(status["race_channel_count"], 2)
        self.assertEqual([item["id"] for item in status["channels"] if item["race_eligible"]],
                         ["primary", "same-model"])
        self.channels.record_result("primary", False)
        self.channels.record_result("same-model", False)
        model, candidates = self.channels.race_candidates()
        self.assertEqual(model, "model-b")
        self.assertEqual([item[0]["id"] for item in candidates], ["other-model"])
        self.assertEqual(self.channels.public_status()["race_channel_count"], 1)

    def test_exact_channel_ignores_override_and_recovers_from_cooldown(self):
        self.save_channels([self.channel("primary", models=["model-a", "model-b"])])
        self.channels.record_result("primary", False, "previous failure")
        output = io.BytesIO()
        Image.new("RGB", (12, 16), "navy").save(output, "PNG")
        response_body = json.dumps({"data": [{
            "b64_json": base64.b64encode(output.getvalue()).decode("ascii")
        }]}).encode()

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return response_body

        with patch.object(aigc_client, "_upload_image", return_value=b"jpeg"), patch(
            "aigc_client.urllib.request.urlopen", return_value=Response()
        ) as open_request:
            result = aigc_client.edit_portrait_with_channel(
                Image.new("RGB", (12, 16)), Image.new("L", (12, 16), 255),
                prompt="portrait", model="model-b", size="12x16",
                channel=self.channels.get("primary"), channels=self.channels)
        self.assertEqual(result.size, (12, 16))
        open_request.assert_called_once()
        self.assertIn(b"model-a", open_request.call_args.args[0].data)
        self.assertNotIn(b"model-b", open_request.call_args.args[0].data)
        self.assertEqual(self.channels.public_status()["channels"][0]["health"], "available")
        self.assertTrue(self.channels.candidates("model-a"))


if __name__ == "__main__":
    unittest.main()
