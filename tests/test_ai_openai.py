import json

import httpx

from cuoti import ai


def _fake_post(captured):
    def post(url, headers=None, timeout=None, json=None):
        captured.update(url=url, headers=headers, body=json)
        out = {"question_text": "Q", "child_answer": "", "correct_answer": "A", "topic": "T", "causes": ["粗心", "乱写"]}
        return httpx.Response(200, request=httpx.Request("POST", url),
                              json={"choices": [{"message": {"content": "```json\n" + _dumps(out) + "\n```"}}]})
    return post


def _dumps(o):
    return json.dumps(o, ensure_ascii=False)


def test_openai_uses_max_completion_tokens(monkeypatch):
    captured = {}
    monkeypatch.setattr(httpx, "post", _fake_post(captured))
    monkeypatch.setattr(ai, "config", lambda: ai.ModelConfig("openai_compatible", "some-vision-model",
                                                            "https://api.openai.com/v1", "sk-test"))
    out = ai.read_question([b"\xff\xd8jpeg"], "math", 4, [], ["粗心"])
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert "max_completion_tokens" in captured["body"] and "max_tokens" not in captured["body"]
    assert captured["body"]["messages"][0]["content"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert out["question_text"] == "Q" and out["causes"] == ["粗心"]  # unknown cause dropped


def test_local_server_keeps_max_tokens(monkeypatch):
    captured = {}
    monkeypatch.setattr(httpx, "post", _fake_post(captured))
    monkeypatch.setattr(ai, "config", lambda: ai.ModelConfig("openai_compatible", "qwen-vl", "http://vlm.local/v1", ""))
    ai.read_question([b"x"], "math", 4, [], [])
    assert captured["body"]["max_tokens"] == 4000 and captured["headers"] == {}


def test_key_from_settings_page(client, family, monkeypatch):
    from conftest import login
    login(client, family["parent"], "123456")
    r = client.post("/settings/model", data={"provider": "openai_compatible", "model": "gpt-4o",
                                             "base_url": "https://api.openai.com/v1"}, follow_redirects=True)
    assert "还没有填 API Key" in r.text and not ai.config().ready
    r = client.post("/settings/model", data={"provider": "openai_compatible", "model": "gpt-4o",
                                             "base_url": "https://api.openai.com/v1",
                                             "api_key": " sk-proj-abc\n123WXYZ "}, follow_redirects=True)
    cfg = ai.config()
    assert cfg.ready and cfg.api_key == "sk-proj-abc123WXYZ" and cfg.key_source == "settings"
    assert "…WXYZ" in r.text and "sk-proj-abc123WXYZ" not in r.text  # never echoed back
    # saving again with the field empty keeps the key
    client.post("/settings/model", data={"provider": "openai_compatible", "model": "gpt-4o",
                                         "base_url": "https://api.openai.com/v1"})
    assert ai.config().api_key == "sk-proj-abc123WXYZ"
    captured = {}
    monkeypatch.setattr(httpx, "post", _fake_post(captured))
    r = client.post("/settings/model/test", follow_redirects=True)
    assert "连接正常" in r.text and captured["headers"]["Authorization"] == "Bearer sk-proj-abc123WXYZ"
    client.post("/settings/model", data={"provider": "openai_compatible", "model": "gpt-4o",
                                         "base_url": "https://api.openai.com/v1", "clear_key": "1"})
    assert not ai.config().ready


def test_tutor_cannot_set_key(client, family):
    from conftest import login
    login(client, family["math"], "333333")
    assert client.post("/settings/model", data={"provider": "mock", "api_key": "x"}).status_code == 403
