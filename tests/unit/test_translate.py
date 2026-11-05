"""Tests for the Phase 2 translate flow: registry, iron rule, format gate."""

import logging
from threading import Thread
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from bokucho import cli
from bokucho.application.contracts import NeverCancelled
from bokucho.application.errors import (
    ProviderFailure,
    ProviderMiscounted,
    ProviderRateLimited,
    ProviderRefused,
    ValidationFailure,
)
from bokucho.application.text import (
    mark_reviewed,
    mark_units_reviewed,
    recheck_report,
    save_target,
    translate_project,
)
from bokucho.application.translation_limits import (
    RATE_LIMIT_ATTEMPTS,
    TranslationLane,
    send_with_retries,
)
from bokucho.model import Status, StringFile, TextUnit
from bokucho.project import Project, TranslatorInterfaceConfig
from bokucho.sync.atomic import file_lock
from bokucho.translators import (
    get_translator,
)
from bokucho.translators.base import TranslationContext, Translator
from provider_streams import (
    ClaudeStream,
    ClaudeStreamClient,
    OpenAIStreamClient,
    openai_chunks,
)

runner = CliRunner()


def test_local_translator_constructs_without_api_key():
    get_translator("local", model="gemini-2.5-flash", base_url="http://x", api_key_env="K")


def test_get_translator_unknown_raises():
    with pytest.raises(ValueError, match="Unknown translator"):
        get_translator("nope")


def test_claude_translator_constructs_without_api_key():
    get_translator("claude", model="claude-opus-4-8")


def test_claude_translator_uses_explicit_key(monkeypatch):
    from bokucho.translators import claude

    captured = {}

    class FakeClient:
        pass

    def make_client(**kwargs):
        captured.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(claude.anthropic, "Anthropic", make_client)
    translator = claude.ClaudeTranslator(api_key="credential-secret")

    assert translator.client.__class__ is FakeClient
    assert captured == {"api_key": "credential-secret"}


def test_local_translator_prefers_explicit_key(monkeypatch):
    from bokucho.translators import local

    captured = {}

    class FakeClient:
        pass

    def make_client(**kwargs):
        captured.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(local.openai, "OpenAI", make_client)
    monkeypatch.setenv("OPENAI_API_KEY", "environment-secret")
    translator = local.LocalTranslator(model="test", api_key="credential-secret")

    assert translator.client.__class__ is FakeClient
    assert captured["api_key"] == "credential-secret"


def test_local_translator_forwards_custom_endpoint_and_request_options(monkeypatch):
    from bokucho.translators import local

    client_args = {}
    request_args = {}

    def answer(**kwargs):
        request_args.update(kwargs)
        return '["target"]'

    def make_client(**kwargs):
        client_args.update(kwargs)
        return OpenAIStreamClient(answer)

    monkeypatch.setattr(local.openai, "OpenAI", make_client)
    translator = local.LocalTranslator(
        model="vendor-model",
        base_url="https://model.example/v1",
        api_key="secret",
        request_options={"temperature": 0.25, "max_tokens": 512},
    )

    result = translator.translate(
        [TextUnit(id="u1", source="source")],
        TranslationContext(source_lang="ja", target_lang="zh-Hant"),
    )

    assert result == ["target"]
    assert client_args["base_url"] == "https://model.example/v1"
    assert request_args["temperature"] == 0.25
    assert request_args["max_tokens"] == 512


def test_claude_translator_forwards_base_url_and_request_options(monkeypatch):
    from bokucho.translators import claude

    client_args = {}
    request_args = {}

    def answer(**kwargs):
        request_args.update(kwargs)
        return '["target"]'

    def make_client(**kwargs):
        client_args.update(kwargs)
        return ClaudeStreamClient(answer)

    monkeypatch.setattr(claude.anthropic, "Anthropic", make_client)
    translator = claude.ClaudeTranslator(
        model="claude-model",
        base_url="https://claude.example",
        api_key="secret",
        request_options={"temperature": 0.1, "max_tokens": 1024},
    )

    result = translator.translate(
        [TextUnit(id="u1", source="source")],
        TranslationContext(source_lang="ja", target_lang="zh-Hant"),
    )

    assert result == ["target"]
    assert client_args["base_url"] == "https://claude.example"
    assert request_args["temperature"] == 0.1
    assert request_args["max_tokens"] == 1024


