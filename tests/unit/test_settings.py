"""Tests of the settings subset: shipped defaults, user override file and validation."""

import unittest

from agents_dispatcher.adapters.system.settings_loader import load_settings, shipped_defaults_text
from agents_dispatcher.application.settings import InvalidSettingsError, Settings, SettingsError
from agents_dispatcher.domain.routing import Chain, HandoffLink, ProviderLink, TaskMode

HOME = "/Users/example"


def load_shipped(user_text: str | None = None) -> Settings:
    """Load the shipped defaults, optionally under a user file, for a synthetic home."""
    return load_settings(
        defaults_text=shipped_defaults_text(), user_text=user_text, account_home=HOME
    )


class ShippedDefaultsTests(unittest.TestCase):
    """The defaults that ship with the package (ARCH section 3.2)."""

    def test_defaults_match_the_catalogue(self) -> None:
        """Every M1 key carries its documented default."""
        settings = load_shipped()
        self.assertEqual(settings.memory_bytes, 2_147_483_648, msg="resources.run.memory_bytes")
        self.assertEqual(settings.file_bytes, 10_737_418_240, msg="resources.run.file_bytes")
        self.assertEqual(
            settings.descriptors_per_process, 256, msg="resources.run.descriptors_per_process"
        )
        self.assertEqual(settings.hard_s, 14400.0, msg="watchdog.hard_s")
        self.assertEqual(settings.term_grace_s, 10.0, msg="watchdog.term_grace_s")
        self.assertEqual(settings.kill_wait_s, 2.0, msg="watchdog.kill_wait_s")
        self.assertEqual(
            settings.provider_hosts,
            {"anthropic": ("api.anthropic.com",), "openai": ("chatgpt.com",)},
            msg="proxy.providers.<id>.hosts",
        )
        self.assertEqual(settings.proxy_ports, (443,), msg="proxy.ports")
        self.assertEqual(
            settings.read_deny,
            ("@sensitive-user-data", "@other-vendor-state", "@sdk-control", "@other-runs"),
            msg="sandbox.read_deny",
        )
        self.assertEqual(
            settings.executables,
            {"anthropic": "auto", "openai": "auto"},
            msg="profiles._template.executable for every provider",
        )

    def test_storage_and_proxy_limits_carry_their_catalogue_defaults(self) -> None:
        """ER-2: the SQLite busy timeout and the proxy bounds come from settings, not code."""
        settings = load_shipped()
        self.assertEqual(settings.sqlite_busy_s, 2.0, msg="storage.sqlite_busy_s")
        self.assertEqual(settings.proxy_header_bytes, 16384, msg="proxy.header_bytes")
        self.assertEqual(settings.proxy_connections, 32, msg="proxy.connections")
        self.assertEqual(settings.proxy_buffer_bytes, 1_048_576, msg="proxy.buffer_bytes")
        self.assertEqual(settings.proxy_idle_s, 60.0, msg="proxy.idle_s")
        self.assertEqual(settings.proxy_lifetime_s, 1800.0, msg="proxy.lifetime_s")
        self.assertFalse(settings.proxy_allow_nonpublic, msg="proxy.allow_nonpublic")
        self.assertEqual(settings.proxy_connect_s, 10.0, msg="proxy.connect_s")

    def test_the_worker_model_and_effort_carry_their_defaults(self) -> None:
        """The owner chose claude-sonnet-5-5 (2026-11-13); effort follows ARCH L3554."""
        settings = load_shipped()
        anthropic = settings.providers["anthropic"]
        self.assertEqual(anthropic.model, "claude-sonnet-5-5", msg="providers.anthropic.model")
        self.assertEqual(anthropic.effort, "medium", msg="providers._template.effort")

    def test_a_user_file_can_change_the_model_and_the_effort(self) -> None:
        """Both are single keys under their tables."""
        settings = load_shipped(
            '[providers.anthropic]\nmodel = "opus"\n[providers._template]\neffort = "high"\n'
        )
        anthropic = settings.providers["anthropic"]
        self.assertEqual(anthropic.model, "opus", msg="model override")
        self.assertEqual(anthropic.effort, "high", msg="effort override")

    def test_a_model_that_is_not_one_token_is_refused_naming_the_key(self) -> None:
        """Empty, blank and whitespace-containing values never reach a command line."""
        controls = ("esc\\u001bx", "del\\u007fx", "nel\\u0085x")
        for bad in (
            "",
            " ",
            "two words",
            "tab\there",
            "new\\nline",
            "--dangerously-x",
            "-m",
            *controls,
        ):
            with self.assertRaises(SettingsError, msg=repr(bad)) as caught:
                load_shipped(f'[providers.anthropic]\nmodel = "{bad}"\n')
            self.assertIn("providers.anthropic.model", str(caught.exception), msg=repr(bad))

    def test_an_effort_outside_the_documented_levels_is_refused(self) -> None:
        """`claude --help` documents low, medium, high, xhigh and max."""
        with self.assertRaises(SettingsError) as caught:
            load_shipped('[providers._template]\neffort = "extreme"\n')
        self.assertIn("providers._template.effort", str(caught.exception), msg="names the key")

    def test_the_task_input_bound_defaults_to_one_mebibyte_and_can_change(self) -> None:
        """`security.input_bytes` (ARCH L3573) bounds the task text of `start` and `run`."""
        self.assertEqual(load_shipped().input_bytes, 1048576, msg="shipped default")
        self.assertEqual(
            load_shipped("[security]\ninput_bytes = 4096\n").input_bytes, 4096, msg="override"
        )

    def test_an_input_bound_that_is_not_a_positive_integer_is_refused(self) -> None:
        """Zero and text name the key."""
        for bad in ("0", '"big"', "-1"):
            with self.assertRaises(SettingsError, msg=bad) as caught:
                load_shipped(f"[security]\ninput_bytes = {bad}\n")
            self.assertIn("security.input_bytes", str(caught.exception), msg=bad)

    def test_the_watchdog_scan_interval_comes_from_settings(self) -> None:
        """The interval of the time and memory scan is a setting, not a constant in the code."""
        self.assertEqual(load_shipped().watchdog_scan_s, 1.0, msg="watchdog.scan_s")

    def test_the_background_run_settings_carry_their_defaults(self) -> None:
        """M2: start-up wait, public text bounds and the supervisor log folder are settings."""
        settings = load_shipped()
        self.assertEqual(settings.watchdog_startup_s, 30.0, msg="watchdog.startup_s")
        self.assertEqual(settings.public_reason_bytes, 512, msg="public.reason_bytes")
        self.assertEqual(settings.public_summary_bytes, 1024, msg="public.summary_bytes")
        self.assertEqual(
            settings.logging_directory,
            f"{HOME}/.local/state/agents-dispatcher/logs",
            msg="logging.directory resolves @state",
        )

    def test_default_locations_resolve_under_the_account_home_and_never_under_tmp(self) -> None:
        """Durable roots live in account state, not in a temporary directory."""
        settings = load_shipped()
        self.assertEqual(
            settings.store_path, f"{HOME}/.local/state/agents-dispatcher", msg="store.path"
        )
        self.assertEqual(
            settings.run_area_path, f"{HOME}/.local/state/agents-dispatcher/runs", msg="run_area"
        )
        for path in (settings.store_path, settings.run_area_path):
            temporary_roots = ("/tmp", "/private/tmp", "/var/tmp")  # noqa: S108 - asserted absent
            self.assertFalse(path.startswith(temporary_roots), msg=path)

    def test_sensitive_inventories_resolve_against_the_account_home(self) -> None:
        """The shipped inventories hold absolute paths, so no list is hard-coded elsewhere."""
        settings = load_shipped()
        entries = settings.sensitive_user_data + settings.other_vendor_state
        self.assertIn(f"{HOME}/.ssh", settings.sensitive_user_data, msg="ssh keys are listed")
        self.assertTrue(all(p.startswith("/") for p in entries), msg="every entry is absolute")
        self.assertNotIn(
            f"{HOME}/Library/Keychains", settings.sensitive_user_data, msg="keychains stay readable"
        )


