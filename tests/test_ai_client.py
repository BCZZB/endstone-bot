import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("ai_client_under_test", Path("endstone_bot/ai_client.py"))
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
AIClient = module.AIClient
parse_ai_response = module.parse_ai_response


class FakeResponse:
    def __init__(self, data):
        self.data = json.dumps(data).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return self.data


class AIClientTests(unittest.TestCase):
    def test_structured_actions_are_filtered(self):
        result = parse_ai_response(json.dumps({
            "reply": "好的",
            "actions": [
                {"name": "follow", "args": {"target": "Alex"}},
                {"name": "server_command", "args": {"cmd": "op x"}},
                {"name": "say", "args": {"message": "完成"}},
            ],
        }))
        self.assertTrue(result.ok)
        self.assertEqual([x["name"] for x in result.actions], ["follow", "say"])

    def test_plain_text_is_reply_only(self):
        result = parse_ai_response("你好")
        self.assertEqual(result.reply, "你好")
        self.assertEqual(result.actions, [])

    @patch("urllib.request.urlopen")
    def test_openai_compatible_response(self, urlopen):
        urlopen.return_value = FakeResponse({
            "choices": [{"message": {"content": '{"reply":"OK","actions":[]}'}}]
        })
        client = AIClient("https://example.test/v1", "sk-test", "model")
        result = client.chat([{"role": "user", "content": "test"}])
        self.assertTrue(result.ok)
        self.assertEqual(result.reply, "OK")

    def test_invalid_config(self):
        result = AIClient().chat([])
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "AI 未配置")


if __name__ == "__main__":
    unittest.main()
