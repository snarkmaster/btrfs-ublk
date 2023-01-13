# FIXME: This lacks some magic methods you'd take for granted.

from collections import abc
from types import MappingProxyType


class hashable_dict(abc.Mapping):
    def __init__(self, d):
        self._d = MappingProxyType(d)

    def __hash__(self):
        return hash(frozenset(self._d.items()))

    def __repr__(self):
        return f'hashable_dict({self._d})'

    def __format__(self, fmt):
        return self._d.__format__(fmt)

    def __str__(self):
        return self._d.__str__()

    def __eq__(self, other):
        return self._d == other._d

    def __getitem__(self, key):
        return self._d[key]

    def items(self):
        return self._d.items()

    def keys(self):
        return self._d.keys()

    def values(self):
        return self._d.values()

    def __iter__(self):
        return iter(self._d)

    def __len__(self):
        return len(self._d)

    def __bool__(self):
        return bool(self._d)


def test_hashable_dict():
    assert repr(hashable_dict({3: 5})) == 'hashable_dict({3: 5})'
    assert '{}'.format(hashable_dict({3: 5})) == '{3: 5}'
    assert str(hashable_dict({3: 5})) == '{3: 5}'
    assert hashable_dict({3: 5}) == hashable_dict({3: 5})
    assert hashable_dict({3: 5})[3] == 5
    assert {**hashable_dict({3: 5})} == {3: 5}
    assert list(hashable_dict({3: 5}).items()) == [(3, 5)]
    assert list(hashable_dict({3: 5}).keys()) == [3]
    assert list(hashable_dict({3: 5}).values()) == [5]
    assert list(hashable_dict({3: 5})) == [3]
    assert len(hashable_dict({3: 5})) == 1
    assert hashable_dict({3: 5}) and not hashable_dict({})