def test_provider_failure_does_not_include_sdk_message():
    from bokucho.translators.claude import ClaudeTranslator

    translator = ClaudeTranslator(api_key="credential-secret")
    translator._client = ClaudeStreamClient(ValueError("response leaked credential-secret"))

    with pytest.raises(ProviderFailure) as caught:
        translator.translate(
            [TextUnit(id="u1", source="source")],
            TranslationContext(source_lang="ja", target_lang="zh-Hant"),
        )

    assert "credential-secret" not in str(caught.value)


def _failing_local_translator(monkeypatch, error):
    from bokucho.translators import local

    class Completions:
        def create(self, **kwargs):
            del kwargs
            raise error

    class Client:
        chat = type("Chat", (), {"completions": Completions()})()

    monkeypatch.setattr(local.openai, "OpenAI", lambda **kwargs: Client())
    return local.LocalTranslator(model="m", base_url="http://x", api_key="sk-live-0123456789")


def _status_error(status, body):
    import httpx
    import openai

    response = httpx.Response(status, request=httpx.Request("POST", "http://x/chat"))
    return openai.APIStatusError(f"Error code: {status}", response=response, body=body)


def test_an_error_response_names_its_status_and_what_the_provider_said(monkeypatch):
    body = [{"error": {"code": 403, "message": "Permission denied: consumer has been suspended."}}]
    translator = _failing_local_translator(monkeypatch, _status_error(403, body))

    with pytest.raises(ProviderFailure) as caught:
        translator.translate(
            [TextUnit(id="u1", source="source")],
            TranslationContext(source_lang="ja", target_lang="zh-Hant"),
        )

    assert "HTTP 403" in str(caught.value)
    assert "consumer has been suspended" in str(caught.value)


def test_an_error_response_never_carries_the_key(monkeypatch):
    body = {"error": {"message": "API key sk-live-0123456789 not valid. api_key=other-secret"}}
    translator = _failing_local_translator(monkeypatch, _status_error(400, body))

    with pytest.raises(ProviderFailure) as caught:
        translator.translate(
            [TextUnit(id="u1", source="source")],
            TranslationContext(source_lang="ja", target_lang="zh-Hant"),
        )

    message = str(caught.value)
    assert "HTTP 400" in message
    assert "sk-live-0123456789" not in message
    assert "other-secret" not in message


def test_a_rate_limit_says_which_quota_ran_out(monkeypatch):
    import httpx
    import openai

    response = httpx.Response(429, request=httpx.Request("POST", "http://x/chat"))
    body = [{"error": {"message": "Quota exceeded for metric: requests per day, limit: 250"}}]
    error = openai.RateLimitError("Error code: 429", response=response, body=body)
    translator = _failing_local_translator(monkeypatch, error)

    with pytest.raises(ProviderRateLimited) as caught:
        translator.complete("system", "user")

    assert "HTTP 429" in str(caught.value)
    assert "requests per day" in str(caught.value)


@pytest.mark.parametrize("content", [None, '[\n  ["「咿！？', ""])
def test_openai_compatible_content_filter_is_a_refusal(content):
    """Gemini cuts an answer off where its filter stops it, mid-string."""
    from bokucho.translators.local import LocalTranslator

    reason = "content_filter: PROHIBITED_CONTENT"
    chunks = (
        [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(delta=SimpleNamespace(content=None), finish_reason=reason)
                ],
                usage=None,
            )
        ]
        if content is None
        else openai_chunks(content, finish_reason=reason)
    )
    translator = LocalTranslator("test-model", api_key="unused")
    translator._client = OpenAIStreamClient(chunks)

    with pytest.raises(ProviderRefused):
        translator.translate(
            [TextUnit(id="u1", source="source")],
            TranslationContext(source_lang="ja", target_lang="zh-Hant"),
        )


