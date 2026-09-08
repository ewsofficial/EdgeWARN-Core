"""CLI flag-ownership contracts (decomposition Phase 0/1).

Each service must expose only the flags it honors, per the ownership table in
plans/realtime-runner-decomposition-plan.md. The builders in ``util.cli`` are
the single source of those flag definitions; these tests pin which groups a
service's parser accepts and that unowned flags are rejected exactly.
"""

import argparse
import pytest

from util import cli


def build_parser(*builders):
    parser = argparse.ArgumentParser(add_help=False)
    for builder in builders:
        builder(parser)
    return parser


def accepts(parser, *argv):
    try:
        parser.parse_args(list(argv))
        return True
    except SystemExit:
        return False


class TestPrimaryOwnership:
    def test_primary_parser_accepts_all_owned_flags(self):
        parser = build_parser(
            cli.add_base_directory_flags,
            cli.add_primary_domain_flags,
            cli.add_primary_processing_flags,
            cli.add_mrms_core_only_flag,
            cli.add_service_enablement_flags,
        )
        assert accepts(parser, "--lat_limits", "20", "55", "--lon_limits", "230", "300")
        for flag in ("--base-dir", "--config-dir", "--ctam-module-dir"):
            assert accepts(parser, flag, "/tmp/value"), f"primary parser rejected {flag}"
        for flag in ("--refl-threshold", "--min-seed-percentage", "--drop-offset"):
            assert accepts(parser, flag, "1.0"), f"primary parser rejected {flag}"
        for flag in (
            "--profile", "--disable-ctam", "--disable-tracking",
            "--disable-polygon-expansion", "--mrms-core-only",
            "--disable-ewmrs", "--disable-nws", "--disable-metar",
            "--disable-goes", "--disable-nexrad",
        ):
            assert accepts(parser, flag), f"primary parser rejected {flag}"

    def test_primary_only_parser_rejects_accessory_service_flags(self):
        """Without accessory routing, EWMRS/NWS/METAR/NEXRAD flags are unowned."""
        parser = build_parser(
            cli.add_base_directory_flags,
            cli.add_primary_domain_flags,
            cli.add_primary_processing_flags,
            cli.add_mrms_core_only_flag,
        )
        for flag in ("--disable-metar", "--disable-nws", "--disable-ewmrs", "--disable-nexrad"):
            assert not accepts(parser, flag), f"primary-only parser accepted {flag}"


class TestSharedDefinitions:
    def test_base_dir_accepts_both_spellings(self):
        parser = build_parser(cli.add_base_directory_flags)
        assert accepts(parser, "--base_dir", "/tmp/a")
        assert accepts(parser, "--base-dir", "/tmp/b")

    def test_boolean_switches_cannot_express_unset(self):
        """Owned switches default to None so overlay.resolve can see 'not given'."""
        from tests.architecture.source_inspect import argparse_defaults

        defaults = argparse_defaults("util/cli.py")
        for flag in (
            "--profile", "--disable-ctam", "--disable-tracking",
            "--disable-polygon-expansion", "--disable-ewmrs", "--disable-goes",
            "--mrms-core-only",
        ):
            assert defaults[flag]["default"] is None, f"{flag} has a real default"

    def test_mrms_core_only_defaults_to_none_for_overlay(self):
        parser = build_parser(cli.add_mrms_core_only_flag)
        args = parser.parse_args([])
        assert args.mrms_core_only is None