class OverrideTests(unittest.TestCase):
    """The user file and a changed default."""

    def test_a_user_value_replaces_the_default(self) -> None:
        """Only the named key changes."""
        settings = load_shipped('[watchdog]\nhard_s = "60"\n')
        self.assertEqual(settings.hard_s, 60.0, msg="override applied")
        self.assertEqual(settings.term_grace_s, 10.0, msg="neighbour untouched")

    def test_a_changed_default_takes_effect_without_a_code_change(self) -> None:
        """The defaults text is the single source of every value."""
        changed = shipped_defaults_text().replace('hard_s = "14400"', 'hard_s = "99"')
        settings = load_settings(defaults_text=changed, user_text=None, account_home=HOME)
        self.assertEqual(settings.hard_s, 99.0, msg="default read from the text")

    def test_a_user_path_token_is_resolved(self) -> None:
        """Tokens in a user value resolve like the shipped ones."""
        settings = load_shipped('[run_area]\npath = "@account-state/elsewhere"\n')
        self.assertEqual(settings.run_area_path, f"{HOME}/.local/state/elsewhere", msg="token")


class ValidationTests(unittest.TestCase):
    """Bad input is refused with a message that names the key."""

    def refused(self, user_text: str) -> str:
        """Return the message of the SettingsError the text provokes."""
        with self.assertRaises(SettingsError) as caught:
            load_shipped(user_text)
        return str(caught.exception)

    def test_an_unknown_key_is_refused(self) -> None:
        """A typo must not be silently ignored."""
        self.assertIn("watchdog.hard_sec", self.refused('[watchdog]\nhard_sec = "5"\n'), msg="key")

    def test_a_wrong_type_names_the_key(self) -> None:
        """A number where a string is required."""
        self.assertIn("watchdog.hard_s", self.refused("[watchdog]\nhard_s = 5\n"), msg="key")

    def test_a_non_positive_limit_is_refused(self) -> None:
        """Zero would stop every run at once."""
        self.assertIn(
            "resources.run.memory_bytes",
            self.refused("[resources.run]\nmemory_bytes = 0\n"),
            msg="key",
        )

    def test_a_zero_file_or_descriptor_limit_is_refused(self) -> None:
        """Zero would forbid every write or every open file."""
        for key in ("file_bytes", "descriptors_per_process"):
            with self.subTest(key=key):
                self.assertIn(
                    f"resources.run.{key}",
                    self.refused(f"[resources.run]\n{key} = 0\n"),
                    msg="key named",
                )

    def test_a_port_out_of_range_is_refused(self) -> None:
        """Ports are 1 to 65535."""
        self.assertIn("proxy.ports", self.refused("[proxy]\nports = [0]\n"), msg="key")

    def test_an_unknown_selector_is_refused(self) -> None:
        """Only the four documented selectors exist."""
        self.assertIn(
            "sandbox.read_deny", self.refused('[sandbox]\nread_deny = ["@nothing"]\n'), msg="key"
        )

    def test_a_relative_path_is_refused(self) -> None:
        """Locations are absolute or tokens."""
        self.assertIn("store.path", self.refused('[store]\npath = "state"\n'), msg="key")

    def test_malformed_toml_is_refused_with_a_settings_error(self) -> None:
        """No bare traceback."""
        self.assertIn("settings", self.refused("[watchdog\n").lower(), msg="mentions settings")

    def test_a_non_boolean_flag_is_refused(self) -> None:
        """proxy.allow_nonpublic is a real boolean, not a string."""
        self.assertIn(
            "proxy.allow_nonpublic",
            self.refused('[proxy]\nallow_nonpublic = "yes"\n'),
            msg="key",
        )

    def test_invalid_background_run_settings_are_refused(self) -> None:
        """Zero waits, zero bounds and relative log folders are errors that name the key."""
        cases = {
            "watchdog.startup_s": '[watchdog]\nstartup_s = "0"\n',
            "public.reason_bytes": "[public]\nreason_bytes = 0\n",
            "public.summary_bytes": "[public]\nsummary_bytes = 0\n",
            "logging.directory": '[logging]\ndirectory = "logs"\n',
        }
        for key, text in cases.items():
            with self.subTest(key=key):
                self.assertIn(key, self.refused(text), msg="the error names the key")

    def test_a_non_positive_scan_interval_is_refused(self) -> None:
        """Zero would make the watchdog spin."""
        self.assertIn("watchdog.scan_s", self.refused('[watchdog]\nscan_s = "0"\n'), msg="key")

    def test_a_non_positive_busy_timeout_is_refused(self) -> None:
        """Zero would make every contention an instant failure."""
        self.assertIn(
            "storage.sqlite_busy_s",
            self.refused('[storage]\nsqlite_busy_s = "0"\n'),
            msg="key",
        )

    def test_a_wrong_schema_version_is_refused(self) -> None:
        """The schema version is an exact constant."""
        self.assertIn("schema_version", self.refused("schema_version = 2\n"), msg="key")

    def test_too_many_fraction_digits_are_refused(self) -> None:
        """Quoted decimal seconds carry at most nine fractional digits."""
        self.assertIn(
            "watchdog.kill_wait_s",
            self.refused('[watchdog]\nkill_wait_s = "1.0123456789"\n'),
            msg="key",
        )