def test_claude_refusal_stop_reason_is_a_refusal():
    from bokucho.translators.claude import ClaudeTranslator

    translator = ClaudeTranslator("test-model", api_key="unused")
    translator._client = ClaudeStreamClient(ClaudeStream('["partial', stop_reason="refusal"))

    with pytest.raises(ProviderRefused):
        translator.translate(
            [TextUnit(id="u1", source="source")],
            TranslationContext(source_lang="ja", target_lang="zh-Hant"),
        )


def _local_answering(text):
    from bokucho.translators.local import LocalTranslator

    translator = LocalTranslator("test-model", api_key="unused")
    translator._client = OpenAIStreamClient(text)
    return translator


def _claude_answering(text):
    from bokucho.translators.claude import ClaudeTranslator

    translator = ClaudeTranslator("test-model", api_key="unused")
    translator._client = ClaudeStreamClient(text)
    return translator


@pytest.mark.parametrize("answering", [_local_answering, _claude_answering])
def test_an_answer_with_the_wrong_count_is_miscounted(answering):
    translator = answering('["一", "二"]')

    with pytest.raises(ProviderMiscounted) as caught:
        translator.translate(
            [TextUnit(id=f"u{index}", source="source") for index in range(3)],
            TranslationContext(source_lang="ja", target_lang="zh-Hant"),
        )

    assert "Expected 3 translations, got 2" in str(caught.value)


class FakeTranslator(Translator):
    name = "fake"

    def __init__(self):
        super().__init__()
        self.seen: list[str] = []
        self.batches: list[list[str]] = []
        self.glossary = None

    def translate(self, batch, context, glossary=None):
        self.seen.extend(u.source for u in batch)
        self.batches.append([u.source for u in batch])
        self.glossary = dict(glossary or {})
        return ["譯文" for _ in batch]


def _project(tmp_path, monkeypatch, units) -> tuple[Project, FakeTranslator]:
    proj_dir = tmp_path / "proj"
    assert runner.invoke(cli.app, ["init", str(proj_dir), "--name", "t"]).exit_code == 0
    project = Project.load(proj_dir)
    project.save_string_file(StringFile(source_file="a.lsb", units=units), "a.lsb.json")
    fake = FakeTranslator()
    monkeypatch.setattr(
        cli, "make_translation_lanes", lambda project: (TranslationLane(fake),)
    )
    return project, fake


def test_translate_only_touches_untranslated(tmp_path, monkeypatch):
    units = [
        TextUnit(id="u1", source="おはよう"),
        TextUnit(id="u2", source="こんにちは", target="人工譯", status=Status.REVIEWED),
        TextUnit(id="u3", source="やあ", target="核准譯", status=Status.REVIEWED),
        TextUnit(id="u4", source="どうも", target="機翻", status=Status.MT),
    ]
    project, fake = _project(tmp_path, monkeypatch, units)

    result = runner.invoke(cli.app, ["translate", str(project.root)])
    assert result.exit_code == 0, result.stdout

    assert fake.seen == ["おはよう"]
    [sf] = project.load_string_files()
    assert sf.by_id("u1").status is Status.MT
    assert sf.by_id("u1").target == "譯文"
    assert sf.by_id("u2").target == "人工譯"
    assert sf.by_id("u3").target == "核准譯"
    assert sf.by_id("u4").target == "機翻"


def test_translate_logs_how_many_lines_are_not_in_the_source_language(
    tmp_path, monkeypatch, caplog
):
    units = [
        TextUnit(id="u1", source="おはよう"),
        TextUnit(id="u2", source="Thank you very much for playing this game"),
    ]
    project, fake = _project(tmp_path, monkeypatch, units)

    with caplog.at_level(logging.INFO, logger="bokucho.application.text"):
        result = runner.invoke(cli.app, ["translate", str(project.root)])

    assert result.exit_code == 0, result.stdout
    assert fake.seen == ["おはよう"]
    assert "1 line(s) are not written in ja" in caplog.text


