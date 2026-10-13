"""Построение пула, переносимый SQLite и проверка целостности."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from dataclasses import dataclass
import numpy as np
import shapely
from shapely.geometry import mapping, shape, Polygon
from shapely.ops import unary_union
from . import __version__
from .geography.grid import GRID, centers, cell_id
from .geography.geometry import load_geometry, polygons


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass
class Pool:
    metadata: dict
    geometries: dict
    # cell_id, row, col, latitude, longitude, in_water (None means not checked)
    cells: list

    @property
    def checksum(self):
        return digest({"metadata": self.metadata, "geometries": self.geometries, "cells": self.cells})


def build(config_path):
    config_path = Path(config_path)
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    for key in ("title", "mask_date", "territorial_interpretation", "island_rule", "water_rule", "status"):
        if not cfg.get(key):
            raise ValueError(f"Не задано поле {key}")
    from datetime import date
    date.fromisoformat(cfg["mask_date"])
    if cfg["status"] not in ("draft", "approved", "synthetic_test", "research_baseline"):
        raise ValueError("Неизвестный статус маски")
    if cfg["island_rule"] not in ("intersection_with_mainland", "preprocessed_mask"):
        raise ValueError("Правило островов должно быть явным")
    if cfg["water_rule"] != "flag_only":
        raise ValueError("Согласовано только обозначение водоёмов, без исключения")
    if cfg.get("island_scope") != "marine_only":
        raise ValueError("Укажите island_scope=marine_only: исключаются только морские острова")
    if cfg.get("water_selection") != "all_features":
        raise ValueError("Укажите water_selection=all_features: отмечаются все объекты водного слоя")
    provenance, geometries, raw_sources = {}, {}, {}
    for role in ("territory", "mainland", "water"):
        spec = cfg.get(role)
        if not spec:
            continue
        for key in ("path", "source", "version", "license", "description"):
            if not spec.get(key):
                raise ValueError(f"Не задано {role}.{key}")
        path = config_path.parent / spec["path"]
        raw = path.read_bytes()
        provenance[role] = {k: v for k, v in spec.items() if k != "path"}
        provenance[role]["sha256"] = hashlib.sha256(raw).hexdigest()
        raw_sources[role] = raw.decode("utf-8")
        geometries[role] = mapping(load_geometry(path))
    if "territory" not in geometries:
        raise ValueError("Не задан слой territory")
    mask = shape(geometries["territory"])
    if cfg["island_rule"] == "intersection_with_mainland":
        if "mainland" not in geometries:
            raise ValueError("Для исключения островов необходим mainland")
        # Mainland — физическая материковая часть, не административная маска.
        # Заполняем её внутренние кольца, сохраняя озёра и внутренние острова.
        # Отверстия политической маски и водного слоя никогда не заполняем.
        mainland = unary_union([Polygon(p.exterior) for p in polygons(shape(geometries["mainland"]))])
        geometries["mainland_envelope"] = mapping(mainland)
        mask = mask.intersection(mainland)
    elif not cfg.get("preprocessing_description"):
        raise ValueError("Для готовой маски опишите выполненное исключение островов")
    if mask.is_empty or not mask.is_valid or mask.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError("Итоговая маска пуста или некорректна")
    geometries["mask"] = mapping(mask)
    rows, cols, lats, lons = centers()
    pts = shapely.points(lons % 360, lats)
    shapely.prepare(mask)
    keep = shapely.covers(mask, pts)  # Включает границу и границы внутренних колец.
    water = shape(geometries["water"]) if "water" in geometries else None
    flags = shapely.covers(water, pts[keep]).tolist() if water is not None else [None] * int(keep.sum())
    cells = [[cell_id(int(r), int(c)), int(r), int(c), float(lat), float(lon), flag]
             for r, c, lat, lon, flag in zip(rows[keep], cols[keep], lats[keep], lons[keep], flags)]
    if not cells:
        raise ValueError("В маску не попал ни один центр ячейки")
    metadata = {
        "schema_version": 1, "algorithm_version": __version__, "grid": GRID,
        "config": {k: v for k, v in cfg.items() if k not in ("territory", "mainland", "water")},
        "sources": provenance, "raw_sources": raw_sources,
        "membership": "center_covered_by_mask", "longitude_domain": "0..360",
        "water_status": "checked_against_supplied_layer" if water is not None else "not_checked",
        "runtime": {"numpy": np.__version__, "shapely": shapely.__version__, "geos": shapely.geos_version_string},
    }
    return Pool(metadata, geometries, cells)


def save(pool, path):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=".pool-", suffix=".sqlite", dir=path.parent)
    os.close(fd)
    try:
        with sqlite3.connect(tmp) as db:
            db.executescript('''
                PRAGMA user_version=1;
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE cells (
                    cell_id TEXT PRIMARY KEY, row INTEGER NOT NULL, col INTEGER NOT NULL,
                    latitude REAL NOT NULL, longitude REAL NOT NULL,
                    in_water INTEGER CHECK(in_water IN (0,1) OR in_water IS NULL), UNIQUE(row,col));
            ''')
            db.executemany("INSERT INTO metadata VALUES (?,?)", [
                ("metadata", canonical(pool.metadata)), ("geometries", canonical(pool.geometries)),
                ("checksum", pool.checksum)])
            db.executemany("INSERT INTO cells VALUES (?,?,?,?,?,?)", pool.cells)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def load(path):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ValueError("Неподдерживаемая версия файла пула")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Повреждение SQLite")
        values = dict(db.execute("SELECT key,value FROM metadata"))
        cells = [list(r) for r in db.execute("SELECT * FROM cells ORDER BY row,col")]
    for c in cells:
        c[5] = None if c[5] is None else bool(c[5])
        if c[0] != cell_id(c[1], c[2]) or c[3] != -90 + c[1] * .5 or c[4] != -180 + c[2] * .625:
            raise ValueError("Идентификатор или координаты не соответствуют сетке")
    pool = Pool(json.loads(values["metadata"]), json.loads(values["geometries"]), cells)
    if pool.metadata["schema_version"] != 1 or pool.metadata["grid"] != GRID:
        raise ValueError("Неподдерживаемая сетка или схема")
    if pool.checksum != values["checksum"]:
        raise ValueError("Контрольная сумма не совпадает: файл изменён")
    return pool


def rebuild(pool):
    """Повторное построение из исходников, встроенных в сохранённый пул."""
    with tempfile.TemporaryDirectory(prefix="wind-rebuild-") as folder:
        folder = Path(folder)
        cfg = dict(pool.metadata["config"])
        for role, raw in pool.metadata["raw_sources"].items():
            data = raw.encode("utf-8")
            spec = dict(pool.metadata["sources"][role])
            if hashlib.sha256(data).hexdigest() != spec.pop("sha256"):
                raise ValueError(f"Повреждён исходный слой {role}")
            filename = f"{role}.geojson"
            (folder / filename).write_bytes(data)
            spec["path"] = filename
            cfg[role] = spec
        config_path = folder / "config.json"
        config_path.write_text(canonical(cfg), encoding="utf-8")
        return build(config_path)
