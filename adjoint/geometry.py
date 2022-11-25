import numpy as np


class Vector3:
    def __init__(self, x=None, y=None, z=None):
        self._x = x
        self._y = y
        self._z = z

        self._non_none = [str(i) for i in [x, y, z] if i is not None]
        self._dimensions = len(self._non_none)

    def __str__(self) -> str:
        s = f"{self._dimensions}-dimensional vector: ({','.join(self._non_none)})"
        return s

    def __repr__(self) -> str:
        return f"Vector({','.join(self._non_none)})"

    @property
    def x(self):
        return self._x

    @property
    def y(self):
        return self._y

    @property
    def z(self):
        return self._z


def calculate_3d_normal(p0, p1, p2):
    """Calculates the normal vector of a plane defined by 3 points."""
    n = np.cross(
        np.array([p1.x - p0.x, p1.y - p0.y, p1.z - p0.z]),
        np.array([p2.x - p0.x, p2.y - p0.y, p2.z - p0.z]),
    )
    n = n / np.sqrt(np.sum(n**2))
    return n
