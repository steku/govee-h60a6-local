"""Local scene catalog loader and parser for Govee H60A6."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
from typing import Any

from .const import NATIVE_SCENES

_LOGGER = logging.getLogger(__name__)

SCENES_FILE = Path(__file__).parent / "scenes_h60a6.json"


@dataclass(slots=True)
class GoveeScene:
    """Represents an official or fallback Govee H60A6 hardware scene."""

    name: str
    category: str
    scene_code: int
    scene_param: str = ""


def _extract_effect_entries(
    effects: list[dict[str, Any]],
    fallback_name: str,
    category: str,
    fallback_code: Any = None,
) -> list[dict[str, Any]]:
    """Extract normalized scene dicts from a list of Govee `lightEffects`."""
    results: list[dict[str, Any]] = []
    for idx, eff in enumerate(effects):
        if not isinstance(eff, dict):
            continue
        raw_code = eff.get("sceneCode")
        if raw_code is None:
            raw_code = fallback_code
        try:
            scene_code = int(raw_code)
        except (TypeError, ValueError):
            continue

        scene_param = str(eff.get("scenceParam") or eff.get("sceneParam") or "").strip()
        if scene_code == 0 and not scene_param:
            continue

        raw_name = (
            eff.get("scenceName")
            or eff.get("sceneName")
            or eff.get("name")
            or fallback_name
        )
        name = str(raw_name).strip() or f"Scene {scene_code}"
        if len(effects) > 1 and name == fallback_name:
            name = f"{name} #{idx + 1}"

        results.append(
            {
                "name": name,
                "category": category,
                "scene_code": scene_code,
                "scene_param": scene_param,
            }
        )
    return results


def parse_govee_library_json(raw_json: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse Govee's `light-effect-libraries` JSON response into normalized scene dicts.

    Supports both structures Govee returns:
      - `data.categories[].lightEffects[]`
      - `data.categories[].scenes[].lightEffects[]`
    """
    data = raw_json.get("data")
    if not isinstance(data, dict):
        return []
    categories = data.get("categories")
    if not isinstance(categories, list):
        return []

    parsed: list[dict[str, Any]] = []
    for cat in categories:
        if not isinstance(cat, dict):
            continue
        cat_name = str(
            cat.get("categoryName")
            or cat.get("sceneName")
            or cat.get("analyticName")
            or "Scenes"
        ).strip()

        # Shape A: category itself holds `lightEffects`
        direct_effects = cat.get("lightEffects")
        if isinstance(direct_effects, list) and direct_effects:
            scene_name = str(
                cat.get("sceneName")
                or cat.get("analyticName")
                or cat_name
                or "Scene"
            ).strip()
            parsed.extend(
                _extract_effect_entries(
                    direct_effects,
                    fallback_name=scene_name,
                    category=cat_name,
                    fallback_code=cat.get("sceneCode"),
                )
            )

        # Shape B: category holds `scenes[]`, each with `lightEffects[]`
        nested_scenes = cat.get("scenes")
        if isinstance(nested_scenes, list):
            for sc in nested_scenes:
                if not isinstance(sc, dict):
                    continue
                scene_name = str(
                    sc.get("sceneName")
                    or sc.get("scenceName")
                    or sc.get("analyticName")
                    or "Scene"
                ).strip()
                effects = sc.get("lightEffects")
                if isinstance(effects, list) and effects:
                    parsed.extend(
                        _extract_effect_entries(
                            effects,
                            fallback_name=scene_name,
                            category=cat_name,
                            fallback_code=sc.get("sceneCode"),
                        )
                    )
                elif sc.get("sceneCode") is not None:
                    try:
                        code = int(sc["sceneCode"])
                    except (TypeError, ValueError):
                        continue
                    param = str(sc.get("scenceParam") or sc.get("sceneParam") or "").strip()
                    if code != 0 or param:
                        parsed.append(
                            {
                                "name": scene_name,
                                "category": cat_name,
                                "scene_code": code,
                                "scene_param": param,
                            }
                        )

    return parsed


def _build_scene_map(entries: list[dict[str, Any]]) -> dict[str, GoveeScene]:
    """Build a Home Assistant effect-name -> GoveeScene map with unique keys."""
    scenes: dict[str, GoveeScene] = {}
    for item in entries:
        base_name = str(item.get("name") or "").strip()
        category = str(item.get("category") or "Scenes").strip()
        try:
            scene_code = int(item["scene_code"])
        except (KeyError, TypeError, ValueError):
            continue
        scene_param = str(item.get("scene_param") or "").strip()
        if not base_name:
            base_name = f"#{scene_code}"

        key = base_name if base_name.startswith("Scene: ") else f"Scene: {base_name}"
        if key in scenes:
            alt = f"Scene: {base_name} ({category})"
            if alt not in scenes:
                key = alt
            else:
                suffix = 2
                while f"{alt} #{suffix}" in scenes:
                    suffix += 1
                key = f"{alt} #{suffix}"

        scenes[key] = GoveeScene(
            name=base_name,
            category=category,
            scene_code=scene_code,
            scene_param=scene_param,
        )
    return scenes


def load_h60a6_scenes(path: Path | None = None) -> dict[str, GoveeScene]:
    """Load bundled H60A6 scenes from JSON, falling back to built-in `NATIVE_SCENES`."""
    target = path or SCENES_FILE
    if target.exists():
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            entries: list[dict[str, Any]] = []
            if isinstance(raw, dict) and isinstance(raw.get("scenes"), list):
                entries = raw["scenes"]
            elif isinstance(raw, dict) and isinstance(raw.get("data"), dict):
                entries = parse_govee_library_json(raw)
            loaded = _build_scene_map(entries)
            if loaded:
                return loaded
        except Exception as err:
            _LOGGER.warning("Failed to load Govee scenes from %s: %s", target, err)

    return {
        key: GoveeScene(
            name=key.removeprefix("Scene: "),
            category="Built-in",
            scene_code=code,
            scene_param="",
        )
        for key, code in NATIVE_SCENES.items()
    }
