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


class TestSharedDefinitions:
    def test_base_dir_accepts_both_spellings(self):
        parser = build_parser(cli.add_base_directory_flags)
        assert accepts(parser, "--base_dir", "/tmp/a")
        assert accepts(parser, "--base-dir", "/tmp/b")


    def test_mrms_core_only_defaults_to_none_for_overlay(self):
        parser = build_parser(cli.add_mrms_core_only_flag)
        args = parser.parse_args([])
        assert args.mrms_core_only is None
