import os
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import lib.runner as runner
from lib.runner import (
    canonical_repository_identity,
    deduplicate_repositories,
    acquire_repository_locks,
    release_repository_locks,
    terminate_process_group,
    stop_nginx_and_workers,
    build_catalog,
    install_landing_page,
)


class RepositoryDeduplicationTests(unittest.TestCase):
    def test_duplicate_names_keep_the_first_config_entry(self):
        repositories = [
            {"name": "docs", "git": "https://example.com/first.git", "revision": "main"},
            {"name": "docs", "git": "https://example.com/second.git", "revision": "main"},
        ]

        self.assertEqual(deduplicate_repositories(repositories), repositories[:1])

    def test_different_names_for_same_remote_keep_one_entry(self):
        repositories = [
            {"name": "docs", "git": "https://token@example.com/team/project.git", "revision": "main"},
            {"name": "project-docs", "git": "https://example.com/team/project", "revision": "main"},
        ]

        self.assertEqual(len(deduplicate_repositories(repositories)), 2)
        self.assertEqual(
            canonical_repository_identity(repositories[0]["git"]),
            canonical_repository_identity(repositories[1]["git"]),
        )

    def test_identity_preserves_case_sensitive_path(self):
        self.assertNotEqual(
            canonical_repository_identity("https://github.com/Team/Project.git"),
            canonical_repository_identity("https://github.com/team/project.git"),
        )

    def test_identity_normalizes_https_ssh_username_and_ports(self):
        remotes = [
            "https://token@github.com:443/team/project.git",
            "ssh://git@github.com:22/team/project",
            "git@github.com:team/project.git",
        ]
        identities = {canonical_repository_identity(remote) for remote in remotes}
        self.assertEqual(len(identities), 1)


class ProcessTerminationTests(unittest.TestCase):
    def test_termination_waits_until_worker_has_exited(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )

        terminate_process_group(process, grace_seconds=0.2)

        self.assertIsNotNone(process.poll())

    def test_termination_kills_child_processes_too(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            child_pid_path = Path(temp_dir) / "child.pid"
            child_code = (
                "import os, time; "
                f"open({str(child_pid_path)!r}, 'w').write(str(os.getpid())); "
                "time.sleep(30)"
            )
            parent_code = (
                "import subprocess, sys, time; "
                f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
                "time.sleep(30)"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", parent_code],
                start_new_session=True,
            )
            deadline = time.monotonic() + 5
            while not child_pid_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(child_pid_path.exists(), "child did not start")
            child_pid = int(child_pid_path.read_text())

            terminate_process_group(process, grace_seconds=0.2)

            self.assertIsNotNone(process.poll())
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                pass
            else:
                self.fail("child process is still running")


class RepositoryLockTests(unittest.TestCase):
    def test_same_remote_cannot_be_locked_by_two_repository_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first = {"name": "docs", "git": "git@github.com:team/project.git"}
            second = {"name": "project-docs", "git": "https://github.com/team/project"}

            first_locks = acquire_repository_locks(first, Path(temp_dir), Path("/repos"))
            second_locks = acquire_repository_locks(second, Path(temp_dir), Path("/repos"))

            self.assertIsNotNone(first_locks)
            self.assertIsNone(second_locks)
            release_repository_locks(first_locks)

            second_locks = acquire_repository_locks(second, Path(temp_dir), Path("/repos"))
            self.assertIsNotNone(second_locks)
            release_repository_locks(second_locks)

    def test_lock_remains_held_after_manager_hands_it_to_worker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = {"name": "docs", "git": "https://github.com/team/project.git"}
            locks = acquire_repository_locks(repo, Path(temp_dir), Path("/repos"))
            self.assertIsNotNone(locks)
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(0.3)"],
                start_new_session=True,
                pass_fds=tuple(handle.fileno() for handle in locks),
            )
            for handle in locks:
                handle.close()

            self.assertIsNone(
                acquire_repository_locks(repo, Path(temp_dir), Path("/repos"))
            )
            process.wait(timeout=5)
            next_locks = acquire_repository_locks(
                repo, Path(temp_dir), Path("/repos")
            )
            self.assertIsNotNone(next_locks)
            release_repository_locks(next_locks)