if __name__ == "__main__":
    unittest.main()


class TaskTypeTests(unittest.TestCase):
    """Task types, the default mode and the write allow-list (ARCH L3532-L3548, L3565)."""

    def refused(self, user_text: str) -> str:
        """Return the message of the InvalidSettingsError the text provokes."""
        with self.assertRaises(InvalidSettingsError, msg="invalid settings") as caught:
            load_shipped(user_text)
        return str(caught.exception)

    def test_the_shipped_task_types_and_defaults(self) -> None:
        """Default mode read; audit reads, implement writes; only implement may write."""
        settings = load_shipped()
        self.assertEqual(settings.default_mode, TaskMode.READ, msg="defaults.mode")
        self.assertEqual(settings.tasks["audit"].mode, TaskMode.READ, msg="tasks.audit.mode")
        self.assertEqual(settings.tasks["implement"].mode, TaskMode.WRITE, msg="tasks.implement")
        self.assertEqual(settings.allow_write_tasks, ("implement",), msg="allow_write_tasks")
        self.assertEqual(settings.recommendation_model, "gpt-6.1-sol", msg="recommendation_model")
        self.assertEqual(settings.recommendation_effort, "medium", msg="recommendation_effort")

    def test_a_user_file_adds_a_task_type(self) -> None:
        """New names are open; everything else is still checked."""
        settings = load_shipped('[tasks.security-review]\nmode = "read"\n')
        self.assertEqual(settings.tasks["security-review"].mode, TaskMode.READ, msg="added")
        self.assertIn("audit", settings.tasks, msg="the shipped task types stay")

    def test_a_user_file_can_allow_another_write_task(self) -> None:
        """The allow-list is a user setting naming declared task types."""
        text = '[tasks.fix]\nmode = "write"\n[security]\nallow_write_tasks = ["implement", "fix"]\n'
        self.assertEqual(load_shipped(text).allow_write_tasks, ("implement", "fix"), msg="list")

    def test_a_task_type_without_a_mode_names_the_key(self) -> None:
        """A table with only a chain has no mode."""
        message = self.refused('[tasks.review]\nchain = [{ provider = "anthropic" }]\n')
        self.assertIn("tasks.review.mode", message, msg="the missing key")

    def test_a_bad_task_name_names_the_key(self) -> None:
        """Grammar: lowercase letters, digits, hyphens."""
        self.assertIn("tasks.Bad", self.refused('[tasks.Bad]\nmode = "read"\n'), msg="the key")

    def test_an_unknown_task_key_is_refused(self) -> None:
        """Only mode and chain exist."""
        self.assertIn(
            "tasks.audit.moed", self.refused('[tasks.audit]\nmoed = "read"\n'), msg="the key"
        )

    def test_a_bad_mode_is_refused_naming_the_key(self) -> None:
        """Only read or write are modes."""
        self.assertIn("defaults.mode", self.refused('[defaults]\nmode = "both"\n'), msg="default")
        self.assertIn(
            "tasks.audit.mode", self.refused('[tasks.audit]\nmode = "both"\n'), msg="task"
        )

    def test_an_allow_list_entry_must_name_a_declared_task_type(self) -> None:
        """A typo would silently allow nothing."""
        text = '[security]\nallow_write_tasks = ["implment"]\n'
        self.assertIn("security.allow_write_tasks", self.refused(text), msg="the key")

    def test_only_the_auto_invocation_is_supported(self) -> None:
        """No other template is registered in this version."""
        text = '[profiles._template]\ninvocation_read = "custom"\n'
        self.assertIn("profiles._template.invocation_read", self.refused(text), msg="read")
        text = '[profiles._template]\ninvocation_write = "custom"\n'
        self.assertIn("profiles._template.invocation_write", self.refused(text), msg="write")

    def test_syntax_and_value_errors_are_invalid_settings(self) -> None:
        """They exit 2; an unreadable file stays a plain SettingsError."""
        self.assertTrue(issubclass(InvalidSettingsError, SettingsError), msg="a SettingsError")
        with self.assertRaises(InvalidSettingsError, msg="syntax"):
            load_shipped("[watchdog\n")
        with self.assertRaises(InvalidSettingsError, msg="unknown key"):
            load_shipped('[watchdog]\nhard_sec = "5"\n')


