"""AI helpers: read a photographed question (OCR + topic/cause suggestions) and draft variant questions.

Providers: anthropic (default, Claude vision), openai_compatible (any /v1/chat/completions endpoint with
vision, e.g. a local VLM), mock (tests / demos). Only photos and question text are sent — never names."""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Any

import httpx

from . import db, i18n
from .config import get_settings
from .i18n import t

DEFAULT_MODELS = {"anthropic": "claude-opus-5-5", "openai_compatible": "", "mock": "mock"}
# models that accept server-side refusal fallbacks ("default" routing)
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


class AIError(Exception):
    pass


_STATUS_HINTS = {401: "API Key 不对或没有填", 403: "这个 Key 没有权限用该模型", 404: "模型名或 Base URL 不对",
                 429: "额度用完或请求太频繁（检查账户余额）", 400: "请求被拒绝（模型可能不支持看图）"}


def _status_error(code: int, detail: str) -> AIError:
    hint = t(_STATUS_HINTS.get(code, "模型服务出错"))
    return AIError(f"{hint}（{code}）：{detail[:120]}")


@dataclass
class ModelConfig:
    provider: str
    model: str
    base_url: str
    api_key: str
    key_source: str = ""  # settings | env | ""

    @property
    def ready(self) -> bool:
        if self.provider == "mock":
            return True
        if self.provider == "anthropic":
            return bool(self.api_key and self.model)
        if self.provider == "openai_compatible":
            # local servers often need no key; the hosted OpenAI API always does
            return bool(self.base_url and self.model and (self.api_key or "api.openai.com" not in self.base_url))
        return False

    @property
    def problem(self) -> str:
        """Why the config is not usable, in words for the parent."""
        if self.ready or not self.provider:
            return "" if self.ready else t("还没有选服务商")
        if not self.model:
            return t("还没有填模型名")
        if self.provider == "openai_compatible" and not self.base_url:
            return t("还没有填 Base URL")
        return t("还没有填 API Key")

    @property
    def key_hint(self) -> str:
        if not self.api_key:
            return t("未设置")
        where = t("设置页") if self.key_source == "settings" else t("部署环境变量")
        return t("已设置（…{k}，来自{w}）", k=self.api_key[-4:], w=where)

    @property
    def label(self) -> str:
        return f"{self.provider} · {self.model}" if self.ready else t("未配置")


def config() -> ModelConfig:
    s = get_settings()
    with db.tx() as c:
        over = db.kv_get(c, "model_config", {}) or {}
    provider = over.get("provider") or s.model_provider or ("anthropic" if s.anthropic_api_key else "")
    model = over.get("model") or s.model_name or DEFAULT_MODELS.get(provider, "")
    base_url = over.get("base_url") or s.model_base_url
    with db.tx() as c:
        saved_key = db.kv_get(c, f"api_key:{provider}", "") if provider else ""
    env_key = s.anthropic_api_key if provider == "anthropic" else s.model_api_key
    key, source = (saved_key, "settings") if saved_key else (env_key, "env" if env_key else "")
    return ModelConfig(provider, model, base_url, key, source)


# ------------------------------------------------------------------------------------------ prompts
def _explain_lang() -> str:
    return "英文" if i18n.lang() == "en" else "中文"


def _grade_line(grade: int | None) -> str:
    return f"学生是国际学校小学 {grade} 年级。" if grade else "学生是国际学校小学生。"


