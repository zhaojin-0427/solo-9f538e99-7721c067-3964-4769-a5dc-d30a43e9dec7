"""名称解析：把一个观测名称沿同义名指向关系追踪到接受名。

规则（按需求语义）：
- 同义名指向关系是有向边，允许分叉（一个名称可指向多个目标）。
- 从观测名称出发，沿所有路径追踪。
- 只有「所有可达路径无环，且每条极大路径都终止于同一个接受名」时才纳入供数。
- 通向不同接受名、遇到环、或不存在可达接受名的记录列为待核并说明原因。

实现：
1. BFS 求从起点出发的可达子图；
2. Kahn 拓扑排序判断可达子图是否有环（有剩余入度即有环）；
3. 统计可达子图中的“汇聚点”（无出边的节点）：
   - 汇聚点必须全部是接受名；
   - 汇聚点恰好一个 => 纳入；多个不同接受名 => 分叉；零个且无环 => 无接受名。
"""
from __future__ import annotations

from dataclasses import dataclass, field

REASON_CYCLE = "cycle"
REASON_DIVERGENT = "divergent_accepted"
REASON_NO_ACCEPTED = "no_accepted_name"


@dataclass
class Resolution:
    included: bool
    accepted_name: str | None = None
    reason_code: str | None = None
    reason: str | None = None
    reachable: set[str] = field(default_factory=set)


def resolve_name(
    start: str,
    adjacency: dict[str, list[str]],
    accepted: set[str],
) -> Resolution:
    """解析单个名称。

    :param start: 观测中的物种名
    :param adjacency: 名称 -> 指向的名称列表（分叉即一个键对应多个值）
    :param accepted: 接受名集合
    """
    # 1. 可达性 BFS
    reachable: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node in reachable:
            continue
        reachable.add(node)
        for nxt in adjacency.get(node, ()):
            if nxt not in reachable:
                stack.append(nxt)

    # 2. 可达子图内的入度（Kahn 环检测）
    indegree: dict[str, int] = {node: 0 for node in reachable}
    for node in reachable:
        for nxt in adjacency.get(node, ()):
            if nxt in reachable:
                indegree[nxt] += 1

    queue = [node for node in reachable if indegree[node] == 0]
    visited_count = 0
    head = 0
    while head < len(queue):
        node = queue[head]
        head += 1
        visited_count += 1
        for nxt in adjacency.get(node, ()):
            if nxt in reachable:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    queue.append(nxt)
    has_cycle = visited_count < len(reachable)

    # 3. 汇聚点：可达子图中无出边（或出边全部指向子图外，本图结构下不会发生）的节点
    sinks = sorted(
        node
        for node in reachable
        if not any(nxt in reachable for nxt in adjacency.get(node, ()))
    )
    sink_accepted = sorted({node for node in sinks if node in accepted})
    non_accepted_sinks = [node for node in sinks if node not in accepted]

    if has_cycle:
        return Resolution(
            included=False,
            reason_code=REASON_CYCLE,
            reason="名称指向关系中存在环，路径无法到达稳定的接受名，待人工核实",
            reachable=reachable,
        )
    if len(sink_accepted) > 1:
        return Resolution(
            included=False,
            reason_code=REASON_DIVERGENT,
            reason=(
                "不同路径通向不同接受名："
                + "、".join(sink_accepted)
                + "，存在分叉冲突，待人工核实"
            ),
            reachable=reachable,
        )
    if len(sink_accepted) == 1 and not non_accepted_sinks:
        return Resolution(
            included=True,
            accepted_name=sink_accepted[0],
            reachable=reachable,
        )
    # 无环、且没有形成单一接受名汇聚
    if sink_accepted:
        reason = (
            "部分路径终止于非接受名（"
            + "、".join(non_accepted_sinks)
            + "），未能全部汇聚到接受名 "
            + sink_accepted[0]
            + "，待人工核实"
        )
    elif start in accepted:
        reason = f"名称 {start} 本身是接受名，但存在无法到达接受名的路径，待人工核实"
    else:
        reason = "从该名称出发没有可达的接受名，待人工核实"
    return Resolution(
        included=False,
        reason_code=REASON_NO_ACCEPTED,
        reason=reason,
        reachable=reachable,
    )
