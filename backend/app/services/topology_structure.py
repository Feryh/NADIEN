"""Struktur graf dari link: ring, mesh, pair redundan, dan single point of failure.

Modul murni (tanpa DB). Label mengikuti bentuk graf yang sebenarnya, tidak pernah
diasumsikan ring.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

ClusterKind = Literal["ring", "mesh", "pair"]
Pair = tuple[int, int]


@dataclass(frozen=True)
class Cluster:
    id: str
    kind: ClusterKind
    device_ids: frozenset[int]


@dataclass(frozen=True)
class Structure:
    clusters: list[Cluster]
    cluster_of: dict[int, str]
    spof_links: frozenset[int]
    spof_devices: frozenset[int]


def _group_links_by_pair(links: Iterable[tuple[int, int, int]]) -> dict[Pair, list[int]]:
    pair_links: dict[Pair, list[int]] = defaultdict(list)
    for link_id, device_a, device_b in links:
        if device_a == device_b:
            continue
        pair_links[(min(device_a, device_b), max(device_a, device_b))].append(link_id)
    return pair_links


def _biconnected_components(adjacency: dict[int, list[int]]) -> list[list[Pair]]:
    """Tarjan iteratif dengan stack edge (rekursi akan jebol pada rantai ribuan node)."""
    discovery: dict[int, int] = {}
    low: dict[int, int] = {}
    components: list[list[Pair]] = []
    counter = 0

    for root in adjacency:
        if root in discovery:
            continue
        counter += 1
        discovery[root] = low[root] = counter
        edge_stack: list[Pair] = []
        # frame: (node, parent, indeks tetangga berikutnya)
        frames: list[tuple[int, int | None, int]] = [(root, None, 0)]
        while frames:
            node, parent, index = frames.pop()
            neighbours = adjacency[node]
            if index < len(neighbours):
                frames.append((node, parent, index + 1))
                neighbour = neighbours[index]
                if neighbour == parent:
                    continue
                if neighbour not in discovery:
                    counter += 1
                    discovery[neighbour] = low[neighbour] = counter
                    edge_stack.append((node, neighbour))
                    frames.append((neighbour, node, 0))
                elif discovery[neighbour] < discovery[node]:
                    edge_stack.append((node, neighbour))
                    low[node] = min(low[node], discovery[neighbour])
            elif parent is not None:
                low[parent] = min(low[parent], low[node])
                if low[node] >= discovery[parent]:
                    components.append(_pop_component(edge_stack, (parent, node)))
    return components


def _pop_component(edge_stack: list[Pair], stop_edge: Pair) -> list[Pair]:
    component: list[Pair] = []
    while True:
        edge = edge_stack.pop()
        component.append(edge)
        if edge == stop_edge:
            return component


def _classify(component_pairs: list[Pair],
              pair_links: dict[Pair, list[int]]) -> tuple[ClusterKind | None, frozenset[int]]:
    """Return (kind, devices) untuk cluster, atau (None, devices) untuk bridge."""
    devices = frozenset(device for edge in component_pairs for device in edge)
    if len(devices) == 2:
        key = (min(devices), max(devices))
        return ("pair" if len(pair_links[key]) >= 2 else None), devices
    return ("ring" if len(component_pairs) == len(devices) else "mesh"), devices


def _cluster_id(devices: frozenset[int]) -> str:
    """Two biconnected components share at most one device, so the two smallest members are unique per cluster
    (the smallest alone repeats when an articulation point is the lowest id of both)."""
    first, second = sorted(devices)[:2]
    return f"c:{first}-{second}"


def analyse(links: Iterable[tuple[int, int, int]]) -> Structure:
    pair_links = _group_links_by_pair(links)
    adjacency: dict[int, list[int]] = defaultdict(list)
    for device_a, device_b in pair_links:
        adjacency[device_a].append(device_b)
        adjacency[device_b].append(device_a)

    clusters: list[Cluster] = []
    spof_links: set[int] = set()
    membership_count: dict[int, int] = defaultdict(int)
    for component_pairs in _biconnected_components(adjacency):
        kind, devices = _classify(component_pairs, pair_links)
        for device in devices:
            membership_count[device] += 1
        if kind is None:
            spof_links.update(pair_links[(min(devices), max(devices))])
        else:
            clusters.append(Cluster(_cluster_id(devices), kind, devices))

    # Perangkat di >= 2 komponen biconnected adalah articulation point.
    spof_devices = frozenset(d for d, count in membership_count.items() if count >= 2)
    return Structure(clusters, _assign_clusters(clusters), frozenset(spof_links), spof_devices)


def _assign_clusters(clusters: list[Cluster]) -> dict[int, str]:
    # Urut: terbesar dulu, lalu id terkecil, supaya pilihan deterministik.
    ordered = sorted(clusters, key=lambda c: (-len(c.device_ids), min(c.device_ids)))
    cluster_of: dict[int, str] = {}
    for cluster in ordered:
        for device in cluster.device_ids:
            cluster_of.setdefault(device, cluster.id)
    return cluster_of
