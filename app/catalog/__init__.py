"""Immutable import data, never consulted by the inference resolver.

Administrators own the rows in PostgreSQL after bootstrap. Re-running a seed only
inserts missing rows; it never resets an enable switch, permission, limit or route.
"""

import json
from importlib.resources import files


def load_bootstrap_catalog():
    data = json.loads(files(__package__).joinpath("cloud_bot_v7.json").read_text("utf-8"))
    models = data["models"]
    if len(models) != 42 or sum(m["is_public"] for m in models) != 40:
        raise ValueError("Invalid Cloud Bot v7 catalog counts")
    if len({m["model_key"] for m in models}) != 42:
        raise ValueError("Duplicate model keys")
    if len({m["model_id"] for m in models}) != 42:
        raise ValueError("Duplicate model IDs")
    return models


def bootstrap_values(model):
    return {
        "alias": model["model_id"],
        "upstream_model": model["model_id"],
        "provider": model["backend"],
        "provider_transport": "mantle_chat",
        "model_key": model["model_key"],
        "display_name": model["display_name"],
        "manufacturer": model["manufacturer"],
        "region": model["region"],
        "api_route": model["api_route"],
        "max_context_tokens": model["context_window"],
        "max_output_tokens": model["max_output"],
        "model_type": model["model_type"],
        "is_public": model["is_public"],
        "external_api_allowed": model["is_public"],
        "enabled": True,
        "supports_vision": model["supports_vision"],
        "supports_reasoning": model["supports_reasoning"],
        "reasoning_efforts": model["reasoning_efforts"],
        "vision_behavior": model["vision_behavior"],
        "catalog_metadata": model["metadata"],
        # Export has cost tiers, not USD tariffs. Zero leaves the optional cost
        # ledger unpriced; it does NOT imply free AWS inference.
        "input_price": 0,
        "output_price": 0,
        "capabilities": {
            "text_input": True,
            "text_output": True,
            "system_prompt": True,
            "temperature": False,
            "vision": model["supports_vision"],
            "image_input": model["supports_vision"],
            "streaming": False,
            "tools": False,
            "reasoning": False,
            "reasoning_efforts": [],
        },
    }
