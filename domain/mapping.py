"""Vinted label ↔ Wallapop attribute key resolution.

Pure dictionaries + lookup helpers — no I/O, no module-level state. The full
dynamic-form fill loop in ``upload_vinted.py`` consumes these to map each
visible Vinted dropdown to an item attribute and pick a value for it.

Marca / Talla / Color / Estado are intentionally hard-wired in
:data:`LABEL_ALIASES`: Vinted requires them on every listing regardless of
category, so we always want them to resolve — even when the item itself
doesn't carry that attribute. Auto-learning these per category would just
duplicate the rule into every category's mapping block.
"""

from .text import normalize_label


# Wallapop attribute keys keyed by normalized Vinted label. Seeded with common cases;
# the auto-learn loop extends this implicitly by writing resolved mappings to
# category_mapping.json, so this table only needs the first-time guess.
LABEL_ALIASES: dict[str, list[str]] = {
    # These three are always present on Vinted — hard-wire them so they never need
    # auto-learning from a specific item (the item may lack the attribute).
    "marca": ["brand"],
    "talla": ["size"],
    "color": ["color", "colour"],
    "estado": ["condition"],
    "sistema operativo": ["operating_system", "os"],
    "capacidad de almacenamiento": ["storage_capacity", "storage"],
    "ram": ["ram", "memory"],
    "procesador": ["processor", "cpu"],
    "autor": ["author"],
    "editorial": ["publisher"],
    "idioma": ["language"],
    "formato": ["format"],
    "plataforma": ["platform"],
    "género": ["genre"],
    "material": ["material", "composition"],
    "modelo": ["model"],
}


# Wallapop's API returns color values as English canonical keys ('orange', 'black', …)
# even though its UI shows them in Spanish. Vinted's dropdown lists them in Spanish.
COLOR_EN_TO_ES: dict[str, str] = {
    "black": "Negro",
    "white": "Blanco",
    "gray": "Gris",
    "grey": "Gris",
    "silver": "Plateado",
    "red": "Rojo",
    "blue": "Azul",
    "navy": "Azul marino",
    "light_blue": "Azul claro",
    "turquoise": "Turquesa",
    "green": "Verde",
    "dark_green": "Verde oscuro",
    "mint": "Menta",
    "yellow": "Amarillo",
    "mustard": "Mostaza",
    "orange": "Naranja",
    "coral": "Coral",
    "pink": "Rosa",
    "fuchsia": "Fucsia",
    "purple": "Morado",
    "lilac": "Lila",
    "brown": "Marrón",
    "beige": "Beige",
    "cream": "Crema",
    "khaki": "Caqui",
    "burgundy": "Burdeos",
    "multicolor": "Multicolor",
    "multicolour": "Multicolor",
    "gold": "Dorado",
}


def guess_wallapop_key(label: str, attributes: dict) -> str | None:
    """Best-effort mapping Vinted label → key in item.attributes. Returns None if no match."""
    norm = normalize_label(label)
    # 1) Direct match against the label itself
    for k in attributes:
        if normalize_label(k) == norm:
            return k
    # 2) Alias table
    for k in LABEL_ALIASES.get(norm, []):
        if k in attributes:
            return k
    # 3) Substring fallback: Wallapop key is contained in (or contains) the normalized label
    for k in attributes:
        nk = normalize_label(k)
        if nk and (nk in norm or norm in nk):
            return k
    return None


def label_to_wallapop_key(label: str) -> str | None:
    """Resolve a Vinted field label to its canonical Wallapop key using only the alias table.

    Unlike :func:`guess_wallapop_key`, this doesn't need a concrete item — it
    resolves well-known label→key pairs (e.g. "Color"→"color") regardless of
    whether the current item has that attribute. Used to fix
    ``category_mapping`` entries that were saved with ``from: null`` because
    the first item that triggered them lacked the attribute.
    """
    norm = normalize_label(label)
    # Direct alias lookup (covers color, brand, size, etc.)
    aliases = LABEL_ALIASES.get(norm)
    if aliases:
        return aliases[0]
    # Normalized label itself matches a known Wallapop key name
    if norm in {normalize_label(k) for keys in LABEL_ALIASES.values() for k in keys}:
        return norm
    return None
