"""Tests of the task resolver: a task type name becomes a mode and a chain, or an error."""

import unittest

from agents_dispatcher.application.errors import UnknownTaskTypeError, WriteTaskNotAllowedError
from agents_dispatcher.application.routing import TaskResolver
from agents_dispatcher.application.settings import TaskDefinition
from agents_dispatcher.domain.routing import Chain, HandoffLink, ProviderLink, TaskMode

DEFAULT_CHAIN = Chain((ProviderLink("alpha"), ProviderLink("beta"), HandoffLink()))
OWN_CHAIN = Chain((ProviderLink("beta"),))


def make_resolver(
    *, default_mode: TaskMode = TaskMode.READ, allowed: tuple[str, ...] = ("build",)
) -> TaskResolver:
    """Return a resolver over two declared task types."""
    return TaskResolver(
        tasks={
            "review": TaskDefinition(TaskMode.READ),
            "special": TaskDefinition(TaskMode.READ, OWN_CHAIN),
            "build": TaskDefinition(TaskMode.WRITE),
            "forbidden": TaskDefinition(TaskMode.WRITE),
        },
        default_mode=default_mode,
        default_chain=DEFAULT_CHAIN,
        allow_write_tasks=allowed,
    )


class ResolveTests(unittest.TestCase):
    """What a task type resolves to."""

    def test_no_task_type_resolves_from_the_defaults(self) -> None:
        """Mode of the defaults, chain of the defaults, no task type name."""
        task = make_resolver().resolve(None)
        self.assertEqual(
            (task.task_type, task.mode, task.chain),
            (None, TaskMode.READ, DEFAULT_CHAIN),
            msg="the defaults",
        )

    def test_a_task_type_inherits_the_default_chain_and_may_replace_it(self) -> None:
        """A task without a chain of its own walks the default one."""
        resolver = make_resolver()
        self.assertEqual(resolver.resolve("review").chain, DEFAULT_CHAIN, msg="inherited")
        self.assertEqual(resolver.resolve("special").chain, OWN_CHAIN, msg="replaced")

    def test_an_allowed_write_task_is_resolved(self) -> None:
        """Only the allow-list admits a write task."""
        task = make_resolver().resolve("build")
        self.assertEqual((task.task_type, task.mode), ("build", TaskMode.WRITE), msg="write task")

    def test_an_unknown_task_type_is_refused_naming_it(self) -> None:
        """The caller learns which name no settings declare."""
        with self.assertRaises(UnknownTaskTypeError, msg="unknown") as caught:
            make_resolver().resolve("nope")
        self.assertIn("nope", str(caught.exception), msg="names the task type")

    def test_a_write_task_that_is_not_allowed_is_refused_naming_the_key(self) -> None:
        """Authority comes from security.allow_write_tasks, not from the task's own table."""
        with self.assertRaises(WriteTaskNotAllowedError, msg="not allowed") as caught:
            make_resolver().resolve("forbidden")
        message = str(caught.exception)
        self.assertIn("forbidden", message, msg="names the task type")
        self.assertIn("security.allow_write_tasks", message, msg="names the key")

    def test_a_write_default_is_not_a_write_permission(self) -> None:
        """Without a task type there is no name to find in the allow-list."""
        with self.assertRaises(WriteTaskNotAllowedError, msg="write default"):
            make_resolver(default_mode=TaskMode.WRITE).resolve(None)
