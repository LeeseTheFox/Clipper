import random

import pytest
from suika_game_model import (
    BOARD_HEIGHT,
    BOARD_INSET,
    BOARD_WIDTH,
    DROPPABLE_FRUIT_COUNT,
    FLOOR_INSET,
    FRUIT_POINTS,
    FRUIT_RADII,
    Fruit,
    FruitDropGame,
)


def test_drop_clamps_the_fruit_inside_the_garden():
    game = FruitDropGame(random.Random(3))
    game.next_kind = 0

    assert game.drop(-100) is True

    assert game.fruits[0].x == BOARD_INSET + game.fruits[0].radius
    for _ in range(7):
        game.advance(0.05)
    assert game.drop(10_000) is True
    assert game.fruits[1].x == BOARD_WIDTH - BOARD_INSET - game.fruits[1].radius


def test_only_the_five_lowest_fruit_can_be_dropped():
    game = FruitDropGame(random.Random(3))

    assert all(game._next_kind() < DROPPABLE_FRUIT_COUNT for _ in range(100))


def test_touching_fruit_merge_and_keep_momentum_without_a_forced_hop():
    game = FruitDropGame(random.Random(3))
    game.fruits = [Fruit(1, 120, 200, vx=10, vy=80), Fruit(1, 121, 200, vx=30)]

    game.advance(0.02)

    assert len(game.fruits) == 1
    assert game.fruits[0].kind == 2
    assert game.score == FRUIT_POINTS[2]
    assert 10 < game.fruits[0].vx < 30
    assert game.fruits[0].vy > 0
    assert game.fruits[0].y > 200


def test_largest_fruit_does_not_merge_further():
    game = FruitDropGame(random.Random(3))
    largest_kind = len(FRUIT_POINTS) - 1
    game.fruits = [Fruit(largest_kind, 120, 200), Fruit(largest_kind, 122, 200)]

    game._merge_matching_fruits()

    assert len(game.fruits) == 2
    assert game.score == 0


def test_overflow_requires_five_continuous_seconds_for_the_same_fruit():
    game = FruitDropGame()
    fruit = Fruit(0, 120, 80, age=1.0)
    game.fruits = [fruit]

    # Isolate overflow timing from the physics that normally holds up a pile.
    for _ in range(49):
        game._update_game_over(0.1)
    assert fruit.danger_time > 0.0
    assert not game.game_over
    game._update_game_over(0.11)
    assert game.game_over
    assert not game.drop(160)

    game.reset()
    assert not game.game_over
    assert not game.fruits


def test_overflow_warning_clears_when_safe_and_does_not_transfer_to_other_fruit():
    game = FruitDropGame()
    first = Fruit(0, 120, 80, age=1.0)
    second = Fruit(1, 200, 200, age=1.0)
    game.fruits = [first, second]
    game._update_game_over(4.0)

    first.y = 200
    second.y = 80
    game._update_game_over(1.0)
    assert first.danger_time == 0.0
    assert second.danger_time == 1.0
    assert not game.game_over

    # Newly dropped fruit starts above the line and must be allowed to fall.
    game.fruits = [Fruit(0, 120, 48)]
    game._update_game_over(1.0)
    assert game.fruits[0].danger_time == 0.0


def test_repeated_drops_at_the_same_position_roll_off_instead_of_balancing():
    game = FruitDropGame(random.Random(3))
    game.next_kind = 4
    assert game.drop(160)
    for _ in range(100):
        game.advance(0.05)

    game.next_kind = 0
    assert game.drop(160)
    bottom, top = game.fruits
    # Placement should still agree with the pointer to within half a pixel.
    assert abs(top.x - 160) < 0.5
    assert top.x != bottom.x
    for _ in range(200):
        game.advance(0.05)

    assert abs(top.x - bottom.x) > top.radius + bottom.radius
    assert abs(top.y - (BOARD_HEIGHT - FLOOR_INSET - top.radius)) < 1.0
    assert not game.game_over


def test_merge_impulses_push_outward_fade_with_distance_and_scale_with_fruit():
    speeds = []
    for kind in (0, 4):
        game = FruitDropGame()
        radius = FRUIT_RADII[kind + 1]
        neighbor_radius = FRUIT_RADII[2]
        near = Fruit(2, 160 + radius + neighbor_radius, 220)
        farther = Fruit(2, 160, 220 - radius * 2 - neighbor_radius)
        outside = Fruit(3, 160 - radius * 3 - FRUIT_RADII[3], 220)
        game.fruits = [Fruit(kind, 159, 220), Fruit(kind, 161, 220),
                       near, farther, outside]

        game._merge_matching_fruits()

        assert near.vx > 0 and near.vy == 0
        assert farther.vy < 0 and farther.vx == 0
        assert abs(farther.vy) < near.vx
        assert outside.vx == outside.vy == 0
        # Keep even larger merges gentle compared with ordinary falling speeds.
        assert 50 < near.vx <= 180
        speeds.append(near.vx)
        velocities = [(fruit.vx, fruit.vy) for fruit in game.fruits]
        game._merge_matching_fruits()
        assert [(fruit.vx, fruit.vy) for fruit in game.fruits] == velocities

    assert speeds[0] < speeds[1]


@pytest.mark.parametrize("frames", [[1 / 30] * 60, [1 / 144] * 288,
                                   [0.003, 0.017, 0.08, 0.1] * 10, [2.0]])
def test_physics_matches_across_frame_rates_and_delayed_frames(frames):
    def make_game():
        game = FruitDropGame(random.Random(3))
        game.fruits = [Fruit(1, 120, 200), Fruit(1, 121, 200), Fruit(3, 180, 250)]
        return game

    reference = make_game()
    for _ in range(120):
        reference.advance(1 / 60)
    game = make_game()
    for elapsed in frames:
        game.advance(elapsed)

    assert game.fruits == reference.fruits
    assert game.score == reference.score
    assert game.game_over == reference.game_over


def test_simulation_tracks_real_time_and_reset_clears_pending_time():
    game = FruitDropGame()
    fruit = Fruit(0, 160, 100)
    game.fruits = [fruit]
    game.advance(0.2)
    assert fruit.age == pytest.approx(0.2)
    assert fruit.y > 100

    game.advance(0.001)
    game.reset()
    fruit = Fruit(0, 160, 100)
    game.fruits = [fruit]
    game.advance(0.008)
    assert fruit.age == 0.0


@pytest.mark.parametrize("gap, merged", [(0.25, True), (2.0, False)])
def test_resting_matching_fruit_merge_across_a_subpixel_gap_only(gap, merged):
    game = FruitDropGame()
    radius = FRUIT_RADII[6]
    floor = BOARD_HEIGHT - FLOOR_INSET - radius
    game.fruits = [Fruit(6, 80, floor), Fruit(6, 80 + 2 * radius + gap, floor)]

    game.advance(1 / 120)

    assert len(game.fruits) == (1 if merged else 2)
    assert game.score == (FRUIT_POINTS[7] if merged else 0)
    if merged:
        assert game.fruits[0].kind == 7


def test_matching_fruit_merge_when_neighbor_collisions_push_them_together():
    game = FruitDropGame()
    floor = BOARD_HEIGHT - FLOOR_INSET - FRUIT_RADII[6]
    game.fruits = [Fruit(6, 80, floor), Fruit(6, 200, floor), Fruit(5, 254, floor)]

    game.advance(1 / 120)

    assert sorted(fruit.kind for fruit in game.fruits) == [5, 7]
    assert game.score == FRUIT_POINTS[7]
