"""Small, dependency-free physics model for Clipper's fruit-drop easter egg."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

BOARD_WIDTH = 320.0
BOARD_HEIGHT = 440.0
BOARD_INSET = 14.0
FLOOR_INSET = 18.0
SPAWN_Y = 48.0
DANGER_LINE_Y = 82.0
GRAVITY = 1_150.0
MAX_FALL_SPEED = 1_050.0
DROP_COOLDOWN_SECONDS = 0.32
GAME_OVER_DELAY_SECONDS = 1.6

# The deliberately compact set of eleven growing fruit is enough for the
# familiar Suika-style progression without making the little board too busy.
# Every step is visibly larger than the last, even at the small end.
FRUIT_RADII = (10.0, 14.0, 19.0, 25.0, 32.0, 41.0, 52.0, 65.0, 80.0, 98.0, 120.0)
FRUIT_POINTS = (1, 3, 6, 10, 15, 21, 28, 36, 45, 55, 66)
# In the original game, the five lowest tiers are equiprobable drops. Larger
# fruit can only enter the board through merges.
DROPPABLE_FRUIT_COUNT = 5


@dataclass
class Fruit:
    """One circular fruit in the playfield."""

    kind: int
    x: float
    y: float
    vx: float = 0.0
    vy: float = 0.0
    age: float = 0.0

    @property
    def radius(self) -> float:
        return FRUIT_RADII[self.kind]


class FruitDropGame:
    """A lightweight fixed-step, circle-based fruit-drop game."""

    def __init__(self, random_source: random.Random | None = None) -> None:
        self._random = random_source if random_source is not None else random.Random()
        self.reset()

    def reset(self) -> None:
        """Start a fresh board while retaining the random source."""
        self.fruits: list[Fruit] = []
        self.score = 0
        self.next_kind = self._next_kind()
        self.drop_cooldown = 0.0
        self.danger_time = 0.0
        self.game_over = False

    @property
    def can_drop(self) -> bool:
        return not self.game_over and self.drop_cooldown <= 0.0

    def _next_kind(self) -> int:
        return self._random.randrange(DROPPABLE_FRUIT_COUNT)

    def drop(self, x: float) -> bool:
        """Release the queued fruit at *x*, returning whether it was accepted."""
        if not self.can_drop:
            return False

        radius = FRUIT_RADII[self.next_kind]
        clamped_x = min(BOARD_WIDTH - BOARD_INSET - radius, max(BOARD_INSET + radius, x))
        self.fruits.append(Fruit(self.next_kind, clamped_x, SPAWN_Y))
        self.next_kind = self._next_kind()
        self.drop_cooldown = DROP_COOLDOWN_SECONDS
        return True

    def advance(self, elapsed: float) -> None:
        """Advance the simulation by at most one interactive frame's worth of time."""
        if self.game_over:
            return

        remaining = min(max(0.0, elapsed), 0.05)
        while remaining > 0.0:
            step = min(remaining, 1.0 / 120.0)
            self._advance_step(step)
            remaining -= step

    def _advance_step(self, elapsed: float) -> None:
        self.drop_cooldown = max(0.0, self.drop_cooldown - elapsed)
        for fruit in self.fruits:
            fruit.age += elapsed
            fruit.vy = min(MAX_FALL_SPEED, fruit.vy + GRAVITY * elapsed)
            fruit.x += fruit.vx * elapsed
            fruit.y += fruit.vy * elapsed
            fruit.vx *= 0.996
            self._constrain_to_board(fruit)

        # Merge before separating colliding circles: a matching pair should
        # combine as soon as it touches, rather than being bounced apart.
        self._merge_matching_fruits()
        # Two cheap solver passes are enough for this tiny pile while keeping
        # the game comfortable on low-powered hardware.
        for _pass in range(2):
            self._resolve_collisions()
        self._update_game_over(elapsed)

    @staticmethod
    def _next_velocity_after_bounce(velocity: float) -> float:
        return -velocity * 0.28 if velocity > 0.0 else velocity

    def _constrain_to_board(self, fruit: Fruit) -> None:
        radius = fruit.radius
        left = BOARD_INSET + radius
        right = BOARD_WIDTH - BOARD_INSET - radius
        floor = BOARD_HEIGHT - FLOOR_INSET - radius
        if fruit.x < left:
            fruit.x = left
            fruit.vx = abs(fruit.vx) * 0.28
        elif fruit.x > right:
            fruit.x = right
            fruit.vx = -abs(fruit.vx) * 0.28
        if fruit.y > floor:
            fruit.y = floor
            fruit.vy = self._next_velocity_after_bounce(fruit.vy)
            fruit.vx *= 0.88

    def _resolve_collisions(self) -> None:
        for index, first in enumerate(self.fruits):
            for second in self.fruits[index + 1 :]:
                delta_x = second.x - first.x
                delta_y = second.y - first.y
                distance_squared = delta_x * delta_x + delta_y * delta_y
                minimum_distance = first.radius + second.radius
                if distance_squared >= minimum_distance * minimum_distance:
                    continue

                distance = math.sqrt(distance_squared)
                if distance <= 0.001:
                    normal_x, normal_y = 1.0, 0.0
                    distance = 0.001
                else:
                    normal_x, normal_y = delta_x / distance, delta_y / distance

                overlap = minimum_distance - distance
                first.x -= normal_x * overlap / 2
                first.y -= normal_y * overlap / 2
                second.x += normal_x * overlap / 2
                second.y += normal_y * overlap / 2
                self._constrain_to_board(first)
                self._constrain_to_board(second)

                relative_velocity = (second.vx - first.vx) * normal_x + (
                    second.vy - first.vy
                ) * normal_y
                if relative_velocity < 0.0:
                    impulse = -relative_velocity * 0.46
                    first.vx -= impulse * normal_x
                    first.vy -= impulse * normal_y
                    second.vx += impulse * normal_x
                    second.vy += impulse * normal_y

    def _merge_matching_fruits(self) -> None:
        consumed: set[int] = set()
        merged: list[Fruit] = []
        for index, first in enumerate(self.fruits):
            if index in consumed or first.kind >= len(FRUIT_RADII) - 1:
                continue
            for other_index, second in enumerate(self.fruits[index + 1 :], index + 1):
                if other_index in consumed or first.kind != second.kind:
                    continue
                distance = math.hypot(second.x - first.x, second.y - first.y)
                if distance > first.radius + second.radius:
                    continue
                consumed.update((index, other_index))
                next_kind = first.kind + 1
                merged.append(
                    Fruit(
                        next_kind,
                        (first.x + second.x) / 2,
                        (first.y + second.y) / 2,
                        (first.vx + second.vx) / 2,
                        min(-110.0, (first.vy + second.vy) / 2),
                    )
                )
                self.score += FRUIT_POINTS[next_kind]
                break

        if consumed:
            self.fruits = [
                fruit for index, fruit in enumerate(self.fruits) if index not in consumed
            ] + merged

    def _update_game_over(self, elapsed: float) -> None:
        danger = any(
            fruit.age >= 0.7 and fruit.y - fruit.radius < DANGER_LINE_Y
            for fruit in self.fruits
        )
        self.danger_time = self.danger_time + elapsed if danger else 0.0
        if self.danger_time >= GAME_OVER_DELAY_SECONDS:
            self.game_over = True
