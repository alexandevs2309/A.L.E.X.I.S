"""Tests para F1 Capabilities: catálogo, registro y mapping acción→capability."""

from alexis.capabilities import ACTION_TO_CAPABILITY, CapabilitySpec, CapabilityRegistry, build_catalog


def test_catalog_has_all_spheres():
    catalog = build_catalog()
    spheres = {s.sphere for s in catalog.specs()}
    assert "filesystem" in spheres
    assert "cognition" in spheres
    assert "execution" in spheres
    assert "verification" in spheres
    assert "desktop" in spheres
    assert "voice" in spheres
    assert "perception" in spheres
    assert "autonomy" in spheres
    assert "project" in spheres
    assert "web" in spheres
    assert "integration" in spheres
    assert "physical" in spheres


def test_enabled_capabilities_are_honest():
    catalog = build_catalog()
    enabled = catalog.enabled()
    enabled_ids = {s.id for s in enabled}
    # Solo las con adaptador real están habilitadas
    assert "fs.read" in enabled_ids
    assert "fs.stat" in enabled_ids
    assert "fs.write" in enabled_ids
    assert "fs.remove" in enabled_ids
    assert "cognition.understand" in enabled_ids
    assert "cognition.analyze" in enabled_ids
    assert "research.filesystem" in enabled_ids
    assert "execution.sandbox" in enabled_ids
    assert "verification.filesystem" in enabled_ids
    assert "desktop.tools" in enabled_ids
    assert "tts.speak" in enabled_ids
    assert "perception.clap" in enabled_ids
    assert "autonomy.gates" in enabled_ids
    assert "autonomy.queue" in enabled_ids
    # Faltantes (missing) NO están habilitadas
    assert "git.read" not in enabled_ids
    assert "git.commit" not in enabled_ids
    assert "git.push" not in enabled_ids
    assert "terminal.run" not in enabled_ids
    assert "browser.research" not in enabled_ids
    assert "api.http" not in enabled_ids
    assert "mcp.run" not in enabled_ids
    assert "vision.screen" not in enabled_ids
    assert "speech.stt" not in enabled_ids
    assert "iot.mqtt" not in enabled_ids
    assert "codex.run" not in enabled_ids
    assert "opencode.run" not in enabled_ids
    assert "obsidian.run" not in enabled_ids
    assert "omniroute.run" not in enabled_ids
    assert "hermes.run" not in enabled_ids


def test_missing_capabilities_registered_but_not_enabled():
    catalog = build_catalog()
    all_ids = {s.id for s in catalog.specs()}
    missing = {s.id for s in catalog.specs() if s.status == "missing"}
    assert "git.read" in missing
    assert "git.commit" in missing
    assert len(missing) >= 10  # muchas capacidades futuras
    assert all(catalog.has(cid) for cid in missing)


def test_action_to_capability_mapping_complete():
    # Cada acción del planner tiene capability canónica
    expected = {
        "understand": "cognition.understand",
        "analyze": "cognition.analyze",
        "review": "cognition.analyze",
        "research": "research.filesystem",
        "read": "fs.read",
        "observe": "research.filesystem",
        "write": "fs.write",
        "modify": "fs.write",
        "remove": "fs.remove",
        "test": "execute.test",
        "commit": "git.commit",
        "verify": "verification.filesystem",
        "respond": "tts.speak",
        "execute": "execution.sandbox",
        "browser": "browser.research",
        "listen": "speech.stt",
    }
    for action, cap in expected.items():
        assert ACTION_TO_CAPABILITY[action] == cap, action


def test_registry_enable_disable():
    reg = CapabilityRegistry()
    spec = CapabilitySpec(
        id="test.cap",
        sphere="test",
        network=False,
        side_effects=False,
        trust_domain="test",
        sandbox_profile="none",
        status="available",
    )
    reg.register(spec)
    assert not reg.is_enabled("test.cap")
    reg.enable("test.cap")
    assert reg.is_enabled("test.cap")
    assert "test.cap" in {s.id for s in reg.enabled()}
    reg.disable("test.cap")
    assert not reg.is_enabled("test.cap")


def test_registry_rejects_duplicate():
    reg = CapabilityRegistry()
    spec = CapabilitySpec(
        id="dup",
        sphere="test",
        network=False,
        side_effects=False,
        trust_domain="test",
        sandbox_profile="none",
    )
    reg.register(spec)
    try:
        reg.register(spec)
        assert False, "debería fallar"
    except ValueError:
        pass


def test_available_includes_base():
    reg = CapabilityRegistry()
    reg.register_all([
        CapabilitySpec(id="a", sphere="s", network=False, side_effects=False, trust_domain="t", sandbox_profile="x", status="available"),
        CapabilitySpec(id="b", sphere="s", network=False, side_effects=False, trust_domain="t", sandbox_profile="x", status="base"),
        CapabilitySpec(id="c", sphere="s", network=False, side_effects=False, trust_domain="t", sandbox_profile="x", status="missing"),
    ])
    reg.enable("a")
    available = {s.id for s in reg.available()}
    assert available == {"a", "b"}  # base también en available()