"""S5：nexus/display.py 不碰 tkinter；新介面（電腦版 _web_nexus_full／web_nx_detail）不借用 nexus.gui"""
import ast
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def test_display_without_tkinter():
    code = ("import sys; sys.modules['tkinter'] = None\n"
            "import nexus.display as d\n"
            "assert d.fmt_size(1536) == '1.5 KB' and d.ago(None) == '—' and d.who('owner', {}) == '你'\n"
            "assert d.TASK_STATE_NAME['SUCCEEDED'] == '成功' and d.DEVICE_STATE_NAME['ACTIVE'] == '已核准'\n"
            "assert 'tkinter' not in [m for m in sys.modules if sys.modules[m] is not None]\n")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_summary_and_download_result():
    from nexus import display as d
    t = {"manifest_json": json.dumps({"params": {"url": "https://www.xvideos.com/video123/some-title", "quality": "720",
                                                 "return_to_master": True}})}
    assert d.summary_of(t) == "xvideos.com／some-title（720p，傳回）"
    r = json.dumps({"site": "xvideos.com", "title": "x", "returned": True, "files": [{"name": "a.mp4", "size": 2048, "path": "C:/v/a.mp4"}]})
    assert "已傳回這台：C:/v/a.mp4" in d.download_result(r, "書房")


def test_old_names_still_work():
    import pytest
    pytest.importorskip("tkinter")
    from nexus import display as d
    from nexus.gui import devices_tab as dt
    from nexus.gui import tasks_tab as tt
    assert dt.STATE_NAME is d.DEVICE_STATE_NAME and dt._ago is d.ago and dt._local is d.local_min
    assert tt.STATE_NAME is d.TASK_STATE_NAME and tt.summary_of is d.summary_of and tt._who is d.who
    assert tt.DetailWindow._download_result is d.download_result


def test_web_ui_does_not_use_tk_tabs():
    src = open(os.path.join(ROOT, "video_helper_app.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and (node.name.startswith("web_nx") or node.name in ("_web_nexus_full", "_nx", "init_nexus")):
            text = ast.get_source_segment(src, node)
            assert "nexus.gui" not in text, f"{node.name} 還在用 nexus.gui"
