"""
Strategy family registry (P5.2).

  import strategies
  fam = strategies.get("smrt_v41")          # or strategies.for_cfg(cfg)
  cfg = fam.sample_cfg(rng)

A cfg names its family in cfg["family"]. A cfg without one is V4.1: every
candidate stored before P5.2 has no family key, and it must keep meaning what
it meant. candidates.family / strategies.family mirror the key for queries.
"""

from strategies.base import NON_DNA_KEYS, Strategy  # noqa: F401

DEFAULT_FAMILY = "smrt_v41"
REGISTRY = {}


def register(cls):
    inst = cls()
    if inst.name in REGISTRY:
        raise ValueError(f"strategy family {inst.name!r} registered twice")
    REGISTRY[inst.name] = inst
    return cls


def get(name):
    try:
        return REGISTRY[name]
    except KeyError:
        raise ValueError(f"unknown strategy family {name!r}; known: {sorted(REGISTRY)}") from None


def family_of(cfg):
    return cfg.get("family") or DEFAULT_FAMILY


def for_cfg(cfg):
    return get(family_of(cfg))


def names():
    return sorted(REGISTRY)


from strategies import core, smrt_v41, squeeze  # noqa: E402,F401  (each registers itself)