def test_translate_batches_units_across_lsb_files(tmp_path, monkeypatch):
    project, fake = _project(
        tmp_path,
        monkeypatch,
        [TextUnit(id="u1", source="おはよう")],
    )
    project.save_string_file(
        StringFile(source_file="b.lsb", units=[TextUnit(id="u2", source="こんにちは")]),
        "b.lsb.json",
    )
    translator = project.config.translator.model_copy(
        update={"batch_size": 2, "max_request_tokens": 0}
    )
    project.save_config(project.config.model_copy(update={"translator": translator}))
    project = Project.load(project.root)

    result = runner.invoke(cli.app, ["translate", str(project.root)])
    assert result.exit_code == 0, result.stdout

    assert fake.batches == [["おはよう", "こんにちは"]]
    files = {sf.source_file: sf for sf in project.load_string_files()}
    assert files["a.lsb"].by_id("u1").target == "譯文"
    assert files["b.lsb"].by_id("u2").target == "譯文"


def test_translate_format_gate_leaves_unit_untranslated(tmp_path, monkeypatch):
    project, fake = _project(tmp_path, monkeypatch, [TextUnit(id="u1", source="%name%は言う")])
    fake.translate = lambda batch, context, glossary=None: ["名字說話"]

    result = runner.invoke(cli.app, ["translate", str(project.root)])
    assert result.exit_code == 0
    [sf] = project.load_string_files()
    assert sf.by_id("u1").status is Status.UNTRANSLATED
    assert sf.by_id("u1").target == ""


def test_translate_format_gate_rejects_a_script_draft_that_drops_a_specifier(
    tmp_path, monkeypatch
):
    unit = TextUnit(id="s1", source="%sが出現！", kind="script")
    project, fake = _project(tmp_path, monkeypatch, [unit])
    fake.translate = lambda batch, context, glossary=None: ["出現了！"]

    result = runner.invoke(cli.app, ["translate", str(project.root)])
    assert result.exit_code == 0
    [sf] = project.load_string_files()
    assert (sf.by_id("s1").status, sf.by_id("s1").target) == (Status.UNTRANSLATED, "")


def test_translate_format_gate_accepts_a_percent_in_a_script_draft_without_specifiers(
    tmp_path, monkeypatch
):
    project, fake = _project(tmp_path, monkeypatch, [TextUnit(id="s1", source="Lv", kind="script")])
    project.save_config(project.config.model_copy(update={"source_lang": "en"}))
    fake.translate = lambda batch, context, glossary=None: ["等級%"]

    result = runner.invoke(cli.app, ["translate", str(project.root)])
    assert result.exit_code == 0
    [sf] = project.load_string_files()
    assert (sf.by_id("s1").status, sf.by_id("s1").target) == (Status.MT, "等級%")


def test_a_script_draft_that_drops_a_specifier_is_not_reviewed_or_kept_by_recheck(
    tmp_path, monkeypatch
):
    draft = TextUnit(
        id="s1", source="%sが出現！", target="出現了！", status=Status.MT, kind="script"
    )
    project, _ = _project(tmp_path, monkeypatch, [draft])

    with pytest.raises(ValidationFailure, match="%s"):
        mark_reviewed(project, "a.lsb", "s1")
    assert mark_units_reviewed(project, ["s1"]).marked == 0
    assert recheck_report(project, project.load_string_files()).sent_back == 1


