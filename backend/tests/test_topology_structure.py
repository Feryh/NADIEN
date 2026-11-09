from app.services.topology_structure import analyse

RING = [(1, 1, 2), (2, 2, 3), (3, 3, 4), (4, 4, 1)]


def test_should_label_ring_when_four_devices_form_one_cycle():
    result = analyse(RING)
    assert len(result.clusters) == 1
    assert result.clusters[0].kind == "ring"
    assert result.clusters[0].device_ids == frozenset({1, 2, 3, 4})
    assert result.spof_links == frozenset()
    assert result.spof_devices == frozenset()


def test_should_label_mesh_when_component_has_more_pairs_than_devices():
    result = analyse(RING + [(5, 1, 3)])
    assert [c.kind for c in result.clusters] == ["mesh"]


def test_should_mark_link_as_spof_when_it_is_the_only_path():
    result = analyse(RING + [(6, 4, 5)])
    assert result.spof_links == frozenset({6})
    assert result.spof_devices == frozenset({4})
    assert 5 not in result.cluster_of


def test_should_label_pair_when_two_parallel_links_join_same_devices():
    result = analyse([(1, 1, 2), (2, 1, 2)])
    assert len(result.clusters) == 1
    assert result.clusters[0].kind == "pair"
    assert result.clusters[0].device_ids == frozenset({1, 2})
    assert result.spof_links == frozenset()


def test_should_ignore_self_loop_when_both_ends_same_device():
    result = analyse([(1, 1, 1)])
    assert result.clusters == []
    assert result.spof_links == frozenset()
    assert result.spof_devices == frozenset()


def test_should_assign_articulation_device_to_largest_cluster_when_in_two():
    result = analyse(RING + [(5, 4, 5), (6, 5, 6), (7, 6, 4)])
    assert result.cluster_of[4] == "c:1-2"


def test_should_handle_long_chain_without_recursion_error():
    chain = [(i, i, i + 1) for i in range(1, 6000)]
    result = analyse(chain)
    assert len(result.spof_links) == 5999


def test_should_give_distinct_ids_when_two_rings_share_an_articulation_point():
    result = analyse([(10, 1, 2), (11, 2, 3), (12, 3, 1), (20, 1, 4), (21, 4, 5), (22, 5, 1)])
    ids = [c.id for c in result.clusters]
    assert len(ids) == 2
    assert len(set(ids)) == 2
    assert result.cluster_of[2] != result.cluster_of[4]
    assert result.cluster_of[1] in ids


def test_should_keep_both_rings_when_graph_is_disconnected():
    result = analyse(RING + [(11, 11, 12), (12, 12, 13), (13, 13, 11)])
    assert sorted(c.device_ids for c in result.clusters) == [frozenset({1, 2, 3, 4}), frozenset({11, 12, 13})]
    assert result.spof_links == frozenset()


def test_should_mark_every_link_as_spof_when_graph_is_a_star():
    result = analyse([(1, 1, 2), (2, 1, 3), (3, 1, 4)])
    assert result.clusters == []
    assert result.spof_links == frozenset({1, 2, 3})
    assert result.spof_devices == frozenset({1})


def test_should_count_pair_once_when_duplicate_links_are_listed_in_either_order():
    result = analyse([(1, 1, 2), (2, 2, 1)])
    assert [c.kind for c in result.clusters] == ["pair"]
    assert result.spof_links == frozenset()


def test_should_stay_ring_when_a_parallel_link_sits_inside_it():
    result = analyse(RING + [(5, 1, 2)])
    assert [c.kind for c in result.clusters] == ["ring"]


def test_should_mark_device_as_spof_when_removing_it_disconnects_the_graph():
    result = analyse([(1, 1, 2), (2, 2, 3)])
    assert result.spof_devices == frozenset({2})
