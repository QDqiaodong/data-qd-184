"""披露选择的反算核查与最优遮蔽搜索。

一张候选表公开全部行小计、列小计、必须公开格与未遮蔽可选格；这些显示的
事实界定所有相容的非负整数分表：行列和相同、可见格等于显示值，隐藏格
没有其他上下界。敏感格要求在这批相容表上的最大值减最小值达到自己的
min_span_cents，边界必须能由整张相容表达到。

把真值在隐藏格上的取值当作一个可行流 f：隐藏格构成运输多面体
（残量行列和 + 非负整数），任一相容表与 f 之差是支撑在隐藏格上的零散度
环流。于是目标格 (p,q) 的最大增量等于残量有向图中 q→p 的最大流（行→列
弧容量为总量、列→行弧容量为当前值，并移除 (p,q) 自身的两条弧），最大减量
同理为 p→q 的最大流；两者之和即该格在相容表上的可达跨度。约束矩阵全单模，
整数容量的最大流给出精确的整数界。

在所有满足全部敏感格跨度的遮蔽选择中，依次最小化遮蔽损失之和、遮蔽格数、
以及升序排列的遮蔽键列表的字典序；枚举全部可选格子集，不以先遇到的可行
表充当最优答案。
"""
from collections import deque


def _maxflow(arcs, source, sink, node_count):
    """Edmonds–Karp 最大流；arcs 为 (起点, 终点, 容量) 三元组，容量为整数。"""
    graph = [[] for _ in range(node_count)]
    for start, end, cap in arcs:
        graph[start].append([end, cap, len(graph[end])])
        graph[end].append([start, 0, len(graph[start]) - 1])
    total = 0
    while True:
        prev = [None] * node_count
        prev[source] = (source, -1)
        queue = deque([source])
        while queue and prev[sink] is None:
            node = queue.popleft()
            for index, edge in enumerate(graph[node]):
                if edge[1] and prev[edge[0]] is None:
                    prev[edge[0]] = (node, index)
                    queue.append(edge[0])
        if prev[sink] is None:
            return total
        flow = None
        node = sink
        while node != source:
            node, index = prev[node]
            cap = graph[node][index][1]
            flow = cap if flow is None or cap < flow else flow
        node = sink
        while node != source:
            node, index = prev[node]
            edge = graph[node][index]
            edge[1] -= flow
            graph[edge[0]][edge[2]][1] += flow
        total += flow


def _residual_arcs(categories, custodians, truth, hidden):
    """以真值为参考可行流，构造隐藏格运输多面体的残量弧与节点编号。"""
    nodes = {}
    for category in categories:
        nodes[("row", category)] = len(nodes)
    for custodian in custodians:
        nodes[("column", custodian)] = len(nodes)
    total = sum(truth[key] for key in hidden)
    arcs = []
    for key in hidden:
        row = nodes[("row", key[0])]
        column = nodes[("column", key[1])]
        arcs.append((row, column, total))        # 增加值：至多全仓总量
        arcs.append((column, row, truth[key]))   # 减少值：至多当前值
    return nodes, arcs


def cell_bounds(categories, custodians, truth, hidden, target):
    """目标隐藏格在全部相容非负整数表上可达的 (最小值, 最大值)。

    ``truth`` 为 (category, custodian) -> 真值；``hidden`` 为隐藏格键集；
    可见格按真值固定，行列小计按真值汇总，隐藏格只受行列和约束。
    """
    nodes, arcs = _residual_arcs(categories, custodians, truth, hidden)
    row = nodes[("row", target[0])]
    column = nodes[("column", target[1])]
    kept = [(start, end, cap) for start, end, cap in arcs
            if (start, end) != (row, column) and (start, end) != (column, row)]
    node_count = len(nodes)
    current = truth[target]
    high = current + _maxflow(kept, column, row, node_count)
    # 减量环流还要经过目标格自身的列→行弧（容量为当前值），一并取限。
    decrease = min(current, _maxflow(kept, row, column, node_count))
    low = current - decrease
    return low, high


def optimize(categories, custodians, truth, policies):
    """返回最优遮蔽的可选格键集；没有任何合格选择时返回 None。"""
    protected = {key: policy["min_span_cents"]
                 for key, policy in policies.items() if policy["mode"] == "protected"}
    optional = {key: policy["suppression_cost"]
                for key, policy in policies.items() if policy["mode"] == "optional"}

    # 跨度随遮蔽集合单调不减：全部遮蔽仍达不到的敏感格永远达不到。
    full_hidden = frozenset(protected) | frozenset(optional)
    for target, required in protected.items():
        low, high = cell_bounds(categories, custodians, truth, full_hidden, target)
        if high - low < required:
            return None

    opt_keys = sorted(optional)
    best_rank = None
    best_subset = None
    for mask in range(1 << len(opt_keys)):
        subset = frozenset(opt_keys[i] for i in range(len(opt_keys))
                           if mask & (1 << i))
        hidden = frozenset(protected) | subset
        feasible = True
        for target, required in protected.items():
            low, high = cell_bounds(categories, custodians, truth, hidden, target)
            if high - low < required:
                feasible = False
                break
        if not feasible:
            continue
        cost = sum(optional[key] for key in subset)
        rank = (cost, len(subset), tuple(sorted(subset)))
        if best_rank is None or rank < best_rank:
            best_rank = rank
            best_subset = subset
    return best_subset
