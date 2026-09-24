import json

import httpx
import pytest

from app.llm.assessor import assess_case
from app.llm.chat import answer_question
from app.llm.providers import AnthropicProvider, LLMError, OpenAICompatibleProvider, ProviderConfig, extract_json
from tests.conftest import ScriptedLLM, good_judge, good_narrative


def _gen(replies, style="openai"):
    scripted = ScriptedLLM(replies, style)
    cfg = ProviderConfig(provider=style if style == "openai" else "anthropic", model="mock-model", api_key="k")
    provider = OpenAICompatibleProvider(cfg, scripted.transport()) if style == "openai" else AnthropicProvider(cfg, scripted.transport())
    return provider, scripted


def test_extract_json_tolerates_fences_and_preamble():
    assert extract_json('Sure! ```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Here you go: {"a": {"b": "}"}} trailing') == {"a": {"b": "}"}}
    with pytest.raises(LLMError):
        extract_json("no json here")


def test_openai_adapter_sends_expected_request():
    provider, scripted = _gen(['{"ok": true}'])
    out = provider.complete("sys", [{"role": "user", "content": "hi"}], json_output=True)
    assert out.text == '{"ok": true}'
    body = scripted.calls[0]["body"]
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["response_format"] == {"type": "json_object"}
    assert scripted.calls[0]["headers"]["authorization"] == "Bearer k"


def test_anthropic_adapter_parses_content_blocks():
    provider, scripted = _gen(['{"ok": 1}'], style="anthropic")
    out = provider.complete("sys", [{"role": "user", "content": "hi"}], json_output=True)
    assert out.text == '{"ok": 1}'
    body = scripted.calls[0]["body"]
    assert body["system"][0]["text"].startswith("sys") and body["messages"][0]["role"] == "user"
    # prompt caching: the system prompt and the evidence-pack message are marked cacheable
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert scripted.calls[0]["headers"]["x-api-key"] == "k"
    assert out.usage["input"] == 10 and out.usage["output"] == 5 and out.usage["calls"] == 1


def test_good_narrative_passes_first_round(queue):
    case = queue.cases["C1024"]
    provider, scripted = _gen([json.dumps(good_narrative(case)), good_judge()])
    judge, _ = _gen([good_judge()])
    r = assess_case(case, provider, judge, max_revisions=1)
    assert r["source"] == "llm" and r["quality"]["verdict"] == "pass" and r["revisions"] == 0
    assert r["generator"] == "openai:mock-model"


def test_hallucinated_figures_trigger_revision_then_pass(queue):
    case = queue.cases["C1024"]
    bad = good_narrative(case)
    bad["summary"] += " The provider billed 999 visits per week [E3]."
    provider, scripted = _gen([json.dumps(bad), json.dumps(good_narrative(case))])
    judge, _ = _gen([good_judge()])
    r = assess_case(case, provider, judge, max_revisions=1)
    assert r["source"] == "llm" and r["quality"]["verdict"] == "pass" and r["revisions"] == 1
    msgs = scripted.calls[1]["body"]["messages"]
    assert "999" in msgs[-1]["content"], "the revision prompt should carry the feedback"
    # the revision continues the conversation, so the evidence pack is an identical, cacheable prefix
    assert msgs[1] == scripted.calls[0]["body"]["messages"][1] and msgs[2]["role"] == "assistant"
    assert r["usage"]["writer"]["calls"] == 2 and r["usage"]["calls"] >= 2 and r["usage"]["input"] > 0
    assert r["rounds"][0]["verdict"] == "fail" and r["rounds"][1]["verdict"] == "pass"


def test_persistent_failure_falls_back_to_template(queue):
    case = queue.cases["C1019"]
    bad = good_narrative(case)
    bad["summary"] += " The provider is clearly a fraudster."
    provider, _ = _gen([json.dumps(bad)])
    judge, _ = _gen([good_judge()])
    r = assess_case(case, provider, judge, max_revisions=1)
    assert r["source"] == "template_fallback"
    assert r["rejected"]["narrative"]["summary"].endswith("fraudster.")
    assert any(f["level"] == "error" for f in r["flags"])
    assert r["quality"]["verdict"] == "pass"  # the template shown to the user is clean


def test_judge_revise_verdict_is_shown_with_flag_and_low_confidence(queue):
    case = queue.cases["C1024"]
    provider, _ = _gen([json.dumps(good_narrative(case))])
    judge, _ = _gen([good_judge("revise", scores=3, issues=[{"criterion": "completeness", "severity": "medium", "problem": "thin", "fix": "add", "evidence_ids": ["E1"]}])])
    r = assess_case(case, provider, judge, max_revisions=0)
    assert r["source"] == "llm" and r["quality"]["verdict"] == "revise"
    assert r["confidence"]["level"] == "Low" and any(f["level"] == "warn" for f in r["flags"])


def test_invalid_json_then_recovery(queue):
    case = queue.cases["C1002"]
    provider, scripted = _gen(["not json at all", json.dumps(good_narrative(case))])
    r = assess_case(case, provider, None, max_revisions=1)
    assert r["source"] == "llm" and r["rounds"][0].get("error")