class ProviderSettingsTests(unittest.TestCase):
    """Per-provider enabled, implementation, model and effort (ARCH L3549-L3556)."""

    def refused(self, user_text: str) -> str:
        """Return the message of the InvalidSettingsError the text provokes."""
        with self.assertRaises(InvalidSettingsError, msg="invalid settings") as caught:
            load_shipped(user_text)
        return str(caught.exception)

    def test_the_shipped_providers(self) -> None:
        """Both providers are declared; effort and enabled come from the template."""
        providers = load_shipped().providers
        self.assertEqual(list(providers), ["openai", "anthropic"], msg="declared, in order")
        openai, anthropic = providers["openai"], providers["anthropic"]
        self.assertEqual(
            (openai.enabled, openai.implementation, openai.model, openai.effort),
            (True, "builtin.codex", "gpt-6.1-sol", "medium"),
            msg="openai",
        )
        self.assertEqual(
            (anthropic.enabled, anthropic.implementation, anthropic.model, anthropic.effort),
            (True, "builtin.claude", "claude-sonnet-5-5", "medium"),
            msg="anthropic",
        )

    def test_a_user_file_disables_one_provider(self) -> None:
        """Switch a single provider off with its own key."""
        providers = load_shipped("[providers.anthropic]\nenabled = false\n").providers
        self.assertFalse(providers["anthropic"].enabled, msg="anthropic off")
        self.assertTrue(providers["openai"].enabled, msg="openai unchanged")

    def test_the_template_value_reaches_every_provider_without_its_own(self) -> None:
        """providers._template.enabled and .effort are inherited."""
        providers = load_shipped(
            '[providers._template]\nenabled = false\neffort = "high"\n'
            '[providers.openai]\neffort = "low"\n'
        ).providers
        self.assertFalse(providers["anthropic"].enabled, msg="inherited enabled")
        self.assertEqual(providers["anthropic"].effort, "high", msg="inherited effort")
        self.assertEqual(providers["openai"].effort, "low", msg="own effort wins")

    def test_a_provider_can_change_its_model(self) -> None:
        """providers.<id>.model."""
        providers = load_shipped('[providers.anthropic]\nmodel = "opus"\n').providers
        self.assertEqual(providers["anthropic"].model, "opus", msg="model")

    def test_a_provider_that_is_not_declared_cannot_be_added(self) -> None:
        """Names cannot register providers (ARCH L1568)."""
        self.assertIn(
            "providers.third.model", self.refused('[providers.third]\nmodel = "x"\n'), msg="key"
        )

    def test_bad_provider_values_name_their_key(self) -> None:
        """Each field has its own validator."""
        cases = {
            "providers.anthropic.enabled": "[providers.anthropic]\nenabled = 1\n",
            "providers.anthropic.model": '[providers.anthropic]\nmodel = "-x"\n',
            "providers.anthropic.effort": '[providers.anthropic]\neffort = "huge"\n',
            "providers.anthropic.implementation": '[providers.anthropic]\nimplementation = ""\n',
        }
        for key, text in cases.items():
            with self.subTest(key=key):
                self.assertIn(key, self.refused(text), msg="the key is named")


