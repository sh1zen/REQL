"""Configuration parsing and override isolation at the public loader boundary."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import config as builtin_config
from memory.config import (
    ConfigError,
    default_config,
    load_config,
    load_effective_config,
    load_project_config_data,
    merge_config,
    set_local_config_option,
    write_sample_config,
)

PACKAGED_DEFAULTS = Path(builtin_config.__file__).with_name("config")


class ConfigurationIntegrityTests(unittest.TestCase):
    """Keep supported YAML values and caller-owned overrides intact."""

    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "reql.conf"
        self.defaults = self.root / "config"
        self.defaults.mkdir()

    def _read_fixture_resource(self, _module: str, resource: str) -> bytes:
        """Read a bundled resource from the test project."""
        return (self.root / resource).read_bytes()

    def test_single_quoted_values_preserve_hashes_apostrophes_and_double_quotes(self) -> None:
        """Comment stripping follows YAML quoting rather than truncating the scalar."""
        for scalar, expected in (
            ("'invoice # archive' # comment", "invoice # archive"),
            ("'it''s # archived' # comment", "it's # archived"),
            ("'say \"invoice # archive\"' # comment", 'say "invoice # archive"'),
            ("don't-truncate#this # comment", "don't-truncate#this"),
            ('"invoice \\\"#\\\" archive" # comment', 'invoice "#" archive'),
        ):
            with self.subTest(scalar=scalar):
                self.path.write_text(f"project:\n  id: {scalar}\n", encoding="utf-8")
                self.assertEqual(load_config(self.path).project.id, expected)

    def test_unterminated_or_invalid_quoted_values_are_configuration_errors(self) -> None:
        """Malformed quoting must not become an accepted plain string."""
        for scalar in ("'unterminated", '"unterminated', "'bad'quote'", "'ok' trailing", "'"):
            with self.subTest(scalar=scalar):
                self.path.write_text(f"project:\n  id: {scalar}\n", encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, "line 2"):
                    load_config(self.path)

    def test_duplicate_yaml_keys_are_rejected_with_location(self) -> None:
        """A duplicate section or option cannot silently replace earlier settings."""
        for text in ("project:\n  id: first\n  id: second\n",
                     "project:\n  id: first\nproject:\n  id: second\n"):
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, "[Dd]uplicate.*line 3|line 3.*[Dd]uplicate"):
                    load_config(self.path)

    def test_nested_and_dotted_overrides_do_not_mutate_caller_values(self) -> None:
        """Path updates use private mappings even when callers reuse their inputs."""
        overrides = {"compile": {"documents": {"markdown": False}}, "compile.documents.pdf": True}
        before = deepcopy(overrides)
        config = merge_config(default_config(), overrides)
        self.assertTrue(config.compile.documents["pdf"])
        self.assertFalse(config.compile.documents["markdown"])
        self.assertEqual(overrides, before)

    def test_nonfinite_file_size_limits_fail_at_configuration_boundary(self) -> None:
        """Reject unusable limits before filesystem scanning converts them to bytes."""
        for value in (float("nan"), float("inf"), float("-inf"), 10 ** 400):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "scan.max_file_size_mb"):
                merge_config(default_config(), {"scan.max_file_size_mb": value})
        with self.assertRaisesRegex(ConfigError, "scan.max_file_size_mb"):
            load_effective_config(start_dir=self.root, env={"REQL_CONFIG_OVERRIDES": '{"scan.max_file_size_mb": NaN}'})

    def test_invalid_policy_is_rejected_at_configuration_boundary(self) -> None:
        """Reject malformed expressions, invalid ranges and duplicate values."""
        for key, value, message in (
            ("testing.discovery.directories", ["cases["], "Invalid regex in testing.discovery.directories"),
            ("testing.discovery.directories", [""], "testing.discovery.directories"),
            ("testing.suggestions.evidence_order", ["static", "static", "candidate"], "evidence_order"),
            ("lexical.seeds.entity_confidence", 1.1, "lexical.seeds.entity_confidence"),
            ("lexical.index.metadata_weight", -1.0, "lexical.index.metadata_weight"),
            ("retrieval.context.min_confidence", 1.1, "retrieval.context.min_confidence"),
            ("retrieval.ranking.min_edge_weight", float("nan"), "retrieval.ranking.min_edge_weight"),
            ("query.defaults.path_columns", ["score", "score"], "query.defaults.path_columns"),
        ):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, message):
                merge_config(default_config(), {key: value})

    def test_default_template_contains_only_supported_sections(self) -> None:
        """Keep generated files small while resolving the full runtime defaults."""
        write_sample_config(self.path)
        self.assertEqual(load_project_config_data(self.path), {"scan": {"exclude": []}})
        for overrides, cache_enabled in (({}, True), ({"cache.enabled": False}, False)):
            with self.subTest(cache_enabled=cache_enabled):
                config = load_effective_config(self.path, overrides=overrides, env={})
                self.assertEqual(config, merge_config(default_config(), overrides))
                self.assertEqual(config.to_dict()["cache"], {"enabled": cache_enabled})

    def test_packaged_user_defaults_are_valid_project_overrides(self) -> None:
        """Every documented user default can also be supplied via reql.conf."""
        user_defaults = (PACKAGED_DEFAULTS / "user.yaml").read_text(encoding="utf-8")
        self.path.write_text(user_defaults, encoding="utf-8")
        self.assertEqual(load_config(self.path), default_config())

    def test_bundled_yaml_reports_missing_invalid_and_overlapping_defaults(self) -> None:
        """Configuration loading reports the resource and error for damaged defaults."""
        user_defaults = (PACKAGED_DEFAULTS / "user.yaml").read_text(encoding="utf-8")
        core_path = self.defaults / "core.yaml"
        core_defaults = (PACKAGED_DEFAULTS / "core.yaml").read_text(encoding="utf-8")
        core_path.write_text(core_defaults, encoding="utf-8")
        user_path = self.defaults / "user.yaml"

        with patch("memory.config.loader.get_data", side_effect=self._read_fixture_resource):
            with self.assertRaisesRegex(ConfigError, r"Cannot read built-in configuration .*user\.yaml"):
                default_config()
            for text, message in (
                ("project:\n   id: invalid\n", r"user\.yaml at line 2"),
                ("", r"Missing built-in config section\(s\):.*testing"),
                ("lexical:\n  index:\n    metadata_weight: 1.2\n", r"Duplicate built-in option lexical\.index\.metadata_weight"),
                ('retrieval:\n  search:\n    type_bonuses: {"Other": 0.05}\n',
                 r"Duplicate built-in option retrieval\.search\.type_bonuses"),
            ):
                with self.subTest(message=message):
                    user_path.write_text(text, encoding="utf-8")
                    with self.assertRaisesRegex(ConfigError, message):
                        default_config()
            user_path.write_text(user_defaults.replace("  id: default\n", "  id: refreshed\n", 1), encoding="utf-8")
            self.assertEqual(default_config().project.id, "refreshed")
            user_path.write_text(user_defaults, encoding="utf-8")
            self.assertEqual(default_config().project.id, "default")
            before_activation, activation_and_rest = core_defaults.split("\n  activation:\n", 1)
            _, after_activation = activation_and_rest.split("\n  salience:\n", 1)
            core_path.write_text(f"{before_activation}\n  salience:\n{after_activation}", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, r"Missing config option\(s\) in \[analysis\]: activation"):
                default_config()

    def test_yaml_membership_controls_project_overrides(self) -> None:
        """Moving defaults between bundled files changes permissions at every merge boundary."""
        core = (PACKAGED_DEFAULTS / "core.yaml").read_text(encoding="utf-8")
        user = (PACKAGED_DEFAULTS / "user.yaml").read_text(encoding="utf-8")
        protected = "    metadata_weight: 1.2\n"
        cache = "\ncache:\n  enabled: true\n"
        (self.defaults / "core.yaml").write_text(core.replace(protected, "") + cache, encoding="utf-8")
        (self.defaults / "user.yaml").write_text(
            user.replace(cache, "") + "\nlexical:\n  index:\n" + protected, encoding="utf-8",
        )
        self.path.write_text("lexical:\n  index:\n    metadata_weight: 1.3\n", encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, r"lexical\.index\.metadata_weight.*user\.yaml"):
            load_config(self.path)

        with patch("memory.config.loader.get_data", side_effect=self._read_fixture_resource):
            config = load_config(self.path)
            self.assertEqual(config.lexical.index.metadata_weight, 1.3)
            self.assertEqual(load_project_config_data(self.path), {"lexical": {"index": {"metadata_weight": 1.3}}})
            direct = merge_config(config, {"lexical.index.metadata_weight": 1.4}, project_only=True)
            self.assertEqual(direct.lexical.index.metadata_weight, 1.4)
            self.assertEqual(load_effective_config(
                self.path, env={"REQL_CONFIG_OVERRIDES": "lexical.index.metadata_weight=1.4"},
            ).lexical.index.metadata_weight, 1.4)
            with self.assertRaisesRegex(ConfigError, r"cache\.enabled.*user\.yaml"):
                set_local_config_option("cache.enabled", "false", start_dir=self.root)
            self.path.write_text("cache:\n  enabled: false\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, r"cache\.enabled.*user\.yaml"):
                load_config(self.path)

    def test_project_map_options_allow_new_entries(self) -> None:
        """A registry declared in user.yaml retains support for new format names."""
        self.path.write_text(
            "compile:\n  documents:\n    custom: true\n"
            '  document_formats:\n    custom: {"extensions": [".custom"]}\n',
            encoding="utf-8",
        )
        config = load_config(self.path)
        self.assertTrue(config.compile.documents["custom"])
        self.assertEqual(config.compile.document_formats["custom"]["extensions"], [".custom"])

    def test_scan_ignore_defaults_retains_lists_owned_by_core(self) -> None:
        """Reset only the scan lists that user.yaml exposes to project configuration."""
        core = (PACKAGED_DEFAULTS / "core.yaml").read_text(encoding="utf-8")
        user = (PACKAGED_DEFAULTS / "user.yaml").read_text(encoding="utf-8")
        before, excludes_and_rest = user.split("\n  exclude:\n", 1)
        excludes, after = excludes_and_rest.split("\ncompile:\n", 1)
        (self.defaults / "core.yaml").write_text(f"{core}\nscan:\n  exclude:\n{excludes}", encoding="utf-8")
        (self.defaults / "user.yaml").write_text(f"{before}\n\ncompile:\n{after}", encoding="utf-8")
        self.path.write_text("scan:\n  ignore_defaults: true\n  include: [\"custom/\"]\n", encoding="utf-8")

        with patch("memory.config.loader.get_data", side_effect=self._read_fixture_resource):
            config = load_config(self.path)
            self.assertTrue(config.scan.ignore_defaults)
            self.assertEqual(config.scan.include, ["custom/"])
            self.assertEqual(config.scan.exclude, default_config().scan.exclude)

    def test_core_policy_cannot_be_set_through_project_overrides(self) -> None:
        """Check file, nested scanner, environment and command override boundaries."""
        for contents, message in (
            ("lexical:\n  seeds:\n    entity_confidence: 0.7\n", r"lexical\.seeds\.entity_confidence"),
            ("storage:\n  page_cache_blocks: 64\n", r"storage\.page_cache_blocks.*core\.yaml"),
            ('query:\n  defaults:\n    path_columns: ["score"]\n', r"query\.defaults\.path_columns.*core\.yaml"),
        ):
            self.path.write_text(contents, encoding="utf-8")
            for load in (load_config, load_project_config_data):
                with self.subTest(load=load.__name__, option=message), self.assertRaisesRegex(ConfigError, message):
                    load(self.path)

        write_sample_config(self.path, overwrite=True)
        original = self.path.read_bytes()
        for option, value in (
            ("lexical.normalization.fold_accents", False),
            ("lexical.documents.frequency_weight", 0.7),
            ("lexical.index.token_signal_weight", 0.7),
            ("retrieval.search.exact_symbol_score", 0.9),
            ("retrieval.ranking.seed_weight", 0.2),
            ("retrieval.ranking.min_edge_weight", 0.2),
            ("retrieval.context.min_confidence", 0.75),
            ("retrieval.explore.docs_nodes", ["SourceFragment"]),
            ("analysis.salience.half_life_days", 30),
            ("query.path.salience_weight", 0.3),
            ("storage.page_cache_blocks", 64),
        ):
            with self.subTest(option=option), self.assertRaisesRegex(ConfigError, r"core\.yaml"):
                load_effective_config(self.path, env={}, overrides={option: value})
        with self.assertRaisesRegex(ConfigError, r"query\.path\.edge_weight"):
            load_effective_config(self.path, env={"REQL_CONFIG_OVERRIDES": '{"query": {"path": {"edge_weight": 0.7}}}'})
        with self.assertRaisesRegex(ConfigError, r"lexical\.index\.metadata_weight"):
            set_local_config_option("lexical.index.metadata_weight", "1.3", start_dir=self.root)
        self.assertEqual(self.path.read_bytes(), original)

    def test_project_setup_settings_follow_override_precedence(self) -> None:
        """Load file, environment and explicit setup overrides in order."""
        self.path.write_text(
            'retrieval:\n  source:\n    setup_headings: ["Avvio locale"]\n'
            '    setup_preferred_paths: ["guide.md"]\n',
            encoding="utf-8",
        )
        set_local_config_option("scan.max_file_size_mb", "768", start_dir=self.root)
        env = {"REQL_CONFIG_OVERRIDES": '{"retrieval.source.setup_preferred_paths": ["install.md"]}'}
        cases = (
            ({}, {}, "guide.md", "Avvio locale"),
            (env, {}, "install.md", "Avvio locale"),
            (env, {"retrieval.source.setup_preferred_paths": ["docs/install.md"]}, "docs/install.md", "Avvio locale"),
            (
                {"REQL_CONFIG_OVERRIDES": '{"retrieval.source.setup_headings": ["Setup"],'
                                          '"retrieval.source.setup_preferred_paths": ["install.md"]}'},
                {"retrieval": {"source": {"setup_preferred_paths": ["docs/install.md"]}}},
                "docs/install.md", "Setup",
            ),
        )
        defaults = default_config()
        for environment, overrides, preferred_path, heading in cases:
            with self.subTest(environment=environment, overrides=overrides):
                config = load_effective_config(self.path, env=environment, overrides=overrides)
                self.assertEqual(config.scan.max_file_size_mb, 768)
                self.assertEqual(config.retrieval.source.setup_headings, (heading,))
                self.assertEqual(config.retrieval.source.setup_preferred_paths, (preferred_path,))
                self.assertEqual(config.lexical.normalization, defaults.lexical.normalization)
                self.assertEqual(config.query.path.edge_weight, defaults.query.path.edge_weight)

    def test_config_lookup_uses_nearest_reql_config(self) -> None:
        """Respect the nearest enclosing configuration and explicit overrides."""
        self.path.write_text("scan:\n  max_file_size_mb: 2\n", encoding="utf-8")
        nested = self.root / "nested"
        src = nested / "src"
        src.mkdir(parents=True)
        self.assertEqual(load_effective_config(start_dir=src, env={}).scan.max_file_size_mb, 2)
        (nested / "reql.conf").write_text("scan:\n  max_file_size_mb: 3\n", encoding="utf-8")
        self.assertEqual(load_effective_config(start_dir=src, env={}).scan.max_file_size_mb, 3)
        config = load_effective_config(start_dir=src, env={"REQL_CONFIG": str(self.path)})
        self.assertEqual(config.scan.max_file_size_mb, 2)


if __name__ == "__main__":
    unittest.main()
