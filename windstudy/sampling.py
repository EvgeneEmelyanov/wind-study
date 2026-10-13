"""Отбор исходных ячеек по Соболю с фильтрацией по постоянному пулу."""
import json
import os
import tempfile
from pathlib import Path
import numpy as np
import scipy
from scipy.stats import qmc
from .pool import digest
from .geography.grid import GRID, cell_id

ALGORITHM = "sobol-grid-filter-v1"


def rectangle(pool):
    """Минимальная циклическая дуга столбцов, включая полные крайние ячейки."""
    cols = sorted({c[2] for c in pool.cells})
    if not cols:
        raise ValueError("Пул пуст")
    gaps = [(cols[(i + 1) % len(cols)] - c) % GRID['nlon'] for i, c in enumerate(cols)]
    start = cols[(int(np.argmax(gaps)) + 1) % len(cols)]
    width = max((c - start) % GRID['nlon'] for c in cols) + 1
    rows = [c[1] for c in pool.cells]
    if min(rows) == 0 or max(rows) == GRID['nlat'] - 1:
        raise ValueError("Полярные ячейки пока не поддерживаются отбором")
    return {"row_start": min(rows), "row_count": max(rows)-min(rows)+1,
            "col_start": start, "col_count": width,
            "south": GRID['lat0'] + (min(rows)-.5)*GRID['dlat'],
            "north": GRID['lat0'] + (max(rows)+.5)*GRID['dlat'],
            "west_unwrapped": GRID['lon0'] + (start-.5)*GRID['dlon'],
            "east_unwrapped": GRID['lon0'] + (start+width-.5)*GRID['dlon']}


def generate(pool, size=512, seed=42):
    if type(size) is not int or not 1 <= size <= len(pool.cells):
        raise ValueError("Размер должен быть от 1 до количества ячеек пула")
    if type(seed) is not int or not 0 <= seed <= 2**32-1:
        raise ValueError("Seed должен быть целым числом от 0 до 4294967295")
    bounds = rectangle(pool)
    lookup = {c[0]: list(c) for c in pool.cells}
    if len(lookup) != len(pool.cells):
        raise ValueError("В пуле повторяются ID")
    engine = qmc.Sobol(d=2, scramble=True, bits=30, seed=seed)
    chosen, seen = [], set()
    examined = outside = duplicates = 0
    # Последовательность читается целиком, без пропуска начальных точек.
    # Пакеты не зависят от размера выборки, обеспечивая вложенность.
    max_points = 2**24
    while len(chosen) < size and examined < max_points:
        for x, y in engine.random(1024):
            examined += 1
            col = (bounds['col_start'] + int(x*bounds['col_count'])) % GRID['nlon']
            row = bounds['row_start'] + int(y*bounds['row_count'])
            cid = cell_id(row, col)
            if cid not in lookup:
                outside += 1
            elif cid in seen:
                duplicates += 1
            else:
                chosen.append(lookup[cid])
                seen.add(cid)
            if len(chosen) == size:
                break
    if len(chosen) < size:
        raise ValueError("Достигнут предел 2^24 точек; полная выборка не получена")
    return {"schema_version": 1, "algorithm": ALGORITHM, "pool_checksum": pool.checksum,
            "pool_size": len(pool.cells), "size": size, "seed": seed,
            "parameters": {"dimensions": 2, "scramble": True, "bits": 30,
                           "coordinate_order": ["longitude", "latitude"], "rectangle": bounds,
                           "mapping": "half-open grid footprints; east/north on ties; cyclic longitude"},
            "runtime": {"numpy": np.__version__, "scipy": scipy.__version__},
            "statistics": {"examined": examined, "outside_pool": outside, "duplicates": duplicates},
            "cells": chosen}


def validate(sample, pool):
    if sample.get('schema_version') != 1 or sample.get('algorithm') != ALGORITHM:
        raise ValueError("Неподдерживаемая версия выборки")
    if sample.get('pool_checksum') != pool.checksum:
        raise ValueError("Выборка относится к другому пулу. Откройте соответствующий пул")
    cells = sample.get('cells', [])
    if len(cells) != sample.get('size') or not cells:
        raise ValueError("Некорректный размер выборки")
    lookup = {c[0]: list(c) for c in pool.cells}
    ids = [c[0] for c in cells]
    if len(set(ids)) != len(ids) or any(lookup.get(c[0]) != c for c in cells):
        raise ValueError("Повторы или ячейки, не соответствующие пулу")


def save_sample(sample, path):
    path = Path(path)
    envelope = {"sample": sample, "checksum": digest(sample)}
    fd, temporary = tempfile.mkstemp(prefix='.sample-', suffix='.json', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(envelope, stream, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_sample(path, pool):
    envelope = json.loads(Path(path).read_text(encoding='utf-8'))
    sample = envelope['sample']
    if digest(sample) != envelope.get('checksum'):
        raise ValueError("Контрольная сумма выборки не совпадает")
    validate(sample, pool)
    return sample