class ChainSettingsTests(unittest.TestCase):
    """The default chain, task chains and link attributes (ARCH L3523, L3533)."""

    def refused(self, user_text: str) -> str:
        """Return the message of the InvalidSettingsError the text provokes."""
        with self.assertRaises(InvalidSettingsError, msg="invalid settings") as caught:
            load_shipped(user_text)
        return str(caught.exception)

    def test_the_default_chain_ships_as_the_architecture_has_it(self) -> None:
        """openai, anthropic, then a handoff (ARCH L3533)."""
        self.assertEqual(
            load_shipped().default_chain.links,
            (ProviderLink("openai"), ProviderLink("anthropic"), HandoffLink()),
            msg="defaults.chain",
        )

    def test_the_shipped_task_types_inherit_the_default_chain(self) -> None:
        """No chain of their own."""
        tasks = load_shipped().tasks
        self.assertIsNone(tasks["audit"].chain, msg="audit inherits")
        self.assertIsNone(tasks["implement"].chain, msg="implement inherits")

    def test_a_user_file_replaces_a_task_chain_with_link_attributes(self) -> None:
        """A link may carry model and effort."""
        text = (
            '[tasks.audit]\nmode = "read"\n'
            'chain = [{ provider = "anthropic", model = "opus", effort = "high" }]\n'
        )
        tasks = load_shipped(text).tasks
        self.assertEqual(
            tasks["audit"].chain, Chain((ProviderLink("anthropic", "opus", "high"),)), msg="chain"
        )
        self.assertIsNone(tasks["implement"].chain, msg="the other task is untouched")

    def test_a_user_file_replaces_the_default_chain(self) -> None:
        """defaults.chain is a user key like any other."""
        text = '[defaults]\nchain = [{ provider = "anthropic" }]\n'
        self.assertEqual(
            load_shipped(text).default_chain, Chain((ProviderLink("anthropic"),)), msg="replaced"
        )

    def test_a_bad_chain_names_its_key(self) -> None:
        """The same rules for the default chain and a task chain."""
        bad = {
            "empty": "chain = []",
            "duplicate": 'chain = [{ provider = "anthropic" }, { provider = "anthropic" }]',
            "handoff first": 'chain = [{ handoff = true }, { provider = "anthropic" }]',
            "undeclared provider": 'chain = [{ provider = "third" }]',
            "unknown link key": 'chain = [{ provider = "anthropic", speed = "fast" }]',
            "link without provider": 'chain = [{ model = "opus" }]',
            "handoff false": "chain = [{ handoff = false }]",
            "not a list": 'chain = "anthropic"',
            "bad model": 'chain = [{ provider = "anthropic", model = "-x" }]',
            "bad effort": 'chain = [{ provider = "anthropic", effort = "huge" }]',
            "bad provider id": 'chain = [{ provider = "Anthropic" }]',
        }
        for label, line in bad.items():
            with self.subTest(label=label, scope="defaults"):
                self.assertIn("defaults.chain", self.refused(f"[defaults]\n{line}\n"), msg=label)
            with self.subTest(label=label, scope="task"):
                text = f'[tasks.review]\nmode = "read"\n{line}\n'
                self.assertIn("tasks.review.chain", self.refused(text), msg=label)


