def _cross(o: tuple, a: tuple, b: tuple) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def convex_hull(
    points: list[tuple[float, float]],
) -> list[tuple[float, float]] | None:
    """Andrew's monotone chain convex hull.

    Points are (longitude, latitude) pairs (GeoJSON order).
    Returns the hull vertices in counter-clockwise order, closed (first == last),
    or None when fewer than 3 non-collinear unique points are provided.
    """
    pts = list({(round(x, 8), round(y, 8)) for x, y in points})
    if len(pts) < 3:
        return None

    pts.sort()

    lower: list[tuple] = []
    for p in pts:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)

    upper: list[tuple] = []
    for p in reversed(pts):
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)

    hull = lower[:-1] + upper[:-1]
    if len(hull) < 3:
        return None  # all points were collinear

    hull.append(hull[0])  # close the ring
    return hull


def build_territory_geojson(cat_id: int, sightings: list) -> dict | None:
    """Build a GeoJSON Feature for a cat's territory, or None if not enough points."""
    points = [
        (s.longitude, s.latitude)
        for s in sightings
        if s.latitude is not None and s.longitude is not None
    ]
    hull = convex_hull(points)
    if hull is None:
        return None
    return {
        "type": "Feature",
        "properties": {"cat_id": cat_id},
        "geometry": {
            "type": "Polygon",
            "coordinates": [hull],
        },
    }
