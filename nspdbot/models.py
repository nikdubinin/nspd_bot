"""Разбор «сырого» ответа НСПД в удобную структуру.

Все обращения к полям ответа НСПД собраны здесь, чтобы форматирование
и генерация текстов не зависели от внутренней схемы API.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .cadastral import quarter_of


class ObjectKind(str, Enum):
    LAND = "land"
    BUILDING = "building"
    STRUCTURE = "structure"
    UNFINISHED = "unfinished"
    PREMISES = "premises"
    PARKING = "parking"
    OTHER = "other"

    @property
    def is_oks(self) -> bool:
        """Объект капитального строительства (у него есть связанные ЗУ)."""
        return self in (ObjectKind.BUILDING, ObjectKind.STRUCTURE, ObjectKind.UNFINISHED)


# categoryId слоёв НСПД
_CATEGORY_KINDS: dict[int, ObjectKind] = {
    36368: ObjectKind.LAND,
    36369: ObjectKind.BUILDING,
    36383: ObjectKind.STRUCTURE,
    36384: ObjectKind.UNFINISHED,
    37163: ObjectKind.PREMISES,
    37159: ObjectKind.PARKING,
}

_KIND_LABELS: dict[ObjectKind, str] = {
    ObjectKind.LAND: "Земельный участок",
    ObjectKind.BUILDING: "Здание",
    ObjectKind.STRUCTURE: "Сооружение",
    ObjectKind.UNFINISHED: "Объект незавершённого строительства",
    ObjectKind.PREMISES: "Помещение",
    ObjectKind.PARKING: "Машино-место",
    ObjectKind.OTHER: "Объект недвижимости",
}

_EARTH_RADIUS = 6378137.0


def _clean(value: Any) -> Any:
    """Пустые значения НСПД ('', None, [], 'None') -> None; строки обрезаются."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value if value and value.lower() not in {"none", "null"} else None
    if isinstance(value, (list, tuple)):
        items = [v for v in (_clean(x) for x in value) if v is not None]
        return items or None
    return value


def _first(options: dict, *keys: str) -> Any:
    for key in keys:
        value = _clean(options.get(key))
        if value is not None:
            return value
    return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _floor_text(value: Any) -> str | None:
    """['4/Этаж'] / '-1/Этаж' -> '4' / '-1'."""
    value = _clean(value)
    if value is None:
        return None
    items = value if isinstance(value, list) else [value]
    floors = [str(item).split("/", 1)[0].strip() for item in items]
    return ", ".join(f for f in floors if f) or None


def _iter_coords(coords: Any):
    if isinstance(coords, (list, tuple)):
        if len(coords) >= 2 and all(isinstance(c, (int, float)) for c in coords[:2]):
            yield float(coords[0]), float(coords[1])
        else:
            for item in coords:
                yield from _iter_coords(item)


def _centroid_wgs84(geometry: Any) -> tuple[float, float] | None:
    """Центр габаритного прямоугольника геометрии -> (широта, долгота)."""
    if not isinstance(geometry, dict):
        return None
    points = list(_iter_coords(geometry.get("coordinates")))
    if not points:
        return None
    xs, ys = zip(*points)
    x = (min(xs) + max(xs)) / 2
    y = (min(ys) + max(ys)) / 2

    crs_name = str(((geometry.get("crs") or {}).get("properties") or {}).get("name", ""))
    if "4326" in crs_name:
        return y, x
    # По умолчанию НСПД отдаёт EPSG:3857 (Web Mercator)
    lon = math.degrees(x / _EARTH_RADIUS)
    lat = math.degrees(2 * math.atan(math.exp(y / _EARTH_RADIUS)) - math.pi / 2)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return round(lat, 6), round(lon, 6)


