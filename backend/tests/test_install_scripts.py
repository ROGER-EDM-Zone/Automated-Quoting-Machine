"""Guards on the Windows install scripts.

These cannot be executed here — there is no PowerShell on this machine — so
they are read instead. That is worth doing because the one bug these check for
got all the way onto a real office PC and stopped the install dead, and it did
so while every underlying command was succeeding.

The bug: PowerShell counts anything a program writes to the "error" channel as
an error, and `$ErrorActionPreference = "Stop"` turns that into a fatal one.
Alembic narrates its normal progress on that channel, so a database migration
that worked perfectly killed the setup. `Invoke-Step` exists to capture both
channels as plain text and judge a command only on its exit code.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

INSTALL = Path(__file__).resolve().parents[2] / "install"
SETUP = INSTALL / "Setup.ps1"

#: The launchers that `cd` into the backend folder. Command Prompt cannot make
#: a network folder its working directory at all, so each has to say so itself.
LAUNCHERS = ("Start.bat", "Sign-in.bat", "Check-connection.bat")


def setup_source() -> str:
    return SETUP.read_text(encoding="utf-8")


def test_setup_script_exists():
    assert SETUP.is_file()


def test_the_error_channel_is_merged_in_exactly_one_safe_place():
    """`... 2>&1 | Out-Null` is the exact shape that broke the real install.

    Merging the channels is fine — necessary, even — but only where the result
    is turned straight into plain strings. Piped anywhere else, each captured
    line is still an error object, and the next one to flow past re-triggers
    the very abort this is meant to prevent.
    """
    merges = [
        (line_no, line.strip())
        for line_no, line in enumerate(setup_source().splitlines(), start=1)
        if "2>&1" in line.split("#", 1)[0]
    ]
    assert len(merges) == 1, f"expected one merge, inside Invoke-Step; found {merges}"

    _, line = merges[0]
    assert 'ForEach-Object { "$_" }' in line, (
        f"{line} merges the error channel without flattening it to text. "
        "Ordinary progress output would abort the install."
    )


def test_every_python_command_goes_through_invoke_step():
    """Any bare `& $python ...` reintroduces the same trap."""
    source = setup_source()
    assert "function Invoke-Step" in source

    stray = [
        line.strip()
        for line in source.splitlines()
        if re.search(r"^\s*&\s*\$(venvPython|python)\b", line)
    ]
    assert not stray, (
        "These call Python directly instead of via Invoke-Step, so ordinary "
        f"progress output would abort the install: {stray}"
    )


def test_a_failed_step_shows_what_it_printed():
    """Silently discarding the output is how the last failure became unreadable."""
    source = setup_source()
    assert "function Step-Tail" in source
    # Every failure branch should offer the captured text rather than a bare
    # "it went wrong".
    assert source.count("$(Step-Tail)") >= 4


def test_setup_refuses_to_install_into_a_network_folder():
    """A redirected Desktop looks local and breaks venvs and SQLite silently."""
    source = setup_source()
    assert 'StartsWith("\\\\")' in source
    assert "System.IO.DriveType]::Network" in source
    # And it must say what to do about it, in words, not just refuse.
    assert "C:\\EDMZone\\Quoting" in source


@pytest.mark.parametrize("name", LAUNCHERS)
def test_launchers_refuse_a_network_folder_too(name: str):
    body = (INSTALL / name).read_text(encoding="utf-8")
    assert '"%HERE:~0,2%"=="\\\\"' in body, (
        f"{name} would `cd` into a network folder, fail, and then report the "
        "app as 'not set up' — which is not what went wrong."
    )
    assert "C:\\EDMZone\\Quoting" in body


def test_setup_never_seeds_invented_rates():
    """Placeholder rates in the machine that quotes customers is the one
    thing this system exists to prevent."""
    source = setup_source()
    assert "scripts.seed" in source
    for line in source.splitlines():
        if "scripts.seed" in line and not line.strip().startswith("#"):
            assert "--sources-only" in line, line.strip()