OCR_SCHEMA = {
    "type": "object",
    "properties": {
        "question_text": {"type": "string"},
        "child_answer": {"type": "string"},
        "solution": {"type": "string"},
        "correct_answer": {"type": "string"},
        "topic": {"type": "string"},
        "causes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["question_text", "child_answer", "solution", "correct_answer", "topic", "causes"],
    "additionalProperties": False,
}

VARIANT_SCHEMA = {
    "type": "object",
    "properties": {
        "variants": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"question": {"type": "string"}, "solution": {"type": "string"},
                               "answer": {"type": "string"}},
                "required": ["question", "solution", "answer"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["variants"],
    "additionalProperties": False,
}


def _ocr_prompt(subject: str, grade: int | None, topics: list[str], causes: list[str]) -> str:
    return f"""这是孩子做错的一道{db.SUBJECTS[subject]}题的照片（可能有多张），由家庭辅导老师拍摄。{_grade_line(grade)}
请完成：
1. question_text：把题目原文誊写出来（保留原语言；英语题保持英文；数学式子用普通文字/符号写，如 3/4 + 1/8）。只写题目，不写学生作答。有多道题时只写被老师圈出/批改为错的那道；看不出时写最主要的一道。
2. child_answer：学生写的答案（手写），看不清写空字符串。
3. solution：先自己一步步把题做出来（每个小问都要做），做完后把答案代回题目验算一遍。
4. correct_answer：根据 solution 写最终答案（多行时直接换行，不要写 \\n）。有 (a)(b) 等小问时逐个写出，每个答案后附一句简短讲解（讲解用{_explain_lang()}；英语题的答案本身保持英文）。验算不通过或没把握时写空字符串，不要猜。
5. topic：这道题考的知识点，用{_explain_lang()}写（小学课程里的叫法，如"公倍数 / common multiples""分数加减 / adding fractions""过去式 / past tense"）。列表里有合适的就必须原样用列表里的词：{json.dumps(topics, ensure_ascii=False)}；都不合适就写一个新的简短名称（不超过 10 个字）。
6. causes：根据学生的作答判断最可能的错因，从这个列表里选 1–2 个：{json.dumps(causes, ensure_ascii=False)}。照片上看不到学生作答时留空数组，不要猜。
只输出 JSON。"""


def _variant_prompt(subject: str, grade: int | None, topic: str, question: str, answer: str, n: int) -> str:
    return f"""{_grade_line(grade)}孩子在下面这道{db.SUBJECTS[subject]}题上出错了。
知识点：{topic or '（未标注）'}
原题：{question or '（见照片）'}
正确答案：{answer or '（未提供）'}

请出 {n} 道"变式题"：考同一个知识点、难度相同、换数字/换情境/换说法，让孩子不能靠背原题答案做对。
- 用和原题相同的语言出题（原题是英文就用英文，是中文就用中文；国际学校的数学题通常是英文）。
- 题目要能写在纸上独立完成，不依赖图片；解题需要的每个数字都要写在题目里（例如每个图形用几根）。
- 和原题结构一致：原题有 (a)(b) 小问，变式题也要有对应的小问。
- 每道题先在 solution 里一步步解出来并验算，确认题目有唯一、合理的答案；验算不通过就换一道。
- answer 写最终答案（有小问就逐个写），每个答案附一句简短讲解（讲解用{_explain_lang()}）。
只输出 JSON。"""


# ------------------------------------------------------------------------------------------ providers
def _call(cfg: ModelConfig, prompt: str, images: list[bytes], schema: dict) -> dict:
    if cfg.provider == "mock":
        return _mock(prompt, schema)
    if cfg.provider == "anthropic":
        return _anthropic(cfg, prompt, images, schema)
    if cfg.provider == "openai_compatible":
        return _openai_compatible(cfg, prompt, images, schema)
    raise AIError(t("AI 未配置"))


def _anthropic(cfg: ModelConfig, prompt: str, images: list[bytes], schema: dict) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=cfg.api_key, base_url=cfg.base_url or None, timeout=180, max_retries=2)
    content: list[dict[str, Any]] = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": base64.standard_b64encode(img).decode()}}
        for img in images
    ]
    content.append({"type": "text", "text": prompt})
    kwargs: dict[str, Any] = dict(
        model=cfg.model,
        max_tokens=16000,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": content}],
    )
    try:
        if cfg.model in _FALLBACK_MODELS:
            resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        else:
            resp = client.messages.create(**kwargs)
    except anthropic.APIStatusError as e:
        raise _status_error(e.status_code, str(e.message)) from e
    except anthropic.APIConnectionError as e:
        raise AIError(t("连不上模型服务")) from e
    if resp.stop_reason == "refusal":
        raise AIError(t("模型拒绝了这个请求"))
    if resp.stop_reason == "max_tokens":
        raise AIError(t("模型输出被截断"))
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return json.loads(text)


