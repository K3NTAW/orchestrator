"""Clearly labelled synthetic values for redaction tests."""


def fake_secret(label):
    return "fixture-" + label + "-" + ("0" * 24)