def test_provider_error_falls_back_to_template(queue):
    case = queue.cases["C1002"]
    cfg = ProviderConfig(provider="openai", model="m", api_key="k")
    provider = OpenAICompatibleProvider(cfg, httpx.MockTransport(lambda req: httpx.Response(401, json={"error": "bad key"})))
    r = assess_case(case, provider, None, max_revisions=1)
    assert r["source"] == "template_unavailable" and "401" in r["rounds"][0]["error"]


def test_chat_retries_once_on_unsupported_figures(queue):
    case = queue.cases["C1024"]
    assessment = assess_case(case, None, None)
    provider, scripted = _gen(["The provider billed 4242 visits [E3].", "Duplicate billing was detected [E1]."])
    r = answer_question(case, assessment, "why?", [], ["note one"], provider)
    assert r["source"] == "llm" and r["checks"]["verdict"] == "pass" and r["citations"] == ["E1"]
    assert len(scripted.calls) == 2
    assert "note one" in scripted.calls[0]["body"]["messages"][1]["content"]


def test_chat_falls_back_offline_when_model_is_down(queue):
    case = queue.cases["C1024"]
    assessment = assess_case(case, None, None)
    cfg = ProviderConfig(provider="openai", model="m", api_key="k")
    provider = OpenAICompatibleProvider(cfg, httpx.MockTransport(lambda req: httpx.Response(503, text="down")))
    r = answer_question(case, assessment, "why is this suspicious?", [], [], provider)
    assert r["source"] == "offline_fallback" and r["citations"]


def test_openai_adapter_learns_gpt6_parameter_rules():
    """GPT-5/6 models reject max_tokens and non-default temperature; the adapter adapts once."""
    import json

    import httpx

    from app.llm.providers import OpenAICompatibleProvider, ProviderConfig

    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if "max_tokens" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead.", "param": "max_tokens"}})
        if "temperature" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported value: 'temperature' does not support 0.2 with this model. Only the default (1) value is supported.", "param": "temperature"}})
        return httpx.Response(200, json={"model": body["model"], "choices": [{"message": {"content": "{\"ok\": true}"}, "finish_reason": "stop"}], "usage": {}})

    # A model served through a custom base URL is not assumed to be a reasoning model: it learns from the errors.
    p = OpenAICompatibleProvider(ProviderConfig(provider="openai", model="gpt-6-luna", api_key="k", base_url="http://proxy/v1", max_tokens=1000), httpx.MockTransport(handler))
    assert p.complete("sys", [{"role": "user", "content": "hi"}], json_output=True).text == '{"ok": true}'
    assert [("max_tokens" in b, "temperature" in b) for b in seen] == [(True, True), (False, True), (False, False)]
    assert seen[-1]["max_completion_tokens"] == 3000
    seen.clear()
    p.complete("sys", [{"role": "user", "content": "again"}])
    assert len(seen) == 1 and "temperature" not in seen[0] and "max_completion_tokens" in seen[0]
    # Against api.openai.com the model name alone selects the right shape on the first call.
    seen.clear()
    p2 = OpenAICompatibleProvider(ProviderConfig(provider="openai", model="gpt-6-sol", api_key="k", max_tokens=1400), httpx.MockTransport(handler))
    p2.complete("sys", [{"role": "user", "content": "hi"}])
    assert len(seen) == 1 and seen[0]["max_completion_tokens"] == 4200 and "temperature" not in seen[0]
    # An empty answer that hit the token budget is a clear error, not an empty narrative.
    def exhausted(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})
    p3 = OpenAICompatibleProvider(ProviderConfig(provider="openai", model="gpt-6-sol", api_key="k"), httpx.MockTransport(exhausted))
    import pytest
    from app.llm.providers import LLMError
    with pytest.raises(LLMError, match="completion tokens"):
        p3.complete("sys", [{"role": "user", "content": "hi"}])


def test_perplexity_agent_provider_request_and_parsing():
    """One Perplexity key reaches other makers' models; no tools are sent, so the model never searches."""
    import json as _json

    import httpx

    from app.llm import catalog
    from app.llm.providers import PerplexityAgentProvider, ProviderConfig

    seen = []

    def handler(request):
        body = _json.loads(request.content)
        seen.append((str(request.url), dict(request.headers), body))
        if len(seen) == 1:  # the first call rejects temperature, the adapter drops it and retries
            return httpx.Response(400, json={"error": {"message": "temperature is not supported for this model"}})
        return httpx.Response(200, json={"model": body["model"], "output": [
            {"type": "message", "content": [{"type": "output_text", "text": '{"ok": true}'}]}], "usage": {"input_tokens": 5}})

    cfg = ProviderConfig(provider="perplexity", model="google/gemini-3.1-pro-preview", api_key="pplx-test")
    comp = PerplexityAgentProvider(cfg, httpx.MockTransport(handler)).complete("sys", [{"role": "user", "content": "hi"}], json_output=True)
    assert comp.text == '{"ok": true}' and comp.model == "google/gemini-3.1-pro-preview"
    assert "temperature" in seen[0][2]
    url, headers, body = seen[-1]
    assert url == "https://api.perplexity.ai/v1/agent" and headers["authorization"] == "Bearer pplx-test"
    assert "tools" not in body and "temperature" not in body and body["input"] == [{"role": "user", "content": "hi"}]
    assert "JSON" in body["instructions"]
    assert catalog.vendor_of("perplexity", "anthropic/claude-sonnet-5") == "anthropic"
    assert catalog.vendor_of("perplexity", "google/gemini-3.8-flash") == "gemini"
    assert catalog.find("pplx-gpt-6-luna")["vendor"] == "openai" and catalog.find("gpt-6-sol")["vendor"] == "openai"


