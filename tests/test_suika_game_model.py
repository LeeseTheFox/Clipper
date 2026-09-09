import random

from suika_game_model import (
    BOARD_INSET,
    BOARD_WIDTH,
    DROPPABLE_FRUIT_COUNT,
    FRUIT_POINTS,
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


def test_every_fruit_size_is_visibly_larger_than_the_previous_one():
    radii = [Fruit(kind, 0, 0).radius for kind in range(len(FRUIT_POINTS))]

    assert all(
        later >= earlier * 1.18 for earlier, later in zip(radii, radii[1:], strict=False)
    )


def test_matching_fruit_merge_and_increase_the_score():
    game = FruitDropGame(random.Random(3))
    game.fruits = [Fruit(2, 120, 200), Fruit(2, 122, 200)]

    game._merge_matching_fruits()

    assert len(game.fruits) == 1
    assert game.fruits[0].kind == 3
    assert game.score == FRUIT_POINTS[3]
    assert game.fruits[0].vy == -110


def test_physics_merges_matching_fruit_when_they_touch():
    game = FruitDropGame(random.Random(3))
    game.fruits = [Fruit(1, 120, 200), Fruit(1, 121, 200)]

    game.advance(0.01)

    assert len(game.fruits) == 1
    assert game.fruits[0].kind == 2
    assert game.score == FRUIT_POINTS[2]


def test_largest_fruit_does_not_merge_further():
    game = FruitDropGame(random.Random(3))
    largest_kind = len(FRUIT_POINTS) - 1
    game.fruits = [Fruit(largest_kind, 120, 200), Fruit(largest_kind, 122, 200)]

    game._merge_matching_fruits()

    assert len(game.fruits) == 2
    assert game.score == 0