def test_glossary_add_update_and_writeback_loop(tmp_path, monkeypatch):
    """The Phase 3 loop: a reviewer-corrected term is reused by later translate runs."""
    project, fake = _project(tmp_path, monkeypatch, [TextUnit(id="u1", source="ヒロインが笑う")])

    add = ["glossary", "add", "--project", str(project.root)]
    assert runner.invoke(cli.app, [*add, "ヒロイン", "女主角"]).exit_code == 0
    assert runner.invoke(cli.app, [*add, "ヒロイン", "希羅因"]).exit_code == 0

    listing = runner.invoke(cli.app, ["glossary", "list", "--project", str(project.root)])
    assert listing.stdout.count("ヒロイン") == 1

    result = runner.invoke(cli.app, ["translate", str(project.root)])
    assert result.exit_code == 0
    assert fake.glossary == {"ヒロイン": "希羅因"}


def test_translate_limit(tmp_path, monkeypatch):
    units = [TextUnit(id=f"u{i}", source=f"こんにちは{i}") for i in range(5)]
    project, fake = _project(tmp_path, monkeypatch, units)

    result = runner.invoke(cli.app, ["translate", str(project.root), "--limit", "2"])
    assert result.exit_code == 0
    assert len(fake.seen) == 2


def _counted_by_units(project: Project) -> Project:
    """The project batching by unit count, so no token counter is needed."""
    translator = project.config.translator.model_copy(
        update={"batch_size": 10, "max_request_tokens": 0}
    )
    project.save_config(project.config.model_copy(update={"translator": translator}))
    return Project.load(project.root)


@pytest.mark.parametrize("keys", [1, 3, 6, 8])
def test_more_keys_never_mean_less_waiting_on_a_rate_limit(keys):
    def spent(lane):
        raise ProviderRateLimited("HTTP 429")

    lanes = tuple(TranslationLane(FakeTranslator()) for _ in range(keys))
    waits: list[float] = []

    with pytest.raises(ProviderRateLimited):
        send_with_retries(
            lanes, 0, spent, input_tokens=0, token=NeverCancelled(), sleep=waits.append
        )

    assert len(waits) == RATE_LIMIT_ATTEMPTS - 1


class _SixtyTokensAUnit:
    def count(self, batch, context, glossary) -> int:
        return 60 * len(batch)


class _TpmLimiter:
    def __init__(self, tpm: int) -> None:
        self.tpm = tpm
        self.sent: list[int] = []

    def acquire(self, input_tokens, cancellation) -> None:
        if input_tokens > self.tpm:
            raise ValidationFailure("A single sentence exceeds the interface TPM limit")
        self.sent.append(input_tokens)


def test_a_tpm_below_the_request_budget_sizes_the_requests(tmp_path, monkeypatch):
    units = [TextUnit(id=f"u{i}", source=f"こんにちは{i}") for i in range(3)]
    project, fake = _project(tmp_path, monkeypatch, units)
    interface = TranslatorInterfaceConfig(
        id="metered",
        name="metered",
        provider="claude",
        model="claude-opus-4-8",
        batch_size=0,
        max_request_tokens=10240,
        tpm=150,
    )
    translator = project.config.translator.model_copy(
        update={"interfaces": [interface], "active_interface_id": "metered"}
    )
    project.save_config(project.config.model_copy(update={"translator": translator}))
    project = Project.load(project.root)
    limiter = _TpmLimiter(150)

    translate_project(
        project,
        lanes=(TranslationLane(fake, limiter),),
        token_counter=_SixtyTokensAUnit(),
    )

    assert limiter.sent == [120, 60]
    assert [sf.by_id(f"u{i}").target for sf in project.load_string_files() for i in range(3)] == [
        "譯文"
    ] * 3


def test_redo_translates_machine_drafts_again_and_leaves_human_work(tmp_path, monkeypatch):
    units = [
        TextUnit(id="u1", source="おはよう"),
        TextUnit(id="u2", source="こんにちは", target="人工譯", status=Status.REVIEWED),
        TextUnit(id="u3", source="どうも", target="機翻", status=Status.MT),
        TextUnit(id="u4", source="　", target="　", status=Status.IGNORED),
    ]
    project, fake = _project(tmp_path, monkeypatch, units)
    project = _counted_by_units(project)

    translate_project(project, lanes=(TranslationLane(fake),), redo=True)

    assert fake.seen == ["おはよう", "どうも"]
    [sf] = project.load_string_files()
    assert sf.by_id("u1").target == "譯文"
    assert sf.by_id("u2").target == "人工譯"
    assert (sf.by_id("u3").target, sf.by_id("u3").status) == ("譯文", Status.MT)
    assert sf.by_id("u4").target == "　"