def test_usage_normalizes_every_provider_shape():
    from app.llm import usage

    oa = usage.normalize({"prompt_tokens": 1000, "completion_tokens": 200, "prompt_tokens_details": {"cached_tokens": 400},
                          "completion_tokens_details": {"reasoning_tokens": 50}}, "gpt-6-luna")
    assert (oa["input"], oa["cached"], oa["output"], oa["reasoning"]) == (1000, 400, 200, 50)
    assert oa["cost_source"] == "estimate" and abs(oa["cost_usd"] - (600 * 0.10 + 400 * 0.025 + 200 * 0.50) / 1e6) < 1e-12
    an = usage.normalize({"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 900}, "claude-haiku-4-5")
    assert an["input"] == 1000 and an["cached"] == 900  # Anthropic reports cache reads outside input_tokens
    px = usage.normalize({"input_tokens": 12088, "output_tokens": 2743, "cost": {"total_cost": 0.04021},
                          "input_tokens_details": {"cached_tokens": 4736}}, "openai/gpt-6-luna")
    assert px["cost_usd"] == 0.04021 and px["cost_source"] == "provider" and px["cached"] == 4736
    tot = usage.total([oa, px])
    assert tot["calls"] == 2 and tot["input"] == 13088 and tot["cost_source"] == "mixed"


def test_model_facing_pack_is_compact(queue):
    import json as _json

    from app.engine.evidence import evidence_pack
    from app.llm.prompts import pack_json

    pack = evidence_pack(queue.cases["C1024"])
    text = pack_json(pack)
    assert '"definition"' not in text and ": " not in text[:200]
    assert len(text) < 0.9 * len(_json.dumps(pack, indent=1))  # about 21% fewer tokens (o200k)
    ids = {e["id"] for e in _json.loads(text)["evidence"]}
    assert ids == {e["id"] for e in pack["evidence"]}  # nothing the model cites is lost


def test_rules_failure_skips_the_judge_call(queue):
    case = queue.cases["C1024"]
    bad = good_narrative(case)
    bad["summary"] += " The provider billed 999 visits per week [E3]."
    provider, _ = _gen([_json_dumps(bad), _json_dumps(bad)])
    judge, judge_calls = _gen([good_judge()])
    r = assess_case(case, provider, judge, max_revisions=1)
    assert judge_calls.calls == []  # both drafts failed the rule checks, so the judge was never paid for
    assert r["source"] == "template_fallback" and r["usage"]["judge"]["calls"] == 0 and r["usage"]["writer"]["calls"] == 2


def _json_dumps(x):
    import json as _json
    return _json.dumps(x)


def test_rate_limits_are_retried_with_backoff(monkeypatch):
    import httpx

    from app.llm import providers

    monkeypatch.setattr(providers, "_sleep", lambda s: None)
    replies = [httpx.Response(429, headers={"retry-after": "1"}, json={"error": {"message": "slow down"}}),
               httpx.Response(503, text="busy"),
               httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}})]
    client = httpx.Client(transport=httpx.MockTransport(lambda r: replies.pop(0)))
    assert providers.post_with_retry(client, "https://x/y", {}).status_code == 200 and not replies
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, text="bad key")))
    assert providers.post_with_retry(client, "https://x/y", {}).status_code == 401  # never retried


def test_perplexity_claude_gets_max_tokens_and_no_temperature():
    """Perplexity requires max_output_tokens for Claude and rejects it together with temperature."""
    import json as _json

    import httpx

    from app.llm.providers import PerplexityAgentProvider, ProviderConfig

    bodies = []

    def handler(request):
        body = _json.loads(request.content)
        bodies.append(body)
        if "temperature" in body or "max_output_tokens" not in body:
            return httpx.Response(400, json={"error": {"message": "invalid request"}})
        return httpx.Response(200, json={"model": body["model"], "output_text": "ok", "usage": {}})

    cfg = ProviderConfig(provider="perplexity", model="anthropic/claude-sonnet-5", api_key="k")
    assert PerplexityAgentProvider(cfg, httpx.MockTransport(handler)).complete("s", [{"role": "user", "content": "q"}]).text == "ok"
    assert len(bodies) == 1
    cfg = ProviderConfig(provider="perplexity", model="xai/grok-4.7", api_key="k")  # unknown quirk: learned from the 400
    assert PerplexityAgentProvider(cfg, httpx.MockTransport(handler)).complete("s", [{"role": "user", "content": "q"}]).text == "ok"
