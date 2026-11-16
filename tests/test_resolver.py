"""解析算法单元测试：直达、链、分叉汇聚、分叉冲突、环、无接受名等。"""
from app.resolver import (
    REASON_CYCLE,
    REASON_DIVERGENT,
    REASON_NO_ACCEPTED,
    resolve_name,
)


def test_accepted_name_directly():
    r = resolve_name("A", {}, {"A"})
    assert r.included and r.accepted_name == "A"


def test_synonym_chain_to_accepted():
    adj = {"old": ["mid"], "mid": ["A"]}
    r = resolve_name("old", adj, {"A"})
    assert r.included and r.accepted_name == "A"


def test_forking_paths_converge_same_accepted():
    # 分叉到两条链，最终汇聚到同一接受名（含菱形合流）
    adj = {"old": ["s1", "s2"], "s1": ["A"], "s2": ["s3"], "s3": ["A"]}
    r = resolve_name("old", adj, {"A"})
    assert r.included and r.accepted_name == "A"


def test_forking_paths_to_different_accepted():
    adj = {"old": ["s1", "s2"], "s1": ["A"], "s2": ["B"]}
    r = resolve_name("old", adj, {"A", "B"})
    assert not r.included and r.reason_code == REASON_DIVERGENT
    assert "A" in r.reason and "B" in r.reason


def test_simple_cycle():
    adj = {"x": ["y"], "y": ["x"]}
    r = resolve_name("x", adj, {"A"})
    assert not r.included and r.reason_code == REASON_CYCLE


def test_self_loop_is_cycle():
    adj = {"x": ["x"]}
    r = resolve_name("x", adj, {"x"})
    assert not r.included and r.reason_code == REASON_CYCLE


def test_cycle_reachable_but_accepted_elsewhere():
    # 一条分支成环、另一条到接受名：存在环即待核
    adj = {"old": ["x", "A"], "x": ["y"], "y": ["x"]}
    r = resolve_name("old", adj, {"A"})
    assert not r.included and r.reason_code == REASON_CYCLE


def test_no_accepted_name_reachable():
    adj = {"old": ["unknown"]}
    r = resolve_name("old", adj, {"A"})
    assert not r.included and r.reason_code == REASON_NO_ACCEPTED


def test_dangling_branch_besides_accepted():
    # 一条路径到接受名，另一条终止于非接受名 -> 不纳入
    adj = {"old": ["A", "deadend"]}
    r = resolve_name("old", adj, {"A"})
    assert not r.included and r.reason_code == REASON_NO_ACCEPTED
    assert "deadend" in r.reason


def test_unknown_start_name():
    r = resolve_name("never_seen", {"x": ["A"]}, {"A"})
    assert not r.included and r.reason_code == REASON_NO_ACCEPTED


def test_diamond_join_with_different_terminal_detection():
    # 菱形后再次分叉到两个接受名
    adj = {"old": ["m1", "m2"], "m1": ["j"], "m2": ["j"], "j": ["A", "B"]}
    r = resolve_name("old", adj, {"A", "B"})
    assert not r.included and r.reason_code == REASON_DIVERGENT
