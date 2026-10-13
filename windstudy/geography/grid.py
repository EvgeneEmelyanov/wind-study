"""Индексы MERRA-2: нумерация с нуля, долгота циклическая."""
import math
import numpy as np

GRID = {
    "id": "merra2-native-v1", "crs": "EPSG:4326", "index_base": 0,
    "nlat": 361, "nlon": 576, "lat0": -90.0, "lon0": -180.0,
    "dlat": 0.5, "dlon": 0.625,
    "source": "https://gmao.gsfc.nasa.gov/pubs/docs/Bosilovich785.pdf",
    "source_version": "GMAO Office Note 9, v1.1, 2016-03-21",
    "power_source": "https://power.larc.nasa.gov/docs/methodology/meteorology/",
    "power_coordinate_mapping": "pending_verification; request centers only",
}

def centers():
    row, col = np.indices((GRID["nlat"], GRID["nlon"]))
    return (row.ravel(), col.ravel(),
            (GRID["lat0"] + row * GRID["dlat"]).ravel(),
            (GRID["lon0"] + col * GRID["dlon"]).ravel())

def cell_id(row: int, col: int) -> str:
    if not (0 <= row < 361 and 0 <= col < 576):
        raise ValueError("Индексы вне сетки MERRA-2")
    return f"M2:r{row:03d}:c{col:03d}"

def nearest_cell(lat: float, lon: float):
    """Геометрическое правило приложения, НЕ утверждение о правилах POWER API.

    Равноудалённая точка относится к северному/восточному центру.
    До независимой проверки API этот метод нельзя использовать для загрузки.
    """
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90:
        raise ValueError("Некорректные координаты")
    row = min(360, int(math.floor((lat + 90) / 0.5 + 0.5)))
    col = int(math.floor(((lon + 180) % 360) / 0.625 + 0.5)) % 576
    return row, col
