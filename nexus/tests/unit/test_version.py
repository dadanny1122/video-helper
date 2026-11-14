import nexus


def test_versions_are_well_formed():
    assert nexus.version_tuple(nexus.NEXUS_VERSION) >= (0, 1, 0)
    assert nexus.version_tuple(nexus.MIN_AGENT_VERSION) <= nexus.version_tuple(nexus.NEXUS_VERSION)


def test_protocol_compat_is_major_only():
    assert nexus.protocol_compatible("1.0")
    assert nexus.protocol_compatible("1.7")
    assert not nexus.protocol_compatible("2.0")
    assert not nexus.protocol_compatible("")
    assert not nexus.protocol_compatible(None)
