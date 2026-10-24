import json

import pytest

import judge


class Resp:
    def __init__(self, status=200, body=None, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def log_rows(data):
    return [json.loads(x) for x in (data / "judge.jsonl").read_text().splitlines()]


class Sent(list):
    """The requests the fake transport received; `resp` queues the responses it will give."""
    def __init__(self):
        super().__init__()
        self.resp = []


@pytest.fixture
def sent(monkeypatch):
    calls = Sent()
    monkeypatch.setattr(judge, "_post", lambda url, **kw: calls.append((url, kw)) or calls.resp.pop(0))
    return calls


def test_request_shape_headers_and_log(tmp_path, monkeypatch, sent):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    sent.resp.append(Resp(body={"answers": {"s1": {"noul": 0.8}}, "model": "typesafe/jev-1.13-20260917",
                                "id": "gen-1", "usage": {"cost": 0.0002}, "provider": "TypeSafe"}))
    qs = {"s1": {"type": "noul", "instructions": "Is #1 warm?"}}
    r = judge.ask("Samples:\n#1: bell", qs, data=tmp_path)
    url, kw = sent[0]
    assert url == "https://openrouter.ai/api/alpha/decisions"
    assert kw["json"] == {"model": "typesafe/jev-1.13", "state": "Samples:\n#1: bell", "questions": qs}
    assert kw["headers"] == {"Authorization": "Bearer or-key"}
    assert kw["timeout"] == 30.0
    assert r["answers"] == {"s1": {"noul": 0.8}} and r["usage"] == {"cost": 0.0002}
    row, = log_rows(tmp_path)
    assert row["model"] == "typesafe/jev-1.13-20260917" and row["id"] == "gen-1" and row["usage"] == {"cost": 0.0002}
    assert row["state"] == "Samples:\n#1: bell" and row["questions"] == qs and row["answers"] == {"s1": {"noul": 0.8}}
    assert row["url"] == url and row["elapsed"] >= 0 and row["ts"] > 0
    assert "or-key" not in json.dumps(row)  # the key never reaches the log


def test_key_order_and_overrides(tmp_path, monkeypatch, sent):
    monkeypatch.setenv("OPENROUTER_API_KEY", "general")
    monkeypatch.setenv("SAMPLEPICK_JUDGE_KEY", "specific")
    monkeypatch.setenv("SAMPLEPICK_JUDGE_MODEL", "other/model")
    monkeypatch.setenv("SAMPLEPICK_JUDGE_TIMEOUT", "5")
    sent.resp.append(Resp(body={"answers": {}}))
    judge.ask("s", {}, data=tmp_path)
    kw = sent[0][1]
    assert kw["headers"]["Authorization"] == "Bearer specific" and kw["json"]["model"] == "other/model"
    assert kw["timeout"] == 5.0


def test_custom_url_sends_no_authorization(tmp_path, monkeypatch, sent):
    monkeypatch.setenv("SAMPLEPICK_JUDGE_URL", "http://localhost:9000/decide")
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-leak")
    sent.resp.append(Resp(body={"answers": {}}))
    judge.ask("s", {}, data=tmp_path)
    url, kw = sent[0]
    assert url == "http://localhost:9000/decide" and kw["headers"] == {}


def test_no_key_raises_before_any_call(tmp_path, sent):
    with pytest.raises(judge.JudgeError, match="no judge key"):
        judge.ask("s", {}, data=tmp_path)
    assert sent == [] and not (tmp_path / "judge.jsonl").exists()


@pytest.mark.parametrize("resp,status", [
    (Resp(status=429, text="rate limited"), 429),
    (Resp(status=200, text="<html>oops"), 200),         # 200 but not JSON
    (Resp(status=200, body={"nothing": 1}), 200),       # JSON without answers
])
def test_failed_call_is_logged_and_not_retried(tmp_path, monkeypatch, sent, resp, status):
    monkeypatch.setenv("SAMPLEPICK_JUDGE_KEY", "k")
    sent.resp.append(resp)
    with pytest.raises(judge.JudgeError) as e:
        judge.ask("the state", {"s1": {"type": "noul"}}, data=tmp_path)
    assert e.value.status == status
    assert len(sent) == 1  # no retry
    row, = log_rows(tmp_path)
    assert row["answers"]["error"]["status"] == status
    assert row["state"] == "the state" and row["questions"] == {"s1": {"type": "noul"}}
    assert row["model"] == "typesafe/jev-1.13"


def test_network_error_is_logged(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMPLEPICK_JUDGE_KEY", "k")

    def boom(url, **kw):
        raise TimeoutError("timed out")
    monkeypatch.setattr(judge, "_post", boom)
    with pytest.raises(judge.JudgeError, match="TimeoutError"):
        judge.ask("s", {"q": {}}, data=tmp_path)
    row, = log_rows(tmp_path)
    assert row["answers"]["error"]["status"] == 0 and "timed out" in row["answers"]["error"]["body"]


def test_log_defaults_to_data_dir_from_env(tmp_path, monkeypatch, sent):
    monkeypatch.setenv("SAMPLEPICK_JUDGE_KEY", "k")
    monkeypatch.setenv("SAMPLEPICK_DATA", str(tmp_path / "d"))
    sent.resp.append(Resp(body={"answers": {}}))
    judge.ask("s", {})
    assert (tmp_path / "d" / "judge.jsonl").exists()


def test_every_call_appends_one_line(tmp_path, monkeypatch, sent):
    monkeypatch.setenv("SAMPLEPICK_JUDGE_KEY", "k")
    sent.resp += [Resp(body={"answers": {}}), Resp(status=500, text="x"), Resp(body={"answers": {}})]
    judge.ask("a", {}, data=tmp_path)
    with pytest.raises(judge.JudgeError):
        judge.ask("b", {}, data=tmp_path)
    judge.ask("c", {}, data=tmp_path)
    assert [r["state"] for r in log_rows(tmp_path)] == ["a", "b", "c"]
