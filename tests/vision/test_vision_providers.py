"""Провайдеры: Anthropic через respx, Sidecar, Null, выбор по окружению."""
from __future__ import annotations

import base64
import json
import logging

import httpx
import pytest
import respx
from vision_helpers import SIDECAR_DIR, make_image

from rabota.vision import providers
from rabota.vision.images import open_image
from rabota.vision.providers import (
    DEFAULT_MODEL,
    AnthropicVisionProvider,
    NullProvider,
    ProviderError,
    SidecarProvider,
    normalize_output,
    parse_model_json,
    prepare_image_for_api,
    provider_from_env,
)

SECRET = "sk-test-secret-0123456789"
API = providers.ANTHROPIC_URL

MODEL_ANSWER = {
    "kind": "vacancy",
    "vacancies": [
        {
            "profession": "Бариста",
            "pay_min": "4 000",
            "pay_max": 4000,
            "pay_period": "Смена",
            "city": "Донецк",
            "district": None,
            "schedule": "2/2",
            "requirements": None,
            "employer": "Кофейня",
            "phones": ["+7 949 000-00-01"],
            "telegram": "@user",
            "extra_text": None,
        }
    ],
    "raw_text": "Бариста 4000 смена",
}


def api_response(text: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"content": [{"type": "text", "text": text}], "role": "assistant"})


@pytest.fixture()
def provider() -> AnthropicVisionProvider:
    return AnthropicVisionProvider(api_key=SECRET, model="test-vision-model")


@respx.mock
def test_anthropic_request_shape_and_parsing(provider):
    route = respx.post(API).mock(return_value=api_response(json.dumps(MODEL_ANSWER, ensure_ascii=False)))
    image = make_image(1)
    result = provider.extract(image, hint_text="Ищем бариста")

    req = route.calls.last.request
    assert req.headers["x-api-key"] == SECRET
    assert req.headers["anthropic-version"] == providers.ANTHROPIC_VERSION
    body = json.loads(req.content)
    assert body["model"] == "test-vision-model"
    assert body["max_tokens"] > 0
    assert "вакансии" in body["system"] and "service" in body["system"] and "military" in body["system"]
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "image"
    src = content[0]["source"]
    assert src["type"] == "base64" and src["media_type"] == "image/png"
    assert base64.b64decode(src["data"]) == image
    texts = [c["text"] for c in content if c["type"] == "text"]
    assert any("Ищем бариста" in t for t in texts)

    assert result["kind"] == "vacancy"
    vac = result["vacancies"][0]
    assert vac["pay_min"] == 4000 and vac["pay_max"] == 4000
    assert vac["pay_period"] == "смена"
    assert vac["phones"] == ["+7 949 000-00-01"]
    assert result["raw_text"] == "Бариста 4000 смена"


@respx.mock
def test_anthropic_parses_fenced_json_with_preamble(provider):
    text = "Вот результат:\n```json\n" + json.dumps(MODEL_ANSWER, ensure_ascii=False) + "\n```"
    respx.post(API).mock(return_value=api_response(text))
    assert provider.extract(make_image(2))["vacancies"][0]["profession"] == "Бариста"


@respx.mock
def test_anthropic_no_hint_means_no_context_block(provider):
    route = respx.post(API).mock(return_value=api_response('{"kind": "other"}'))
    provider.extract(make_image(2))
    texts = [c["text"] for c in json.loads(route.calls.last.request.content)["messages"][0]["content"] if c["type"] == "text"]
    assert not any("Текст поста" in t for t in texts)


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error": {"message": "invalid x-api-key"}}),
        httpx.Response(529, text="overloaded"),
        httpx.Response(200, text="not json"),
        api_response("совсем не json"),
        api_response("[1, 2, 3]"),
    ],
)
def test_anthropic_errors_become_provider_error(provider, response):
    respx.post(API).mock(return_value=response)
    with pytest.raises(ProviderError) as exc:
        provider.extract(make_image(3))
    assert SECRET not in str(exc.value)


@respx.mock
def test_anthropic_network_error_hides_key(provider):
    respx.post(API).mock(side_effect=httpx.ConnectError("boom x-api-key=" + SECRET))
    with pytest.raises(ProviderError) as exc:
        provider.extract(make_image(3))
    assert SECRET not in str(exc.value)
    assert SECRET not in repr(exc.value.__cause__.__class__)


@respx.mock
def test_api_key_never_reaches_logs(provider, caplog):
    caplog.set_level(logging.DEBUG)
    respx.post(API).mock(return_value=api_response(json.dumps(MODEL_ANSWER, ensure_ascii=False)))
    provider.extract(make_image(4), "текст")
    respx.post(API).mock(return_value=httpx.Response(401, json={"error": "x"}))
    with pytest.raises(ProviderError):
        provider.extract(make_image(4))
    assert SECRET not in caplog.text
    assert SECRET not in repr(provider) and SECRET not in str(provider.__dict__.get("model"))