class QuotaAndCapacitySettingsTests(unittest.TestCase):
    """The quota, bucket and capacity keys of ARCH L3542-L3544, L3551, L3562 and L3642."""

    def test_the_shipped_values_follow_the_catalogue(self) -> None:
        """Best effort, reserve 15, max age 30 s, bucket codex, capacity 5 per provider."""
        settings = load_shipped()
        self.assertEqual(settings.quota_mode, "best_effort", msg="defaults.quota_mode")
        self.assertEqual(settings.quota_reserve_percent, 15, msg="defaults.quota_reserve_percent")
        self.assertEqual(settings.quota_max_age_s, 30.0, msg="defaults.quota_max_age_s")
        self.assertEqual(settings.bound_buckets, {"openai": "codex"}, msg="bound_bucket")
        self.assertEqual(
            {name: provider.capacity for name, provider in settings.providers.items()},
            {"openai": 5, "anthropic": 5},
            msg="providers._template.capacity",
        )

    def test_a_user_file_can_change_each_key(self) -> None:
        """One provider can differ from the template."""
        settings = load_shipped(
            '[defaults]\nquota_mode = "required"\nquota_reserve_percent = 20\n'
            'quota_max_age_s = "5"\n[providers.openai]\ncapacity = 1\n'
            '[profiles.openai.main]\nbound_bucket = "other"\n'
        )
        self.assertEqual(settings.quota_mode, "required", msg="quota_mode override")
        self.assertEqual(settings.quota_reserve_percent, 20, msg="reserve override")
        self.assertEqual(settings.quota_max_age_s, 5.0, msg="max age override")
        self.assertEqual(settings.providers["openai"].capacity, 1, msg="provider capacity")
        self.assertEqual(settings.providers["anthropic"].capacity, 5, msg="template capacity")
        self.assertEqual(settings.bound_buckets["openai"], "other", msg="bucket override")

    def test_bad_values_are_refused_naming_the_key(self) -> None:
        """Each key has its own shape."""
        bad = {
            "defaults.quota_mode": '[defaults]\nquota_mode = "sometimes"\n',
            "defaults.quota_reserve_percent": "[defaults]\nquota_reserve_percent = 100\n",
            "defaults.quota_max_age_s": '[defaults]\nquota_max_age_s = "0"\n',
            "providers._template.capacity": "[providers._template]\ncapacity = 0\n",
            "providers.openai.capacity": '[providers.openai]\ncapacity = "five"\n',
            "profiles.openai.main.bound_bucket": '[profiles.openai.main]\nbound_bucket = "a b"\n',
            "proxy.providers.openai.hosts": "[proxy.providers.openai]\nhosts = []\n",
        }
        for key, text in bad.items():
            with self.subTest(key=key), self.assertRaises(SettingsError, msg=key) as caught:
                load_shipped(text)
            self.assertIn(key, str(caught.exception), msg=f"names {key}")


