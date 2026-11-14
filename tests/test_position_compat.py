import unittest

from pyais import decode
from pyais.position_compat import (
    Axis,
    COARSE_SCALE,
    FINE_SCALE,
    Conflict,
    LatCell,
    PositionSample,
    compare_samples,
    disjoint_axis,
    find_conflicts,
    position_region,
    region_for_message,
)

COARSE_HALF = 0.5 / COARSE_SCALE   # 1/1200 degree
FINE_HALF = 0.5 / FINE_SCALE       # 1/1200000 degree


def sample(sample_id='anon-1', timestamp=1000.0, msg_type=27, lon=10.0, lat=50.0):
    return PositionSample(sample_id=sample_id, timestamp=timestamp,
                          msg_type=msg_type, lon=lon, lat=lat)


class RegionTestCase(unittest.TestCase):

    def test_coarse_cell_is_wider_than_fine_cell(self):
        coarse = position_region(10.0, 50.0, COARSE_SCALE)
        fine = position_region(10.0, 50.0, FINE_SCALE)
        self.assertEqual(coarse.lon.segments, ((10.0 - COARSE_HALF, 10.0 + COARSE_HALF),))
        self.assertEqual(fine.lon.segments, ((10.0 - FINE_HALF, 10.0 + FINE_HALF),))

    def test_region_for_message_uses_msg_type_scale(self):
        region = region_for_message(27, 10.0, 50.0)
        self.assertAlmostEqual(region.lon.segments[0][1] - 10.0, COARSE_HALF)
        region = region_for_message(1, 10.0, 50.0)
        self.assertAlmostEqual(region.lon.segments[0][1] - 10.0, FINE_HALF)

    def test_region_for_message_rejects_non_position_types(self):
        with self.assertRaises(ValueError):
            region_for_message(5, 10.0, 50.0)

    def test_lon_cell_wraps_at_antimeridian(self):
        region = position_region(180.0, 0.0, COARSE_SCALE)
        self.assertEqual(region.lon.segments,
                         ((180.0 - COARSE_HALF, 180.0), (-180.0, -180.0 + COARSE_HALF)))

    def test_lat_cell_is_clamped_to_poles(self):
        region = position_region(0.0, 90.0, COARSE_SCALE)
        self.assertEqual(region.lat, LatCell(90.0 - COARSE_HALF, 90.0))

    def test_unavailable_sentinels_yield_no_axis(self):
        region = position_region(181.0, 91.0, COARSE_SCALE)
        self.assertIsNone(region.lon)
        self.assertIsNone(region.lat)
        region = position_region(None, None, FINE_SCALE)
        self.assertIsNone(region.lon)
        self.assertIsNone(region.lat)


class CompatibilityTestCase(unittest.TestCase):

    def test_same_position_is_compatible(self):
        a = position_region(10.0, 50.0, COARSE_SCALE)
        b = position_region(10.0, 50.0, FINE_SCALE)
        self.assertIsNone(disjoint_axis(a, b))

    def test_different_centers_with_overlapping_cells_are_compatible(self):
        # centers differ, but the fine report sits inside the coarse cell
        a = position_region(10.0, 50.0, COARSE_SCALE)
        b = position_region(10.0 + 0.9 * COARSE_HALF, 50.0 - 0.9 * COARSE_HALF, FINE_SCALE)
        self.assertIsNone(disjoint_axis(a, b))

    def test_touching_cell_boundaries_are_compatible(self):
        a = position_region(10.0, 50.0, COARSE_SCALE)
        b = position_region(10.0 + 2 * COARSE_HALF, 50.0, COARSE_SCALE)
        self.assertIsNone(disjoint_axis(a, b))

    def test_disjoint_longitude_is_a_lon_conflict(self):
        a = position_region(10.0, 50.0, COARSE_SCALE)
        b = position_region(10.0 + 4 * COARSE_HALF, 50.0, FINE_SCALE)
        self.assertIs(disjoint_axis(a, b), Axis.LON)

    def test_disjoint_latitude_is_a_lat_conflict(self):
        a = position_region(10.0, 50.0, COARSE_SCALE)
        b = position_region(10.0, 50.0 + 4 * COARSE_HALF, FINE_SCALE)
        self.assertIs(disjoint_axis(a, b), Axis.LAT)

    def test_antimeridian_cells_intersect_across_the_wrap(self):
        a = position_region(180.0 - 0.5 * COARSE_HALF, 0.0, COARSE_SCALE)
        b = position_region(-180.0 + 0.5 * COARSE_HALF, 0.0, FINE_SCALE)
        self.assertIsNone(disjoint_axis(a, b))

    def test_antimeridian_cells_can_still_conflict(self):
        a = position_region(179.9, 0.0, COARSE_SCALE)
        b = position_region(-179.0, 0.0, COARSE_SCALE)
        self.assertIs(disjoint_axis(a, b), Axis.LON)

    def test_missing_latitude_allows_only_local_judgement(self):
        # latitude unavailable on one side: only longitude can be judged
        a = position_region(10.0, 91.0, COARSE_SCALE)
        b = position_region(10.0, -50.0, FINE_SCALE)
        self.assertIsNone(disjoint_axis(a, b))
        # ...but a longitude conflict is still detected
        c = position_region(11.0, -50.0, FINE_SCALE)
        self.assertIs(disjoint_axis(a, c), Axis.LON)

    def test_no_common_axis_is_never_a_conflict(self):
        a = position_region(181.0, 91.0, COARSE_SCALE)
        b = position_region(-100.0, -80.0, FINE_SCALE)
        self.assertIsNone(disjoint_axis(a, b))