@dataclass(slots=True)
class NspdObject:
    cad_number: str
    kind: ObjectKind
    category_name: str | None
    options: dict
    geom_id: int | None
    updated_at: str | None
    centroid: tuple[float, float] | None
    raw: dict

    # ------------------------------------------------------------------ factory

    @classmethod
    def from_response(cls, raw: dict, requested_number: str) -> NspdObject:
        """Строит объект из ответа NSPD.search_by_cadastral_number / get_object_info."""
        features = ((raw.get("data") or {}).get("features")) or []
        feature = cls._pick_feature(features, requested_number)
        props = feature.get("properties") or {}
        options = props.get("options") or {}
        system_info = props.get("systemInfo") or {}

        category_id = props.get("category")
        if category_id is None:
            meta = raw.get("meta") or []
            category_id = meta[0].get("categoryId") if meta and isinstance(meta[0], dict) else None
        try:
            kind = _CATEGORY_KINDS.get(int(category_id), ObjectKind.OTHER)
        except (TypeError, ValueError):
            kind = ObjectKind.OTHER

        cad_number = _first(options, "cad_num", "cad_number") or _clean(props.get("label")) or requested_number

        return cls(
            cad_number=str(cad_number),
            kind=kind,
            category_name=_clean(props.get("categoryName")),
            options=options,
            geom_id=feature.get("id") or raw.get("geom_id"),
            updated_at=_clean(system_info.get("updated")) or _clean(system_info.get("inserted")),
            centroid=_centroid_wgs84(feature.get("geometry")),
            raw=raw,
        )

    @staticmethod
    def _pick_feature(features: list, requested_number: str) -> dict:
        """Поиск НСПД может вернуть несколько объектов — берём точное совпадение."""
        for feature in features:
            props = feature.get("properties") or {}
            options = props.get("options") or {}
            if requested_number in (
                options.get("cad_num"),
                options.get("cad_number"),
                props.get("label"),
                props.get("externalKey"),
            ):
                return feature
        return features[0] if features else {}

    # ------------------------------------------------------------------ fields

    def opt(self, *keys: str) -> Any:
        return _first(self.options, *keys)

    @property
    def subtype(self) -> str | None:
        """Уточнение вида: 'Квартира', 'Многоквартирный дом' и т.п."""
        return self.opt("params_type", "build_record_type_value", "land_record_subtype")

    @property
    def type_label(self) -> str:
        base = _KIND_LABELS[self.kind]
        if self.kind is ObjectKind.OTHER and self.category_name:
            return self.category_name
        if self.kind is ObjectKind.PREMISES and self.opt("params_type"):
            return f"{base} ({self.opt('params_type').lower()})"
        return base

    @property
    def name(self) -> str | None:
        return self.opt("building_name", "name")

    @property
    def address(self) -> str | None:
        return self.opt("readable_address", "address")

    @property
    def area(self) -> float | None:
        return _to_float(
            self.opt("build_record_area", "specified_area", "land_record_area", "area", "declared_area")
        )

    @property
    def purpose(self) -> str | None:
        return self.opt("purpose", "permitted_use_name")

    @property
    def permitted_use(self) -> str | None:
        return self.opt("permitted_use_established_by_document")

    @property
    def land_category(self) -> str | None:
        return self.opt("land_record_category_type")

    @property
    def right_type(self) -> str | None:
        return self.opt("right_type")

    @property
    def status(self) -> str | None:
        return self.opt("status", "common_data_status", "previously_posted")

    @property
    def ownership_type(self) -> str | None:
        return self.opt("ownership_type")

    @property
    def floors(self) -> str | None:
        return self.opt("floors")

    @property
    def underground_floors(self) -> str | None:
        return self.opt("underground_floors")

    @property
    def floor(self) -> str | None:
        return _floor_text(self.options.get("floor"))

    @property
    def materials(self) -> str | None:
        return self.opt("materials")

    @property
    def year_built(self) -> str | None:
        return self.opt("year_built")

    @property
    def year_commissioning(self) -> str | None:
        return self.opt("year_commisioning", "year_commissioning")

    @property
    def cost_value(self) -> float | None:
        return _to_float(self.opt("cost_value"))

    @property
    def cost_index(self) -> float | None:
        return _to_float(self.opt("cost_index"))

    @property
    def cost_determination_date(self) -> str | None:
        return self.opt("cost_determination_date")

    @property
    def cost_application_date(self) -> str | None:
        return self.opt("cost_application_date")

    @property
    def cost_registration_date(self) -> str | None:
        return self.opt("cost_registration_date")

    @property
    def registration_date(self) -> str | None:
        return self.opt("land_record_reg_date", "build_record_registration_date", "registration_date")

    @property
    def quarter(self) -> str:
        return self.opt("quarter_cad_number") or quarter_of(self.cad_number)

    @property
    def parent_cad_number(self) -> str | None:
        return self.opt("parent_cad_number")

    @property
    def facility_cad_number(self) -> str | None:
        return self.opt("facility_cad_number")

    @property
    def united_cad_numbers(self) -> Any:
        return self.opt("united_cad_numbers")

    @property
    def intersected_cad_numbers(self) -> Any:
        return self.opt("intersected_cad_numbers")

    @property
    def cultural_heritage(self) -> str | None:
        return self.opt("cultural_heritage_val")

    @property
    def has_related_lookup(self) -> bool:
        """Можно ли запросить связанные объекты (ЗУ <-> ОКС) через API НСПД."""
        return self.geom_id is not None and (self.kind is ObjectKind.LAND or self.kind.is_oks)

    # ------------------------------------------------------------------ texts

    def noun_phrase(self) -> tuple[str, str]:
        """(существительное, причастие) для текстов предложений инвесторам."""
        sub = (self.subtype or "").lower()
        purpose = (self.purpose or "").lower()

        match self.kind:
            case ObjectKind.PREMISES:
                if "квартир" in sub:
                    return "квартира", "расположенная"
                if "комнат" in sub:
                    return "комната", "расположенная"
                if "нежил" in purpose:
                    return "нежилое помещение", "расположенное"
                return "помещение", "расположенное"
            case ObjectKind.BUILDING:
                if "многоквартир" in purpose:
                    return "многоквартирный дом", "расположенный"
                if "жил" in purpose and "нежил" not in purpose:
                    return "жилой дом", "расположенный"
                if "нежил" in purpose:
                    return "нежилое здание", "расположенное"
                return "здание", "расположенное"
            case ObjectKind.LAND:
                return "земельный участок", "расположенный"
            case ObjectKind.STRUCTURE:
                return "сооружение", "расположенное"
            case ObjectKind.UNFINISHED:
                return "объект незавершённого строительства", "расположенный"
            case ObjectKind.PARKING:
                return "машино-место", "расположенное"
        return "объект недвижимости", "расположенный"