class ProviderToolAndStateSettingsTests(unittest.TestCase):
    """Each provider has its own tool and its own login state (D-1 of MEASURE-9, ARCH L3633)."""

    def test_a_provider_can_override_the_tool_of_the_template(self) -> None:
        """`profiles.<id>.main.executable` replaces the shared `auto` for that provider only."""
        settings = load_shipped('[profiles.openai.main]\nexecutable = "/opt/tools/codex"\n')
        self.assertEqual(
            settings.executables,
            {"anthropic": "auto", "openai": "/opt/tools/codex"},
            msg="only openai changed",
        )

    def test_a_relative_tool_path_is_refused_naming_the_key(self) -> None:
        """Same rule as the template key: `auto` or an absolute path."""
        with self.assertRaises(SettingsError, msg="relative") as caught:
            load_shipped('[profiles.openai.main]\nexecutable = "codex"\n')
        self.assertIn("profiles.openai.main.executable", str(caught.exception), msg="names key")

    def test_each_provider_lists_its_own_login_state(self) -> None:
        """Claude Code: its folder and its json file; Codex: its folder."""
        settings = load_shipped()
        self.assertEqual(
            settings.vendor_state,
            {
                "anthropic": (f"{HOME}/.claude", f"{HOME}/.claude.json"),
                "openai": (f"{HOME}/.codex",),
            },
            msg="sandbox.vendor_state.<provider>",
        )
        self.assertEqual(
            settings.other_vendor_state,
            (
                f"{HOME}/.cursor",
                f"{HOME}/.gemini",
                f"{HOME}/.config/github-copilot",
            ),
            msg="third-party tools stay denied for every provider",
        )

    def test_a_vendor_state_list_for_an_undeclared_provider_is_refused(self) -> None:
        """A typo cannot silently open or close a folder."""
        with self.assertRaises(SettingsError, msg="unknown provider") as caught:
            load_shipped('[sandbox.vendor_state]\nmistral = ["@account-home/.mistral"]\n')
        self.assertIn("sandbox.vendor_state.mistral", str(caught.exception), msg="names key")


class InspectorSettingsTests(unittest.TestCase):
    """The limits of the confined account inspector come from settings, not from code."""

    def test_the_shipped_limits(self) -> None:
        """Twenty seconds and one GiB for a `login status` style command."""
        settings = load_shipped()
        self.assertEqual(settings.inspector_timeout_s, 20.0, msg="inspector.timeout_s")
        self.assertEqual(
            settings.inspector_memory_bytes, 1_073_741_824, msg="inspector.memory_bytes"
        )

    def test_a_bad_limit_is_refused_naming_the_key(self) -> None:
        """A number where seconds must be quoted, and a non-positive size."""
        for text, key in (
            ("[inspector]\ntimeout_s = 20\n", "inspector.timeout_s"),
            ("[inspector]\nmemory_bytes = 0\n", "inspector.memory_bytes"),
        ):
            with self.subTest(key=key), self.assertRaises(SettingsError, msg=key) as caught:
                load_shipped(text)
            self.assertIn(key, str(caught.exception), msg="names the key")


