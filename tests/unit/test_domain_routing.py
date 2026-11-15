"""Tests of the routing domain: names, task modes, provider links and the validated chain."""

import unittest

from agents_dispatcher.domain.routing import (
    Chain,
    HandoffLink,
    ProviderLink,
    TaskMode,
    validate_name,
)


class NameTests(unittest.TestCase):
    """Task type names and provider ids share one grammar."""

    def test_lowercase_digits_and_hyphens_are_accepted(self) -> None:
        """A letter first, then letters, digits and hyphens."""
        for name in ("audit", "a", "security-review-2", "a" * 32):
            with self.subTest(name=name):
                self.assertEqual(validate_name(name, "task type"), name, msg="accepted as is")

    def test_everything_else_is_refused(self) -> None:
        """Uppercase, underscore, leading digit or hyphen, empty, too long, spaces, newline."""
        for name in ("Audit", "_template", "1a", "-a", "", "a" * 33, "a b", "a\n", "a_b", "é"):
            with self.subTest(name=name), self.assertRaises(ValueError, msg=repr(name)):
                validate_name(name, "task type")

    def test_the_message_names_what_was_checked(self) -> None:
        """The caller learns which name was refused."""
        with self.assertRaises(ValueError, msg="refused") as caught:
            validate_name("Bad", "task type")
        self.assertIn("task type", str(caught.exception), msg="names the kind of name")


class TaskModeTests(unittest.TestCase):
    """The two modes carry the words the settings use."""

    def test_values_are_the_settings_words(self) -> None:
        """`read` and `write`, nothing else."""
        self.assertEqual([mode.value for mode in TaskMode], ["read", "write"], msg="two modes")


class ChainTests(unittest.TestCase):
    """A chain is nonempty, has unique providers and a handoff only at its end."""

    def test_providers_then_handoff_is_valid(self) -> None:
        """The architecture default shape."""
        links = (ProviderLink("openai"), ProviderLink("anthropic"), HandoffLink())
        self.assertEqual(Chain(links).links, links, msg="kept in order")

    def test_a_chain_without_a_terminal_link_is_valid(self) -> None:
        """A terminal link is allowed, not required."""
        self.assertEqual(len(Chain((ProviderLink("anthropic"),)).links), 1, msg="one link")

    def test_link_attributes_are_kept(self) -> None:
        """Model and effort are link attributes."""
        link = ProviderLink("anthropic", model="m-1", effort="high")
        self.assertEqual((link.model, link.effort), ("m-1", "high"), msg="attributes kept")

    def test_an_empty_chain_is_refused(self) -> None:
        """Nonempty."""
        with self.assertRaises(ValueError, msg="empty") as caught:
            Chain(())
        self.assertIn("empty", str(caught.exception), msg="says why")

    def test_a_duplicate_provider_is_refused(self) -> None:
        """One link per provider."""
        with self.assertRaises(ValueError, msg="duplicate") as caught:
            Chain((ProviderLink("anthropic"), ProviderLink("anthropic", model="m")))
        self.assertIn("anthropic", str(caught.exception), msg="names the provider")

    def test_a_handoff_that_is_not_last_is_refused(self) -> None:
        """Handoff only last."""
        with self.assertRaises(ValueError, msg="handoff first") as caught:
            Chain((HandoffLink(), ProviderLink("anthropic")))
        self.assertIn("handoff", str(caught.exception), msg="names the handoff")

    def test_two_handoffs_are_refused(self) -> None:
        """The first of two is not last."""
        with self.assertRaises(ValueError, msg="two handoffs"):
            Chain((ProviderLink("anthropic"), HandoffLink(), HandoffLink()))

    def test_a_provider_link_validates_its_id(self) -> None:
        """The id obeys the name grammar."""
        with self.assertRaises(ValueError, msg="bad id"):
            ProviderLink("Anthropic")
