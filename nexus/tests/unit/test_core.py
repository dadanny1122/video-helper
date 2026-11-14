import json
import os
import time

import pytest

from nexus.core import config as cfgmod
from nexus.core.enums import (
    DEVICE_TRANSITIONS,
    TERMINAL_DEVICE_STATES,
    DeviceStatus,
    can_transition,
)
from nexus.core.errors import ERROR_CODES, NexusError
from nexus.core.masking import mask, mask_dict
from nexus.protocol.messages import ReplayGuard, check_envelope, envelope, validate


# ---- 狀態機：每一格都驗 ----
def test_transition_table_terminal_states_never_leave():
    for s in TERMINAL_DEVICE_STATES:
        for t in DeviceStatus:
            assert not can_transition(s, t), (s, t)


def test_revoked_can_never_become_active_again():
    assert not can_transition(DeviceStatus.REVOKED, DeviceStatus.ACTIVE)
    assert not can_transition(DeviceStatus.REVOKED, DeviceStatus.PENDING)


def test_only_listed_transitions_allowed():
    allowed = {(s, t) for s, ts in DEVICE_TRANSITIONS.items() for t in ts}
    for s in DeviceStatus:
        for t in DeviceStatus:
            assert can_transition(s, t) == ((s, t) in allowed)
    assert can_transition(DeviceStatus.PENDING, DeviceStatus.ACTIVE)
    assert can_transition(DeviceStatus.SUSPENDED, DeviceStatus.ACTIVE)
    assert not can_transition(DeviceStatus.PENDING, DeviceStatus.SUSPENDED)


# ---- 錯誤格式 ----
def test_error_response_format_has_no_secrets_and_right_fields():
    e = NexusError("CREDENTIAL_REVOKED", details={"device_id": "d1"})
    r = e.to_response(correlation_id="c1")
    assert set(r) == {"protocol_version", "message_id", "correlation_id", "sent_at", "error"}
    assert r["error"]["code"] == "CREDENTIAL_REVOKED" and r["error"]["retryable"] is False
    assert r["correlation_id"] == "c1"
    assert e.http_status == 403


def test_unknown_code_becomes_internal_error():
    assert NexusError("NOPE").code == "INTERNAL_ERROR"
    for status, retry, msg in ERROR_CODES.values():
        assert 400 <= status < 600 and isinstance(retry, bool) and msg


# ---- 遮罩（AT-021 單元部分） ----
def test_mask_hides_private_key_token_cookie_authorization():
    pem = "-----BEGIN EC PRIVATE KEY-----\nMHcCAQEE...\n-----END EC PRIVATE KEY-----"
    assert "MHcCAQEE" not in mask("key: " + pem)
    assert "abcdefgh1234" not in mask("Authorization: Bearer abcdefgh1234")
    assert "s3cr3tvalue" not in mask("token=s3cr3tvalue")
    assert "s3cr3tvalue" not in mask('"registration_token": "s3cr3tvalue"')
    assert "sid=xyz123" not in mask("Cookie: sid=xyz123")
    assert "X-VH-Token: [REDACTED]" in mask("X-VH-Token: deadbeefcafe")
    assert mask("hello 123") == "hello 123"


def test_mask_dict_by_key_and_nested():
    d = {"device_id": "d1", "pair_code": "123456", "nested": {"Authorization": "x" * 20, "ok": 1},
         "list": [{"token": "abcd1234"}, "token=zzzzzzzz"]}
    m = mask_dict(d)
    assert m["device_id"] == "d1" and m["pair_code"] == "[REDACTED]"
    assert m["nested"]["Authorization"] == "[REDACTED]" and m["nested"]["ok"] == 1
    assert m["list"][0]["token"] == "[REDACTED]" and "zzzz" not in m["list"][1]


# ---- 訊息外框／schema／重放 ----
BODY = {"name": (str, True, 64), "seq": (int, False, 10**9)}


def test_envelope_roundtrip_and_unknown_fields_rejected():
    msg = envelope("worker:abc", name="pc1", seq=3)
    body = check_envelope(msg, BODY)
    assert body == {"name": "pc1", "seq": 3}
    bad = dict(msg, evil="rm -rf")
    with pytest.raises(NexusError) as ei:
        check_envelope(bad, BODY)
    assert ei.value.code == "SCHEMA_INVALID" and "evil" in ei.value.details["fields"]


def test_schema_types_lengths_and_bool_is_not_int():
    with pytest.raises(NexusError):
        validate({"name": "x" * 65}, BODY)
    with pytest.raises(NexusError):
        validate({"name": "ok", "seq": True}, BODY)
    with pytest.raises(NexusError):
        validate({"seq": 1}, BODY)          # 缺必要欄位
    with pytest.raises(NexusError):
        validate(["not", "dict"], BODY)


def test_replay_guard_blocks_second_use_and_clock_skew():
    g = ReplayGuard(ttl_seconds=60, skew_seconds=300)
    m = envelope("w1")
    g.check("w1", m["nonce"], m["sent_at"])
    with pytest.raises(NexusError) as ei:
        g.check("w1", m["nonce"], m["sent_at"])
    assert ei.value.code == "REPLAY_DETECTED"
    g.check("w2", m["nonce"], m["sent_at"])      # 不同 sender 可以
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600))
    with pytest.raises(NexusError) as ei:
        g.check("w1", "n2", old)
    assert ei.value.code == "CLOCK_SKEW"


# ---- 設定檔 ----
def test_config_separate_dirs_unknown_fields_kept_and_atomic_save(tmp_path, monkeypatch):
    monkeypatch.setenv("VH_NEXUS_ROOT", str(tmp_path))
    m = cfgmod.Config("master")
    w = cfgmod.Config("worker")
    assert m.path != w.path and "master" in m.path and "worker" in w.path
    assert m["enabled"] is False and m["port"] == 47331
    with pytest.raises(KeyError):
        m["not_a_setting"] = 1
    # 手寫一個有未知欄位、舊 schema 的檔
    json.dump({"schema_version": 0, "port": 50000, "future_thing": {"a": 1}}, open(m.path, "w"))
    m2 = cfgmod.Config("master")
    assert m2["port"] == 50000 and m2.unknown == {"future_thing": {"a": 1}}
    m2["port"] = 50001
    m2.save()
    saved = json.load(open(m2.path))
    assert saved["port"] == 50001 and saved["future_thing"] == {"a": 1} and saved["schema_version"] == 1
    assert not [f for f in os.listdir(os.path.dirname(m2.path)) if f.endswith(".tmp")]
