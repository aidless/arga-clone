"""结果文件的写入纪律：原子替换 + 回读校验。

照搬工作区台账脚本（`../scripts/ledger_append.py`）的两条纪律：
落笔后要能重新解析、并且读出来的必须与写进去的一致。理由在这里更硬——
agent_compare 的输出会被直接引用成"某模型 pass 率 94.2%"，一个被截断或
被半覆盖的 JSON 看起来和完好文件一模一样。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "demo"))
from agent_compare import write_results  # noqa: E402


def test_write_then_read_back(tmp_path):
    target = tmp_path / "out.json"
    n = write_results(str(target), [{"verdict": "PASS"}])
    assert n == target.stat().st_size
    assert json.loads(target.read_text()) == [{"verdict": "PASS"}]


def test_overwrites_a_broken_file_atomically(tmp_path):
    """旧文件是坏 JSON 时，不能出现"写到一半失败、留下半截文件"。"""
    target = tmp_path / "out.json"
    target.write_text('{"broken": ')
    write_results(str(target), {"ok": True})
    assert json.loads(target.read_text()) == {"ok": True}


def test_leaves_no_temp_files_behind(tmp_path):
    target = tmp_path / "out.json"
    write_results(str(target), [1, 2, 3])
    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp-" in p.name]
    assert leftovers == []


def test_unserializable_payload_does_not_replace_the_good_file(tmp_path):
    """写失败必须保留旧文件——否则一次失败会同时毁掉新旧两份证据。"""
    target = tmp_path / "out.json"
    write_results(str(target), {"good": 1})
    with pytest.raises(TypeError):
        write_results(str(target), {"bad": object()})
    assert json.loads(target.read_text()) == {"good": 1}
    assert [p.name for p in tmp_path.iterdir() if ".tmp-" in p.name] == []
