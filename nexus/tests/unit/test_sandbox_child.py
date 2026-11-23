"""V3 PR4：沙箱子程序本身（不開 Job，直接呼叫 main）——任何平台都跑"""
import json
import os

import pytest

from nexus.sandbox import child
from nexus.sandbox.runner import child_command
from nexus.sandbox.workspace import Workspace


def run(tmp_path, task, capsys):
    ws = Workspace(str(tmp_path))
    with open(os.path.join(ws.input, child.TASK_FILE), "w", encoding="utf-8") as f:
        json.dump(task, f)
    cwd = os.getcwd()
    try:
        rc = child.main(ws.path)
    finally:
        os.chdir(cwd)
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.strip()]
    return rc, lines, ws


def test_write_puts_file_in_output(tmp_path, capsys):
    rc, ev, ws = run(tmp_path, {"type": "sandbox.probe", "params": {"mode": "write", "bytes": 10, "name": "../x.mp4"}}, capsys)
    assert rc == 0 and ev[-1] == {"event": "result", "result": {"wrote": 10}}
    assert os.listdir(ws.output) == ["x.mp4"]          # 名字裡的 ../ 被拿掉，只會寫在 output


def test_sleep_reports_progress(tmp_path, capsys):
    rc, ev, _ = run(tmp_path, {"type": "sandbox.probe", "params": {"mode": "sleep", "seconds": 0.5}}, capsys)
    assert rc == 0 and any(e["event"] == "progress" for e in ev) and ev[-1]["event"] == "result"


@pytest.mark.parametrize("task,code", [
    ({"type": "shell", "params": {"cmd": "dir"}}, "ADAPTER_UNSUPPORTED"),
    ({"type": "sandbox.probe", "params": {"mode": "rm-rf"}}, "INVALID_INPUT"),
    ({"type": "sandbox.probe", "params": {"mode": "write", "bytes": 1 << 30}}, "INVALID_INPUT"),
])
def test_unknown_or_bad_tasks_refused(tmp_path, capsys, task, code):
    rc, ev, _ = run(tmp_path, task, capsys)
    assert rc == 2 and ev[-1]["event"] == "error" and ev[-1]["code"] == code


def test_missing_task_file_is_error_without_traceback(tmp_path, capsys):
    ws = Workspace(str(tmp_path))
    assert child.main(ws.path) == 4
    ev = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert ev["code"] == "ADAPTER_ERROR" and "Traceback" not in ev["message"] and str(tmp_path) not in ev["message"]


def test_child_command_is_argument_list_without_shell(tmp_path):
    cmd = child_command(str(tmp_path))
    assert isinstance(cmd, list) and cmd[-2:] == ["--nexus-run", str(tmp_path)] and "-I" in cmd
