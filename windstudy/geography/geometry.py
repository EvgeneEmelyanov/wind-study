"""GeoJSON WGS84; рабочая долгота 0..360 для территории России.

Входной переход 180° можно задавать разрезанными полигонами либо кольцом
с коротким ребром через антимеридиан. Полигон, пересекающий 0°, не поддержан:
он явно отклоняется, а не интерпретируется как почти вся Земля.
"""
import json
from pathlib import Path
import numpy as np
from shapely.geometry import shape, Polygon, MultiPolygon
from shapely.ops import unary_union
from shapely.validation import explain_validity


def _ring(coords):
    out = []
    for coord in coords:
        if len(coord) != 2:
            raise ValueError("Требуются двумерные координаты lon, lat")
        lon, lat = coord
        if not np.isfinite([lon, lat]).all() or not -180 <= lon <= 180 or not -90 <= lat <= 90:
            raise ValueError("Ожидаются координаты WGS84 в градусах")
        out.append((lon % 360, lat))
    if len(out) < 4 or out[0] != out[-1]:
        raise ValueError("Кольцо GeoJSON должно быть замкнуто")
    if any(abs(a[0] - b[0]) > 180 for a, b in zip(out, out[1:])):
        raise ValueError("Геометрия пересекает рабочий шов 0°; требуется другой домен")
    return out


def normalize_geometry(obj):
    if obj["type"] == "Polygon":
        rings = obj["coordinates"]
        geom = Polygon(_ring(rings[0]), [_ring(r) for r in rings[1:]])
    elif obj["type"] == "MultiPolygon":
        geom = unary_union([normalize_geometry({"type": "Polygon", "coordinates": p})
                            for p in obj["coordinates"]])
    else:
        raise ValueError("Поддерживаются только Polygon и MultiPolygon")
    if geom.is_empty or not geom.is_valid:
        raise ValueError(f"Недопустимая геометрия: {explain_validity(geom)}")
    return geom


def load_geometry(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "crs" in data:
        raise ValueError("GeoJSON должен соответствовать RFC 7946 без поля crs")
    if data["type"] == "FeatureCollection":
        geometries = [f["geometry"] for f in data["features"]]
    elif data["type"] == "Feature":
        geometries = [data["geometry"]]
    else:
        geometries = [data]
    if not geometries:
        raise ValueError("Пустой слой")
    return unary_union([normalize_geometry(g) for g in geometries])


def polygons(geom):
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    return [p for g in geom.geoms for p in polygons(g)] if hasattr(geom, "geoms") else []
