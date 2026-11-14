import pytest

from radon.aggregate import FunctionComplexityAggregate


def shard(team, *complexities):
    return {
        'team': team,
        'functions': [
            {'team': team, 'complexity': complexity}
            for complexity in complexities
        ],
    }


def test_distribution_keeps_real_multiplicity_and_order():
    aggregate = FunctionComplexityAggregate.from_shards(
        [shard('payments', 7, 3, 3, 12)]
    )
    assert aggregate.distribution('payments') == {3: 2, 7: 1, 12: 1}
    assert list(aggregate.distribution('payments')) == [3, 7, 12]
    assert aggregate.sample_size('payments') == 4


def test_p90_uses_ceiling_rank():
    aggregate = FunctionComplexityAggregate.from_shards(
        [shard('core', *range(1, 11))]
    )
    # ceil(0.9 * 10) = 9th value of 1..10
    assert aggregate.p90('core') == 9


def test_p90_with_duplicates():
    aggregate = FunctionComplexityAggregate.from_shards(
        [shard('core', 5, 5, 5)]
    )
    assert aggregate.p90('core') == 5
    assert aggregate.sample_size('core') == 3


def test_ceiling_rank_avoids_interpolation():
    aggregate = FunctionComplexityAggregate.from_shards(
        [shard('core', *range(1, 101))]
    )
    # rank = ceil(0.9 * 100) = 90, value 90, never interpolated to 90.x
    assert aggregate.p90('core') == 90
    assert aggregate.percentile(100, 'core') == 100


def test_shards_merge_additively():
    aggregate = FunctionComplexityAggregate.from_shards(
        [
            shard('payments', 3, 3),
            shard('payments', 3, 8),
            shard('search', 2),
        ]
    )
    assert aggregate.distribution('payments') == {3: 3, 8: 1}
    assert aggregate.distribution('search') == {2: 1}
    assert aggregate.sample_size('payments') == 4
    assert aggregate.teams == ['payments', 'search']


def test_aggregate_instances_are_mergeable():
    left = FunctionComplexityAggregate.from_shards(
        [shard('payments', 1), shard('search', 9)]
    )
    right = FunctionComplexityAggregate.from_shards(
        [shard('payments', 1, 5)]
    )
    left.merge(right)
    assert left.distribution('payments') == {1: 2, 5: 1}
    assert left.distribution('search') == {9: 1}


def test_to_dict_roundtrip_preserves_exact_distribution():
    original = FunctionComplexityAggregate.from_shards(
        [shard('payments', 3, 3, 8), shard('search', 2)]
    )
    restored = FunctionComplexityAggregate.from_dict(original.to_dict())
    assert restored.to_dict() == original.to_dict()
    assert restored.distribution() == original.distribution()
    assert restored.p90() == original.p90()


def test_rank_boundary_filter_applies_before_p90():
    # 4 -> rank A, 8 -> rank B, 15 -> rank C
    aggregate = FunctionComplexityAggregate.from_shards(
        [shard('core', 4, 4, 8, 15)]
    )
    only_a = aggregate.distribution('core', ranks=('A',))
    assert only_a == {4: 2}
    assert aggregate.sample_size('core', ranks=('A',)) == 2
    # ceil(0.9 * 2) = 2nd value among [4, 4]
    assert aggregate.p90('core', ranks=('A',)) == 4
    assert aggregate.p90('core', ranks=('B',)) == 8
    assert aggregate.p90('core', ranks=('A', 'B')) == 8


def test_rank_filter_matching_nothing_is_an_empty_sample():
    aggregate = FunctionComplexityAggregate.from_shards(
        [shard('core', 1, 2, 3)]
    )
    assert aggregate.sample_size('core', ranks=('F',)) == 0
    assert aggregate.distribution('core', ranks=('F',)) == {}
    assert aggregate.p90('core', ranks=('F',)) is None


def test_team_without_functions_has_zero_sample_and_no_score():
    aggregate = FunctionComplexityAggregate.from_shards(
        [shard('idle'), shard('active', 4)]
    )
    assert 'idle' in aggregate.teams
    assert aggregate.sample_size('idle') == 0
    assert aggregate.distribution('idle') == {}
    assert aggregate.p90('idle') is None
    summary = aggregate.team_summary('idle')
    assert summary == {
        'team': 'idle',
        'sample_size': 0,
        'distribution': {},
        'p90': None,
    }


def test_unknown_team_has_zero_sample_and_no_score():
    aggregate = FunctionComplexityAggregate()
    assert aggregate.sample_size('ghost') == 0
    assert aggregate.p90('ghost') is None


def test_splitting_big_team_does_not_equal_weight_teams():
    # A large team owns 99 functions, a single-function team owns one.
    # The large team arrives as many small shards; merging must stay
    # function-weighted instead of giving each shard or team equal weight.
    shards = [shard('big', 100, 100) for _ in range(49)]
    shards.append(shard('big', 100))
    shards.append(shard('tiny', 1))
    aggregate = FunctionComplexityAggregate.from_shards(shards)

    assert aggregate.sample_size('big') == 99
    assert aggregate.sample_size('tiny') == 1
    assert aggregate.sample_size() == 100
    # 90th rank out of 100 sits inside the big team's 99 copies of 100
    assert aggregate.p90() == 100
    assert aggregate.p90('big') == 100
    assert aggregate.p90('tiny') == 1
    assert aggregate.distribution() == {1: 1, 100: 99}


def test_overall_summary_is_function_weighted():
    aggregate = FunctionComplexityAggregate.from_shards(
        [
            shard('big', *([10] * 90)),
            shard('small', *([1] * 10)),
        ]
    )
    summary = aggregate.overall_summary()
    assert summary['sample_size'] == 100
    assert summary['distribution'] == {1: 10, 10: 90}
    assert summary['p90'] == 10


def test_alternative_record_formats():
    tuple_shard = {'team': 'a', 'functions': [('a', 2), ('a', 2)]}
    mapping_shard = {'team': 'b', 'functions': {5: 3}}
    inherited_team = {'functions': [{'complexity': 7}, {'complexity': 7}]}
    inherited_team['team'] = 'c'
    aggregate = FunctionComplexityAggregate.from_shards(
        [tuple_shard, mapping_shard, inherited_team]
    )
    assert aggregate.distribution('a') == {2: 2}
    assert aggregate.distribution('b') == {5: 3}
    assert aggregate.distribution('c') == {7: 2}


def test_percentile_validation():
    aggregate = FunctionComplexityAggregate.from_shards([shard('a', 1)])
    for invalid in (0, -5, 101):
        with pytest.raises(ValueError):
            aggregate.percentile(invalid, 'a')


def test_negative_complexity_rejected():
    with pytest.raises(ValueError):
        FunctionComplexityAggregate.from_shards([shard('a', -1)])