def test_anthropic_requires_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(ProviderError):
        AnthropicVisionProvider()
    with pytest.raises(ProviderError):
        AnthropicVisionProvider(api_key="")


def test_anthropic_model_from_env_and_default(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", SECRET)
    monkeypatch.delenv("LLM_MODEL_VISION", raising=False)
    assert AnthropicVisionProvider().model == DEFAULT_MODEL
    monkeypatch.setenv("LLM_MODEL_VISION", "custom-vision")
    assert AnthropicVisionProvider().model == "custom-vision"


def test_prepare_image_downscales_large_and_converts_unknown():
    big = make_image(5, size=3000)
    data, media = prepare_image_for_api(big)
    assert media == "image/jpeg"
    assert max(open_image(data).size) <= providers.MAX_API_SIDE
    small = make_image(5, size=300, fmt="PNG")
    assert prepare_image_for_api(small) == (small, "image/png")


def test_normalize_output_repairs_types_and_drops_junk():
    out = normalize_output(
        {
            "kind": "vacancy",
            "vacancies": [
                {"profession": "  ", "pay_min": 1},
                {"profession": "Грузчик", "pay_min": "45 500", "pay_max": None, "pay_period": "год", "phones": "335"},
                "мусор",
            ],
        }
    )
    assert len(out["vacancies"]) == 1
    vac = out["vacancies"][0]
    assert vac["pay_min"] == 45500 and vac["pay_period"] is None  # период без валидного значения отбрасывается
    assert vac["phones"] == ["335"]
    assert set(vac) == set(providers.VACANCY_FIELDS)


def test_normalize_output_non_vacancy_has_no_vacancies_and_unknown_kind_is_other():
    assert normalize_output({"kind": "service", "vacancies": [{"profession": "Мастер"}]})["vacancies"] == []
    assert normalize_output({"kind": "wat"})["kind"] == "other"
    assert normalize_output("строка")["kind"] == "other"
    # kind=vacancy без единой годной вакансии не остаётся vacancy
    assert normalize_output({"kind": "vacancy", "vacancies": []})["kind"] == "other"


def test_parse_model_json():
    assert parse_model_json('  {"a": 1} ') == {"a": 1}
    with pytest.raises(ProviderError):
        parse_model_json("нет")


# Sidecar


def test_sidecar_reads_fixture_by_hash(poster_bytes):
    out = SidecarProvider([SIDECAR_DIR]).extract(poster_bytes)
    assert out["kind"] == "vacancy"
    assert [v["profession"] for v in out["vacancies"]] == [
        "Продавец, кассир", "Приёмщик товаров", "Грузчик", "Охранник", "Уборщик",
    ]


def test_sidecar_finds_resized_copy(poster_bytes):
    from io import BytesIO

    from PIL import Image

    img = Image.open(BytesIO(poster_bytes)).resize((600, 600))
    buf = BytesIO()
    img.save(buf, "JPEG", quality=70)
    assert SidecarProvider([SIDECAR_DIR]).extract(buf.getvalue())["kind"] == "vacancy"


def test_sidecar_service_fixture(wall_bytes):
    out = SidecarProvider([SIDECAR_DIR]).extract(wall_bytes)
    assert out["kind"] == "service" and out["vacancies"] == []
    assert "+7 949 000-00-00" in out["raw_text"]


def test_sidecar_missing_and_broken(tmp_path):
    prov = SidecarProvider([tmp_path])
    with pytest.raises(ProviderError):
        prov.extract(make_image(9))
    with pytest.raises(ProviderError):
        prov.extract(b"junk")


def test_sidecar_next_to_image(tmp_path):
    img = tmp_path / "pic.jpg"
    img.write_bytes(make_image(10))
    (tmp_path / "pic.json").write_text(json.dumps({"kind": "military"}), encoding="utf-8")
    assert SidecarProvider([]).extract_path(img)["kind"] == "military"
    (tmp_path / "pic.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(ProviderError):
        SidecarProvider([]).extract_path(img)


# Null и выбор по окружению


def test_null_provider():
    out = NullProvider().extract(make_image(1))
    assert out == {"kind": "other", "vacancies": [], "raw_text": ""}


def test_provider_from_env(monkeypatch):
    for name in ("LLM_PROVIDER", "LLM_API_KEY", "LLM_MODEL_VISION"):
        monkeypatch.delenv(name, raising=False)
    assert isinstance(provider_from_env(), NullProvider)  # без ключа не создаётся
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    assert isinstance(provider_from_env(), NullProvider)
    monkeypatch.setenv("LLM_API_KEY", SECRET)
    assert isinstance(provider_from_env(), AnthropicVisionProvider)
    monkeypatch.setenv("LLM_PROVIDER", "sidecar")
    assert isinstance(provider_from_env(), SidecarProvider)
    monkeypatch.setenv("LLM_PROVIDER", "unknown")
    assert isinstance(provider_from_env(), NullProvider)