def test_a_line_saved_while_a_batch_is_out_is_not_overwritten(tmp_path, monkeypatch):
    units = [
        TextUnit(id="u1", source="おはよう"),
        TextUnit(id="u2", source="こんにちは", target="機翻", status=Status.MT),
    ]
    project, fake = _project(tmp_path, monkeypatch, units)
    project = _counted_by_units(project)

    def translate(batch, context, glossary=None):
        save_target(project, "a.lsb", "u2", "人工譯")
        mark_reviewed(project, "a.lsb", "u2")
        return ["譯文" for _ in batch]

    fake.translate = translate
    translate_project(project, lanes=(TranslationLane(fake),))

    [sf] = project.load_string_files()
    assert sf.by_id("u1").target == "譯文"
    assert (sf.by_id("u2").target, sf.by_id("u2").status) == ("人工譯", Status.REVIEWED)


def test_a_line_edited_while_it_is_translated_keeps_the_edit(tmp_path, monkeypatch):
    project, fake = _project(tmp_path, monkeypatch, [TextUnit(id="u1", source="おはよう")])
    project = _counted_by_units(project)

    def translate(batch, context, glossary=None):
        save_target(project, "a.lsb", "u1", "早安")
        return ["譯文" for _ in batch]

    fake.translate = translate
    result = translate_project(project, lanes=(TranslationLane(fake),))

    [sf] = project.load_string_files()
    assert (sf.by_id("u1").target, sf.by_id("u1").status) == ("早安", Status.UNTRANSLATED)
    assert result.translated == 0


def test_a_draft_is_saved_into_the_file_a_write_in_progress_leaves(tmp_path, monkeypatch):
    units = [TextUnit(id="u1", source="おはよう"), TextUnit(id="u2", source="やあ")]
    project, _ = _project(tmp_path, monkeypatch, units)
    with file_lock(project.strings_dir / "a.lsb.json"):
        saving = Thread(target=save_target, args=(project, "a.lsb", "u2", "嗨"))
        saving.start()
        saving.join(0.2)
        [sf] = project.load_string_files()
        sf.by_id("u1").target = "早安"
        project.save_string_file(sf, "a.lsb.json")
    saving.join()

    [sf] = project.load_string_files()
    assert (sf.by_id("u1").target, sf.by_id("u2").target) == ("早安", "嗨")


def test_redo_keeps_the_old_draft_when_the_new_answer_is_rejected(tmp_path, monkeypatch):
    units = [TextUnit(id="u1", source="%name%は言う", target="%name%說", status=Status.MT)]
    project, fake = _project(tmp_path, monkeypatch, units)
    project = _counted_by_units(project)
    fake.translate = lambda batch, context, glossary=None: ["名字說話"]

    translate_project(project, lanes=(TranslationLane(fake),), redo=True)

    [sf] = project.load_string_files()
    assert (sf.by_id("u1").target, sf.by_id("u1").status) == ("%name%說", Status.MT)


def test_time_spent_paused_is_not_counted_as_elapsed(tmp_path, monkeypatch):
    project, fake = _project(tmp_path, monkeypatch, [TextUnit(id="u1", source="おはよう")])
    project = _counted_by_units(project)
    now = [0.0]

    class PausedOnce:
        def add_cancel_callback(self, callback):
            del callback

        def raise_if_cancelled(self):
            now[0] += 100.0

    published = []

    class Sink:
        def publish(self, event):
            published.append(event)

    translate_project(
        project,
        lanes=(TranslationLane(fake),),
        sink=Sink(),
        token=PausedOnce(),
        clock=lambda: now[0],
    )

    assert max(event.metrics["elapsed_seconds"] for event in published) < 100.0