class SampleComparisonTestCase(unittest.TestCase):

    def test_conflict_requires_same_sample_id(self):
        a = sample(sample_id='a', lon=10.0)
        b = sample(sample_id='b', msg_type=1, lon=11.0)
        self.assertIsNone(compare_samples(a, b, window_seconds=60.0))

    def test_conflict_requires_same_observation_window(self):
        a = sample(timestamp=1000.0, lon=10.0)
        b = sample(timestamp=1000.0 + 3601.0, msg_type=1, lon=11.0)
        self.assertIsNone(compare_samples(a, b, window_seconds=3600.0))

    def test_conflict_reports_the_disjoint_axis(self):
        a = sample(lon=10.0, lat=50.0)
        b = sample(msg_type=1, lon=10.0 + 4 * COARSE_HALF, lat=50.0)
        self.assertIs(compare_samples(a, b, window_seconds=60.0), Axis.LON)

    def test_overlapping_cells_with_different_centers_are_not_a_conflict(self):
        a = sample(lon=10.0, lat=50.0)
        b = sample(msg_type=1, lon=10.0 + 0.5 * COARSE_HALF, lat=50.0)
        self.assertIsNone(compare_samples(a, b, window_seconds=60.0))


class FindConflictsTestCase(unittest.TestCase):

    def test_finds_only_definite_conflicts(self):
        coarse = sample(lon=10.0, lat=50.0)
        ok_overlap = sample(msg_type=1, lon=10.0 + 0.5 * COARSE_HALF, lat=50.0)
        bad_lon = sample(msg_type=1, lon=10.0 + 4 * COARSE_HALF, lat=50.0)
        conflicts = find_conflicts([coarse, ok_overlap, bad_lon], window_seconds=60.0)
        # bad_lon is disjoint from both coarse and ok_overlap on longitude;
        # the overlapping pair (coarse, ok_overlap) is not a conflict
        self.assertEqual(len(conflicts), 2)
        for conflict in conflicts:
            self.assertIsInstance(conflict, Conflict)
            self.assertIs(conflict.axis, Axis.LON)
            self.assertIn(bad_lon, (conflict.first, conflict.second))
        pairs = {(c.first, c.second) for c in conflicts}
        self.assertNotIn((coarse, ok_overlap), pairs)

    def test_samples_of_other_ids_are_ignored(self):
        a = sample(sample_id='a', lon=10.0)
        b = sample(sample_id='b', msg_type=1, lon=11.0)
        self.assertEqual(find_conflicts([a, b], window_seconds=60.0), [])

    def test_samples_outside_the_window_are_ignored(self):
        a = sample(timestamp=0.0, lon=10.0)
        b = sample(timestamp=120.0, msg_type=1, lon=11.0)
        self.assertEqual(find_conflicts([a, b], window_seconds=60.0), [])
        conflicts = find_conflicts([a, b], window_seconds=120.0)
        self.assertEqual(len(conflicts), 1)

    def test_unavailable_latitude_restricts_to_local_judgement(self):
        a = sample(lon=10.0, lat=91.0)                       # lat not available
        b = sample(msg_type=1, lon=10.0, lat=-50.0)          # wild lat, same lon
        c = sample(msg_type=1, lon=11.0, lat=-50.0)          # disjoint lon
        conflicts = find_conflicts([a, b, c], window_seconds=60.0)
        # (a, b) is compatible: a carries no latitude, so only longitude is
        # judged and the longitudes match. (a, c) and (b, c) conflict on lon.
        self.assertEqual(len(conflicts), 2)
        for conflict in conflicts:
            self.assertIs(conflict.axis, Axis.LON)
            self.assertIn(c, (conflict.first, conflict.second))


class DecodedMessageTestCase(unittest.TestCase):

    def test_real_type_27_and_type_1_messages(self):
        msg27 = decode(b"!AIVDM,1,1,,B,KC5E2b@U19PFdLbMuc5=ROv62<7m,0*16").asdict()
        self.assertEqual(msg27["msg_type"], 27)
        region = region_for_message(msg27["msg_type"], msg27["lon"], msg27["lat"])
        self.assertIsNotNone(region.lon)
        self.assertIsNotNone(region.lat)

        # a fine report at the coarse cell edge is still compatible
        fine = position_region(msg27["lon"] + COARSE_HALF, msg27["lat"], FINE_SCALE)
        self.assertIsNone(disjoint_axis(region, fine))

        # a fine report clearly outside the coarse cell conflicts on longitude
        far = position_region(msg27["lon"] + 4 * COARSE_HALF, msg27["lat"], FINE_SCALE)
        self.assertIs(disjoint_axis(region, far), Axis.LON)


if __name__ == '__main__':
    unittest.main()