class PollCycleTests(unittest.TestCase):
    def test_poll_interval_does_not_start_second_worker_for_active_repo(self):
        repo = {
            "name": "docs",
            "git": "https://github.com/team/project.git",
            "revision": "main",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            running = {}
            source_locks = {}
            runner.stop_event.clear()

            def fake_launch(*_args):
                result_path = root / f"result-{len(running)}"
                process = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    start_new_session=True,
                )
                return process, result_path

            with patch.object(runner, "launch_worker", side_effect=fake_launch) as launch:
                args = {
                    "repositories": [repo],
                    "repo_dir": root / "repos",
                    "site_dir": root / "sites",
                    "base_url": "https://docs.example.test",
                    "max_workers": 2,
                    "build_timeout": 30,
                    "lock_dir": root / "locks",
                    "running": running,
                    "source_locks": source_locks,
                    "start_new_builds": True,
                }
                runner.run_poll_cycle(**args)
                runner.run_poll_cycle(**args)

                self.assertEqual(launch.call_count, 1)

                stop_nginx_and_workers(running, source_locks, None)

    def test_reaped_worker_updates_last_built_and_logs_build_once(self):
        repo = {
            "name": "docs",
            "git": "https://github.com/team/project.git",
            "revision": "main",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result_path = root / "result.json"
            result_path.write_text(
                json.dumps({"revision": "abc123", "built": True})
            )
            process = subprocess.Popen([sys.executable, "-c", "pass"])
            process.wait(timeout=5)
            running = {"docs": (process, result_path, time.monotonic(), [], "remote")}
            source_locks = {"remote": "docs"}
            logs = []
            runner.stop_event.clear()

            with (
                patch.object(runner, "logger") as logger,
                patch.object(runner, "write_catalog"),
            ):
                runner.run_poll_cycle(
                    repositories=[repo],
                    repo_dir=root / "repos",
                    site_dir=root / "sites",
                    base_url="https://docs.example.test",
                    max_workers=1,
                    build_timeout=30,
                    lock_dir=root / "locks",
                    running=running,
                    source_locks=source_locks,
                    start_new_builds=False,
                )
                logs = [call.args[0] for call in logger.info.call_args_list]

            self.assertEqual(runner.last_built["docs"], "abc123")
            self.assertEqual(
                sum("Successfully built" in message for message in logs),
                1,
            )


class CatalogTests(unittest.TestCase):
    def test_installs_landing_page_into_site_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            site_dir = Path(temp_dir)

            install_landing_page(site_dir)

            self.assertIn("Your engineering wiki", (site_dir / "index.html").read_text())

    def test_catalog_includes_only_docs_with_published_index(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            site_dir = Path(temp_dir)
            (site_dir / "ready").mkdir()
            (site_dir / "ready" / "index.html").write_text("published")
            repositories = [
                {"name": "ready", "description": "Published guide"},
                {"name": "pending", "description": "Not built yet"},
            ]

            catalog = build_catalog(repositories, site_dir)

            self.assertEqual(
                catalog,
                [{
                    "name": "ready",
                    "label": "ready",
                    "url": "/ready/",
                    "description": "Published guide",
                }],
            )

            runner.write_catalog(repositories, site_dir)
            self.assertEqual(
                json.loads((site_dir / "catalog.json").read_text()),
                catalog,
            )

    def test_catalog_uses_label_and_description_with_fallbacks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            site_dir = Path(temp_dir)
            for name in ("custom", "fallback"):
                (site_dir / name).mkdir()
                (site_dir / name / "index.html").write_text("published")

            catalog = build_catalog(
                [
                    {
                        "name": "custom",
                        "label": "Platform Handbook",
                        "description": "Guides for building our platform.",
                    },
                    {"name": "fallback"},
                ],
                site_dir,
            )

            self.assertEqual(catalog[0]["label"], "Platform Handbook")
            self.assertEqual(catalog[0]["description"], "Guides for building our platform.")
            self.assertEqual(catalog[1]["label"], "fallback")
            self.assertEqual(
                catalog[1]["description"],
                "Documentation maintained from fallback.",
            )
            runner.write_catalog(
                [
                    {
                        "name": "custom",
                        "label": "Platform Handbook",
                        "description": "Guides for building our platform.",
                    },
                    {"name": "fallback"},
                ],
                site_dir,
            )
            self.assertEqual(
                json.loads((site_dir / "catalog.json").read_text()),
                catalog,
            )

    def test_worker_syncs_repository_in_worker_process(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = Path(temp_dir) / "result.json"
            payload = {
                "repo": {"name": "docs"},
                "repo_dir": temp_dir,
                "site_dir": temp_dir,
                "base_url": "https://docs.example.test",
                "lock_dir": temp_dir,
                "last_built": None,
                "result_path": str(result_path),
            }

            with (
                patch.object(
                    runner,
                    "sync_repository",
                    return_value=(Path(temp_dir), "abc123"),
                ) as sync,
                patch.object(runner, "build_repository") as build,
            ):
                result = runner.build_worker(payload)

            self.assertEqual(result, 0)
            sync.assert_called_once_with(payload["repo"], Path(temp_dir))
            build.assert_called_once()
            self.assertEqual(
                json.loads(result_path.read_text()),
                {"revision": "abc123", "built": True},
            )


if __name__ == "__main__":
    unittest.main()