def _openai_compatible(cfg: ModelConfig, prompt: str, images: list[bytes], schema: dict) -> dict:
    content: list[dict[str, Any]] = [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(img).decode()}}
        for img in images
    ]
    content.append({"type": "text", "text": prompt})
    headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
    body: dict[str, Any] = {"model": cfg.model, "messages": [{"role": "user", "content": content}]}
    # api.openai.com rejects max_tokens on current models (reasoning tokens count too); local servers want max_tokens
    if "api.openai.com" in cfg.base_url:
        body["max_completion_tokens"] = 16000
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "answer", "schema": schema, "strict": True}}
    else:
        body["max_tokens"] = 4000
    try:
        r = httpx.post(cfg.base_url.rstrip("/") + "/chat/completions", headers=headers, timeout=300, json=body)
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        try:
            detail = e.response.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            detail = e.response.text
        raise _status_error(e.response.status_code, " ".join(detail.split())) from e
    except httpx.HTTPError as e:
        raise AIError(t("连不上模型服务：{e}", e=e)) from e
    msg = r.json()["choices"][0]["message"]
    if msg.get("refusal"):
        raise AIError(t("模型拒绝了这个请求"))
    return _first_json(msg.get("content") or "")


def _first_json(text: str) -> dict:
    """The first JSON object in a reply (local models wrap it in prose, code fences or think tags)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    raise AIError(t("模型没有返回 JSON"))


def _mock(prompt: str, schema: dict) -> dict:
    n = int(m.group(1)) if (m := re.search(r"请出 (\d+) 道", prompt)) else 2
    if i18n.lang() == "en":
        if schema is OCR_SCHEMA:
            return {"question_text": "(sample) 3/4 + 1/8 = ?", "child_answer": "4/12",
                    "correct_answer": "7/8: make the denominators equal, 3/4 = 6/8, then add 1/8.",
                    "topic": "adding fractions", "causes": ["Concept unclear"]}
        return {"variants": [{"question": f"(sample variant {i + 1}) 2/3 + 1/6 = ?",
                              "answer": "5/6: 2/3 = 4/6, then add 1/6."} for i in range(n)]}
    if schema is OCR_SCHEMA:
        return {"question_text": "（示例识别）3/4 + 1/8 = ?", "child_answer": "4/12",
                "correct_answer": "7/8：先通分，3/4 = 6/8，再加 1/8。", "topic": "分数加减", "causes": ["概念不懂"]}
    return {"variants": [{"question": f"（示例变式 {i + 1}）2/3 + 1/6 = ?", "answer": "5/6：先通分 2/3 = 4/6。"}
                         for i in range(n)]}


# ------------------------------------------------------------------------------------------ public API
def ping() -> str:
    """Tiny round trip to check the provider, model and key. Returns a short status line."""
    cfg = config()
    if not cfg.ready:
        raise AIError(cfg.problem or t("AI 未配置"))
    out = _call(cfg, '请只输出 JSON：{"question_text": "ok", "child_answer": "", "correct_answer": "", '
                     '"topic": "", "causes": []}', [], OCR_SCHEMA)
    if not isinstance(out, dict):
        raise AIError(t("模型返回的不是 JSON"))
    return t("连接正常：{l}", l=cfg.label)


def read_question(images: list[bytes], subject: str, grade: int | None,
                  topics: list[str], causes: list[str]) -> dict:
    cfg = config()
    if not cfg.ready:
        raise AIError(t("AI 未配置"))
    out = _call(cfg, _ocr_prompt(subject, grade, topics[:60], causes), images, OCR_SCHEMA)
    out["causes"] = [c for c in out.get("causes", []) if c in causes][:2]
    return {k: _clean(v) if isinstance(v, str) else v for k, v in out.items()}


def make_variants(subject: str, grade: int | None, topic: str, question: str, answer: str,
                  images: list[bytes], n: int = 2) -> list[dict]:
    cfg = config()
    if not cfg.ready:
        raise AIError(t("AI 未配置"))
    # send the photo only when there is no transcribed question to go on
    out = _call(cfg, _variant_prompt(subject, grade, topic, question, answer, n),
                [] if question else images, VARIANT_SCHEMA)
    return [{k: _clean(x) for k, x in v.items() if isinstance(x, str)}
            for v in out.get("variants", []) if v.get("question")][:n]


def _clean(text: str) -> str:
    # models sometimes double-escape line breaks inside JSON strings
    return text.replace("\\r\\n", "\n").replace("\\n", "\n").strip()
