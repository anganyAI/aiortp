import os
import unittest
from typing import TypeVar, cast

T = TypeVar("T")


class TestCase(unittest.TestCase):
    def ensureIsInstance(self, obj: object, cls: type[T]) -> T:
        self.assertIsInstance(obj, cls)
        return cast(T, obj)


def load(name: str) -> bytes:
    path = os.path.join(os.path.dirname(__file__), name)
    with open(path, "rb") as fp:
        return fp.read()
