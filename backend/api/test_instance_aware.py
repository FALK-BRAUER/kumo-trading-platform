"""The platform must be configurable by an instance, and must behave EXACTLY as today when it is not.

Every assertion here has the same shape: the override works, AND the default is unchanged. The second
half is what makes this landable before the instances repo exists — and it is the half that would be
quietly dropped, because an override that works looks finished.

kumo-cockpit#486, steps 3/4/8.
"""

from __future__ import annotations

import os
from pathlib import Path


def test_feed_config_path_defaults_to_the_platforms_own(monkeypatch):
    """Unset means today. The platform still runs standalone; nothing has to move first."""
    monkeypatch.delenv("KUMO_FEED_CONFIG", raising=False)
    from api.feed_config import _feed_config_path

    got = _feed_config_path()
    assert got.name == "feed.toml" and got.parent.name == "config", got
    assert got.exists(), "the platform's own feed.toml is not where it was"


def test_an_instance_can_own_feed_toml(monkeypatch, tmp_path):
    """`feed.toml` selects providers, trader id, venue and the traded UNIVERSE.

    All four are properties of a deployment, not of the software. Committing them to the platform is
    what made this repo un-runnable by anyone else.
    """
    other = tmp_path / "instance-feed.toml"
    other.write_text("")
    monkeypatch.setenv("KUMO_FEED_CONFIG", str(other))
    from api.feed_config import _feed_config_path

    assert _feed_config_path() == other


def test_cors_defaults_to_todays_wildcard(monkeypatch):
    """The comment has said "tighten before any non-paper use" for months while the value sat in
    source, where tightening it means a rebuild. Moving it to config must not change it."""
    from api.app import _CORS_ORIGINS

    # THE MODULE VALUE, not the helper with a literal argument. The first version of this test read
    # `_parse_cors("*")` and passed while the production default was changed to a narrow list — a
    # mutation escaped it. Asserting the parser against a constant you supplied yourself tests the
    # parser, never the default it is given.
    assert "KUMO_CORS_ORIGINS" not in os.environ, (
        "this test asserts the DEFAULT, so it is only meaningful with the variable unset"
    )
    assert _CORS_ORIGINS == ["*"], (
        f"the default CORS policy changed to {_CORS_ORIGINS} — moving a value from source to config "
        f"must not change it, or the migration is a behaviour change wearing a refactor's clothes"
    )


def test_an_instance_can_narrow_cors(monkeypatch):
    from api.app import _parse_cors

    assert _parse_cors("https://a.example, https://b.example") == ["https://a.example", "https://b.example"]


def test_cors_ignores_empty_entries(monkeypatch):
    """A trailing comma is the most likely hand-edit, and an empty origin matches nothing while
    looking like a value — so it must be dropped rather than passed to the middleware."""
    from api.app import _parse_cors

    assert _parse_cors("https://a.example,,  ,") == ["https://a.example"]


def test_the_bind_defaults_are_unchanged(monkeypatch):
    """0.0.0.0:8000 is what runs today; an instance may narrow it without a rebuild."""
    import inspect

    import api.__main__ as m

    src = inspect.getsource(m.main)
    assert '"KUMO_BIND_HOST", "0.0.0.0"' in src, "the default bind host changed"
    assert '"KUMO_BIND_PORT", "8000"' in src, "the default bind port changed"


def test_the_template_exists_and_is_not_a_copy_of_a_real_deployment():
    """The rule: examples are SYNTHETIC, never scrubbed copies.

    A scrubbed copy fails twice — the scrubbing can miss a key, and a reader cannot tell which numbers
    were guidance and which were leftovers. So the template must NOT carry the live universe or the
    live sleeve figures, and its gates must be OFF (CLAUDE.md: opt-in only).
    """
    import json

    tpl = Path(__file__).resolve().parent.parent.parent / "deploy" / "instance-template"
    assert tpl.is_dir(), f"no instance-template at {tpl}"

    env = (tpl / "instance.env.example").read_text()
    assert "KUMO_VOLUME_PREFIX" in env, "the template omits the volume prefix, which is the state seam"
    assert "KUMO_ORDERS_ARMED=false" in env, "the template arms orders by default"

    strategies = json.loads((tpl / "settings" / "strategies.example.json").read_text())
    assert strategies.get("QC27_ENABLED") is False, "template enables a live lane"
    assert len(strategies.get("QC27_UNIVERSE", [])) <= 5, (
        "the template carries a full universe — that is a copy of a deployment, not an example"
    )