class McpSettingsTests(unittest.TestCase):
    """The limits of the MCP server come from settings, not from code."""

    def test_the_shipped_values(self) -> None:
        """Every key carries its documented default."""
        mcp = load_shipped().mcp
        self.assertEqual(mcp.transport, "own", msg="mcp.transport")
        self.assertEqual(
            (mcp.frame_in_bytes, mcp.frame_out_bytes, mcp.frame_depth, mcp.frame_nodes),
            (8_388_608, 4_194_304, 32, 100_000),
            msg="mcp.frame.*",
        )
        self.assertEqual(mcp.numeric_token_bytes, 128, msg="mcp.frame.numeric_token_bytes")
        self.assertEqual((mcp.ids_maximum, mcp.string_bytes), (65_536, 128), msg="mcp.ids.*")
        self.assertEqual(
            (mcp.normal_workers, mcp.normal_queue, mcp.control_workers, mcp.control_queue),
            (6, 12, 1, 4),
            msg="mcp.normal.* and mcp.control.*",
        )
        self.assertEqual(
            (
                mcp.initialize_timeout_s,
                mcp.partial_frame_timeout_s,
                mcp.write_timeout_s,
                mcp.shutdown_drain_s,
                mcp.shutdown_total_s,
            ),
            (10.0, 5.0, 2.0, 2.0, 5.0),
            msg="mcp timeouts",
        )
        self.assertEqual(mcp.priority_burst, 8, msg="mcp.priority_burst")
        self.assertEqual(mcp.wait_max_s, 20.0, msg="mcp.wait_max_s")
        self.assertEqual(mcp.aggregate_bytes, 33_554_432, msg="mcp.input.aggregate_bytes")

    def test_a_user_value_replaces_the_default(self) -> None:
        """A key under `[mcp]` is read."""
        mcp = load_shipped('[mcp]\nwait_max_s = "7"\n[mcp.normal]\nworkers = 2\n').mcp
        self.assertEqual(mcp.wait_max_s, 7.0, msg="mcp.wait_max_s")
        self.assertEqual(mcp.normal_workers, 2, msg="mcp.normal.workers")

    def test_a_bad_value_is_refused_naming_the_key(self) -> None:
        """Wrong type, bound or name."""
        for text, key in (
            ('[mcp]\ntransport = "pipe"\n', "mcp.transport"),
            ("[mcp]\nwait_max_s = 20\n", "mcp.wait_max_s"),
            ("[mcp.frame]\nin_bytes = 0\n", "mcp.frame.in_bytes"),
            ("[mcp.control]\nworkers = 0\n", "mcp.control.workers"),
            ('[mcp]\nshutdown_total_s = "0"\n', "mcp.shutdown_total_s"),
            ("[mcp]\npriority_burst = true\n", "mcp.priority_burst"),
        ):
            with self.subTest(key=key), self.assertRaises(SettingsError, msg=key) as caught:
                load_shipped(text)
            self.assertIn(key, str(caught.exception), msg="names the key")

    def test_the_drain_cannot_outlast_the_total(self) -> None:
        """Draining is part of shutting down."""
        with self.assertRaises(SettingsError, msg="drain above total") as caught:
            load_shipped('[mcp]\nshutdown_drain_s = "6"\n')
        self.assertIn("mcp.shutdown_drain_s", str(caught.exception), msg="names the key")


class ProcessObservationSettingsTests(unittest.TestCase):
    """The clocks and bounds of the process observation of a run (O1 layer 3)."""

    def test_the_shipped_values(self) -> None:
        """Process look every second, path look every two, two empty rounds out of eight."""
        observation = load_shipped().observation
        self.assertEqual(
            (observation.process_interval_s, observation.process_max_age_s),
            (1.0, 2.0),
            msg="process.observation.process_*",
        )
        self.assertEqual(
            (observation.path_interval_s, observation.path_max_age_s),
            (2.0, 5.0),
            msg="process.observation.path_*",
        )
        self.assertEqual(
            (observation.empty_rounds, observation.cleanup_rounds),
            (2, 8),
            msg="process.observation.*_rounds",
        )

    def test_a_user_value_replaces_the_default(self) -> None:
        """A key under `[process.observation]` is read."""
        text = '[process.observation]\nprocess_interval_s = "0.5"\nempty_rounds = 3\n'
        observation = load_shipped(text).observation
        self.assertEqual(observation.process_interval_s, 0.5, msg="process_interval_s")
        self.assertEqual(observation.empty_rounds, 3, msg="empty_rounds")

    def test_a_bad_value_is_refused_naming_the_key(self) -> None:
        """Wrong type or bound."""
        for text, key in (
            (
                "[process.observation]\nprocess_interval_s = 1\n",
                "process.observation.process_interval_s",
            ),
            ("[process.observation]\nempty_rounds = 0\n", "process.observation.empty_rounds"),
            (
                "[process.observation]\ncleanup_rounds = true\n",
                "process.observation.cleanup_rounds",
            ),
        ):
            with self.subTest(key=key), self.assertRaises(SettingsError, msg=key) as caught:
                load_shipped(text)
            self.assertIn(key, str(caught.exception), msg="names the key")

    def test_the_cleanup_cannot_stop_before_it_can_see_empty(self) -> None:
        """Fewer rounds than empty rounds could never end as observed empty."""
        text = "[process.observation]\nempty_rounds = 4\ncleanup_rounds = 3\n"
        with self.assertRaises(SettingsError, msg="rounds below empty rounds") as caught:
            load_shipped(text)
        self.assertIn(
            "process.observation.cleanup_rounds", str(caught.exception), msg="names the key"
        )

    def test_a_look_cannot_be_older_than_it_is_allowed_to_be_taken(self) -> None:
        """A maximum age below the interval would make every look stale before the next one."""
        text = '[process.observation]\nprocess_interval_s = "3"\nprocess_max_age_s = "2"\n'
        with self.assertRaises(SettingsError, msg="age below interval") as caught:
            load_shipped(text)
        self.assertIn(
            "process.observation.process_max_age_s", str(caught.exception), msg="names the key"
        )
