import fcntl
import hashlib
import importlib
import json
import logging
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit

import yaml
from mkdocs.commands.build import build
from mkdocs.config import load_config

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/etc/wiki.yaml"))
LANDING_PAGE_TEMPLATE = Path(__file__).resolve().parent.parent / "site" / "index.html"
DEFAULT_POLL_INTERVAL_SECONDS = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("wiki-runner")

# Ensure MkDocs internal build logs are visible and bubble up
mkdocs_logger = logging.getLogger("mkdocs")
mkdocs_logger.setLevel(logging.INFO)

# Shutdown is owned by the manager process. Workers are separate processes so
# the manager can stop them reliably when they time out or receive SIGTERM.
stop_event = threading.Event()
last_built: dict[str, str] = {}

# Default build timeout in seconds (10 minutes)
DEFAULT_BUILD_TIMEOUT_SECONDS = 600

# Default max concurrent builds
DEFAULT_MAX_CONCURRENT_BUILDS = 4


def handle_shutdown(signum, _frame):
    signame = signal.Signals(signum).name
    logger.info("Received %s; stopping repository workers...", signame)
    stop_event.set()


def is_stop_requested() -> bool:
    return stop_event.is_set()


def git(checkout_dir: Path, *args: str) -> str:
    """Run Git without logging its arguments, which could contain credentials."""
    result = subprocess.run(
        ["git", "-C", str(checkout_dir), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # Avoid logging raw stderr: it may contain a credential-bearing URL.
        raise RuntimeError(f"Git command failed with exit code {result.returncode}")
    return result.stdout.strip()


def canonical_repository_identity(remote: str) -> str:
    """Normalize common HTTPS/SSH Git URL forms into a lock identity."""
    value = remote.strip()
    if "://" in value:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        path = parsed.path.lstrip("/")
        if parsed.port is not None:
            is_default_port = (
                parsed.scheme.lower() == "https" and parsed.port == 443
            ) or (parsed.scheme.lower() == "ssh" and parsed.port == 22)
            if not is_default_port:
                host = f"{host}:{parsed.port}"
    elif "@" in value:
        host_part, path = value.split("@", 1)[1].split(":", 1)
        host = host_part.split(":", 1)[0]
    elif ":" in value:
        host, path = value.split(":", 1)
    else:
        host, path = "", value

    path = path.rstrip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    return f"{host.lower()}/{path}"


def acquire_repository_locks(
    repo: dict,
    lock_dir: Path,
    repo_dir: Path,
) -> list[TextIO] | None:
    """Acquire nonblocking locks for both checkout name and normalized remote."""
    lock_dir.mkdir(parents=True, exist_ok=True)
    identity = canonical_repository_identity(repo["git"])
    checkout_identity = str((repo_dir / repo["name"]).resolve())
    keys = sorted({f"remote:{identity}", f"checkout:{checkout_identity}"})
    handles = []
    try:
        for key in keys:
            filename = hashlib.sha256(key.encode("utf-8")).hexdigest() + ".lock"
            handle = (lock_dir / filename).open("a")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                raise
            handles.append(handle)
        return handles
    except BlockingIOError:
        release_repository_locks(handles)
        return None
    except Exception:
        release_repository_locks(handles)
        raise


def release_repository_locks(handles: list[TextIO] | None) -> None:
    """Release file-lock handles acquired for a worker."""
    if handles is None:
        return
    for handle in reversed(handles):
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def terminate_process_group(
    proc: subprocess.Popen,
    grace_seconds: float = 10,
) -> None:
    """Terminate a worker and its child processes, then reap it."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        if proc.poll() is not None:
            return

    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        logger.warning("Worker pid=%s ignored SIGTERM; escalating to SIGKILL", proc.pid)

    # Kill group members even if the worker exited first; children may still run.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # On platforms without usable process-group signaling, kill the worker.
        if proc.poll() is None:
            proc.kill()
    proc.wait()


def sync_repository(repo: dict, repo_dir: Path) -> tuple[Path, str]:
    """Sync repository and return (checkout_dir, revision)."""
    name = repo["name"]
    checkout_dir = repo_dir / name

    # Keep repository names as direct child directory names.
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError(f"Invalid repository name: {name!r}")

    if not checkout_dir.exists():
        checkout_dir.mkdir(parents=True)
        git(checkout_dir, "init", "--quiet")
    elif not (checkout_dir / ".git").exists():
        if any(checkout_dir.iterdir()):
            raise RuntimeError(
                f"{checkout_dir} exists but is not an empty Git checkout"
            )
        git(checkout_dir, "init", "--quiet")

    remote = subprocess.run(
        ["git", "-C", str(checkout_dir), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )

    if remote.returncode == 0:
        git(checkout_dir, "remote", "set-url", "origin", repo["git"])
    else:
        git(checkout_dir, "remote", "add", "origin", repo["git"])

    # Check for stop signal before fetch
    if is_stop_requested():
        raise InterruptedError("Shutdown requested during sync")

    # Fetch only the configured ref and its tip commit.
    git(
        checkout_dir,
        "fetch",
        "--depth=1",
        "--force",
        "--no-tags",
        "origin",
        repo["revision"],
    )
    revision = git(checkout_dir, "rev-parse", "FETCH_HEAD^{commit}")
    return checkout_dir, revision


def install_requirements(requirements_file: Path, lock_dir: Path) -> None:
    """Install pip requirements under a cross-process lock."""
    logger.info("Installing requirements from %s", requirements_file)
    import fcntl

    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "pip-install.lock"
    with lock_path.open("w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-cache-dir",
                "-r",
                str(requirements_file),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        if proc.stdout:
            for line in proc.stdout:
                cleaned = line.rstrip("\r\n")
                if cleaned:
                    logger.info("[pip] %s", cleaned)

        returncode = proc.wait()
        if returncode != 0:
            logger.error(
                "pip install failed for %s (exit code %s)",
                requirements_file,
                returncode,
            )
            raise RuntimeError(
                f"Failed to install dependencies from {requirements_file}"
            )
    importlib.invalidate_caches()
    logger.info("Successfully installed requirements from %s", requirements_file)


def build_repository(
    repo: dict,
    checkout_dir: Path,
    revision: str,
    base_url: str,
    site_dir: Path,
    lock_dir: Path,
) -> None:
    """Build MkDocs documentation with stop signal checking."""
    name = repo["name"]

    # Reset tracked changes and remove untracked/ignored checkout contents,
    # so the working tree matches the fetched commit.
    git(checkout_dir, "checkout", "--force", "--detach", revision)
    git(checkout_dir, "clean", "-fdx")

    docs_path = (checkout_dir / repo["docs"]).resolve()
    checkout_root = checkout_dir.resolve()

    if not docs_path.is_relative_to(checkout_root):
        raise ValueError(f"Docs path for {name} must be inside its checkout")

    if docs_path.is_file():
        mkdocs_config = docs_path
    elif (docs_path / "mkdocs.yml").is_file():
        mkdocs_config = docs_path / "mkdocs.yml"
    elif (docs_path / "mkdocs.yaml").is_file():
        mkdocs_config = docs_path / "mkdocs.yaml"
    else:
        raise FileNotFoundError(f"Missing MkDocs config in: {docs_path}")

    # Check for requirements.txt in docs directory (e.g. website/requirements.txt) or repo root
    req_candidates = [
        (docs_path if docs_path.is_dir() else docs_path.parent) / "requirements.txt",
        checkout_dir / "requirements.txt",
    ]
    for req_file in req_candidates:
        if req_file.is_file():
            install_requirements(req_file, lock_dir)
            break

    logger.info("Loading MkDocs configuration from %s", mkdocs_config)
    try:
        config = load_config(
            config_file=str(mkdocs_config),
            site_url=f"{base_url.rstrip('/')}/{name}/",
            site_dir=str(site_dir / name),
        )
    except Exception as exc:
        raise RuntimeError(
            f"Failed to load MkDocs configuration for {name}: {exc}"
        ) from exc

    logger.info("Building documentation for %s into %s", name, site_dir / name)
    try:
        build(config)
    except Exception as exc:
        raise RuntimeError(
            f"MkDocs build failed for {name} at revision {revision}: {exc}"
        ) from exc

def build_worker(payload: dict) -> int:
    """Sync/build one repo under the lock inherited from the manager."""
    repo = payload["repo"]
    name = repo["name"]
    try:
        checkout_dir, revision = sync_repository(repo, Path(payload["repo_dir"]))
        if revision == payload.get("last_built"):
            logger.info("%s already built at %s", name, revision)
            Path(payload["result_path"]).write_text(
                json.dumps({"revision": revision, "built": False}),
                encoding="utf-8",
            )
            return 0

        build_repository(
            repo=repo,
            checkout_dir=checkout_dir,
            revision=revision,
            base_url=payload["base_url"],
            site_dir=Path(payload["site_dir"]),
            lock_dir=Path(payload["lock_dir"]),
        )
        Path(payload["result_path"]).write_text(
            json.dumps({"revision": revision, "built": True}),
            encoding="utf-8",
        )
        return 0
    except Exception:
        logger.exception("Sync/build failed for %s", name)
        return 1


def launch_worker(repo: dict, repo_dir: Path, site_dir: Path, base_url: str,
                  last_revision: str | None,
                  lock_dir: Path,
                  lock_handles: list[TextIO]) -> tuple[subprocess.Popen, Path]:
    """Launch an isolated process; a process group lets timeout kill child tools too."""
    result_fd, result_name = tempfile.mkstemp(prefix="autowiki-result-")
    os.close(result_fd)
    result_path = Path(result_name)
    result_path.unlink()
    payload = {
        "repo": repo,
        "repo_dir": str(repo_dir),
        "site_dir": str(site_dir),
        "base_url": base_url,
        "lock_dir": str(lock_dir),
        "last_built": last_revision,
        "result_path": str(result_path),
    }
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--worker"],
        stdin=subprocess.PIPE,
        text=True,
        start_new_session=True,
        pass_fds=tuple(handle.fileno() for handle in lock_handles),
    )
    try:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(payload))
        proc.stdin.close()
    except Exception:
        terminate_process_group(proc)
        release_repository_locks(lock_handles)
        result_path.unlink(missing_ok=True)
        raise
    # Keep the manager's descriptors open too: this keeps the lock held until
    # the manager has observed/reaped the worker, even if the worker is killed.
    return proc, result_path


def stream_process_output(pipe, prefix: str) -> None:
    """Continuously read lines from a pipe and log them."""
    try:
        with pipe:
            for line in iter(pipe.readline, ""):
                cleaned = line.rstrip("\r\n")
                if cleaned:
                    logger.info("[%s] %s", prefix, cleaned)
    except Exception:
        logger.exception("Error reading output from %s", prefix)


def start_nginx() -> subprocess.Popen:
    t_proc = subprocess.run(
        ["nginx", "-t"],
        capture_output=True,
        text=True,
        check=False,
    )
    t_output = (t_proc.stdout + "\n" + t_proc.stderr).strip()
    for line in t_output.splitlines():
        cleaned = line.strip()
        if cleaned:
            logger.info("[nginx -t] %s", cleaned)

    if t_proc.returncode != 0:
        raise RuntimeError(
            f"Nginx configuration test failed (status {t_proc.returncode})"
        )

    proc = subprocess.Popen(
        ["nginx", "-g", "daemon off;"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    if proc.stdout:
        reader_thread = threading.Thread(
            target=stream_process_output,
            args=(proc.stdout, "nginx"),
            daemon=True,
        )
        reader_thread.start()

    # Detect an immediate startup failure.
    time.sleep(0.2)
    if proc.poll() is not None:
        raise RuntimeError(f"Nginx exited during startup (status {proc.returncode})")

    logger.info("Nginx started (pid=%s)", proc.pid)
    return proc


def stop_nginx(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return

    logger.info("Requesting graceful Nginx shutdown (pid=%s)", proc.pid)
    proc.send_signal(signal.SIGQUIT)

    try:
        proc.wait(timeout=30)
        logger.info("Nginx stopped gracefully")
    except subprocess.TimeoutExpired:
        logger.warning("Nginx did not stop within 30s; sending SIGTERM")
        proc.terminate()
        try:
            proc.wait(timeout=10)
            logger.info("Nginx stopped after SIGTERM")
        except subprocess.TimeoutExpired:
            logger.error("Nginx did not stop; sending SIGKILL")
            proc.kill()
            proc.wait()
            logger.info("Nginx killed with SIGKILL")


def deduplicate_repositories(repositories: list[dict]) -> list[dict]:
    """
    Deduplicate repositories by name, keeping the first occurrence.
    Logs a warning when duplicates are found.
    """
    seen_names: set[str] = set()
    unique_repos: list[dict] = []

    for repo in repositories:
        name = repo.get("name")
        if not name:
            logger.warning("Skipping repository without a name")
            continue
        if Path(name).name != name or name in {".", ".."}:
            logger.warning("Skipping repository with invalid name %r", name)
            continue
        if not isinstance(repo.get("git"), str) or not repo["git"].strip():
            logger.warning("Skipping repository %s without a Git remote", name)
            continue
        if not isinstance(repo.get("revision"), str) or not repo["revision"].strip():
            logger.warning("Skipping repository %s without a revision", name)
            continue
        if name in seen_names:
            logger.warning(
                "Duplicate repository name '%s' found, skipping. "
                "First occurrence wins.", name
            )
            continue
        seen_names.add(name)
        unique_repos.append(repo)

    if len(unique_repos) != len(repositories):
        logger.warning(
            "Skipped %d invalid or duplicate repository entries",
            len(repositories) - len(unique_repos),
        )

    return unique_repos


def deduplicate_repository_sources(repositories: list[dict]) -> list[dict]:
    """Keep the first configured name for each normalized Git remote."""
    seen_sources: set[str] = set()
    unique_repos: list[dict] = []
    for repo in repositories:
        identity = canonical_repository_identity(repo["git"])
        if identity in seen_sources:
            logger.warning(
                "Repository %s uses a remote already registered; first entry wins",
                repo["name"],
            )
            continue
        seen_sources.add(identity)
        unique_repos.append(repo)
    if len(unique_repos) != len(repositories):
        logger.warning(
            "Skipped %d duplicate repository source entries",
            len(repositories) - len(unique_repos),
        )
    return unique_repos


def build_catalog(repositories: list[dict], site_dir: Path) -> list[dict[str, str]]:
    """Build landing-page entries only for documentation sites already published."""
    catalog = []
    for repo in repositories:
        name = repo["name"]
        index_path = site_dir / name / "index.html"
        if not index_path.is_file() or index_path.resolve() == (site_dir / "index.html").resolve():
            continue
        catalog.append(
            {
                "name": name,
                "label": repo.get("label") or name,
                "url": f"/{name}/",
                "description": repo.get("description")
                or f"Documentation maintained from {name}.",
            }
        )
    return catalog


def install_landing_page(site_dir: Path) -> None:
    """Install the static landing page unless one is already present."""
    landing_page = site_dir / "index.html"
    if landing_page.exists():
        return
    site_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(LANDING_PAGE_TEMPLATE, landing_page)


def write_catalog(repositories: list[dict], site_dir: Path) -> None:
    """Atomically update the public JSON catalog for the landing page."""
    catalog_path = site_dir / "catalog.json"
    temp_path = catalog_path.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(build_catalog(repositories, site_dir), indent=2),
        encoding="utf-8",
    )
    os.replace(temp_path, catalog_path)


def run_poll_cycle(
    repositories: list[dict],
    repo_dir: Path,
    site_dir: Path,
    base_url: str,
    max_workers: int,
    build_timeout: float,
    lock_dir: Path,
    running: dict[str, tuple[subprocess.Popen, Path, float, list[TextIO], str]],
    source_locks: dict[str, str],
    start_new_builds: bool,
) -> None:
    """Poll repos without waiting for active workers; locks span poll cycles."""

    def reap_finished() -> None:
        for name, (proc, result_path, _started, locks, identity) in list(running.items()):
            code = proc.poll()
            if code is None:
                continue
            try:
                if code == 0 and result_path.exists():
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    revision = result["revision"]
                    if revision:
                        last_built[name] = revision
                        if result["built"]:
                            logger.info("Successfully built %s at revision %s", name, revision)
                elif code != 0:
                    logger.error("Worker for %s exited with status %d", name, code)
            except OSError:
                logger.exception("Could not read build result for %s", name)
            except (json.JSONDecodeError, KeyError, TypeError):
                logger.exception("Invalid build result from worker for %s", name)
            finally:
                result_path.unlink(missing_ok=True)
                running.pop(name)
                source_locks.pop(identity, None)
                release_repository_locks(locks)
                write_catalog(repositories, site_dir)

    reap_finished()

    now = time.monotonic()
    for name, (proc, result_path, started, _locks, _identity) in list(running.items()):
        if now - started >= build_timeout:
            logger.error("Build for %s exceeded %.0f seconds; terminating", name, build_timeout)
            terminate_process_group(proc)
            result_path.unlink(missing_ok=True)
    reap_finished()

    if stop_event.is_set() or not start_new_builds:
        return

    available = max_workers - len(running)
    for repo in repositories:
        if available <= 0 or stop_event.is_set():
            break
        name = repo["name"]
        identity = canonical_repository_identity(repo["git"])
        if name in running or identity in source_locks:
            continue

        locks = acquire_repository_locks(repo, lock_dir, repo_dir)
        if locks is None:
            logger.info(
                "Skipping %s: matching repository or checkout is already active",
                name,
            )
            continue
        try:
            proc, result_path = launch_worker(
                repo,
                repo_dir,
                site_dir,
                base_url,
                last_built.get(name),
                lock_dir,
                locks,
            )
        except Exception:
            release_repository_locks(locks)
            logger.exception("Could not start worker for %s", name)
            continue
        # If SIGTERM arrives as the process starts, this is registered now and
        # stopped at the next manager iteration before the cycle returns.
        source_locks[identity] = name
        running[name] = (proc, result_path, time.monotonic(), locks, identity)
        available -= 1
        logger.info("Started repository check for %s (pid=%s)", name, proc.pid)


def stop_nginx_and_workers(
    workers: dict[str, tuple[subprocess.Popen, Path, float, list[TextIO], str]],
    source_locks: dict[str, str],
    nginx_proc: subprocess.Popen | None,
) -> None:
    """Stop all builds concurrently before gracefully stopping nginx."""
    items = list(workers.items())
    for _name, (proc, _result_path, _started, _locks, _identity) in items:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    if nginx_proc is not None and nginx_proc.poll() is None:
        nginx_proc.send_signal(signal.SIGQUIT)

    for name, (proc, result_path, _started, locks, identity) in items:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.warning("Worker %s did not stop gracefully; terminating", name)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            if proc.poll() is None:
                proc.kill()
        proc.wait()
        result_path.unlink(missing_ok=True)
        release_repository_locks(locks)
        source_locks.pop(identity, None)
        workers.pop(name, None)

    stop_nginx(nginx_proc)


def main() -> None:
    global last_built
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    with CONFIG_PATH.open(encoding="utf-8") as file:
        settings = yaml.safe_load(file)

    repo_dir = Path(settings["repo_dir"])
    site_dir = Path(settings["site_dir"])
    repo_dir.mkdir(parents=True, exist_ok=True)
    site_dir.mkdir(parents=True, exist_ok=True)
    install_landing_page(site_dir)

    base_url = settings["site_url"].rstrip("/")
    poll_interval = settings.get(
        "poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS
    )
    build_timeout = settings.get(
        "build_timeout_seconds", DEFAULT_BUILD_TIMEOUT_SECONDS
    )
    max_concurrent_builds = settings.get(
        "max_concurrent_builds", DEFAULT_MAX_CONCURRENT_BUILDS
    )
    lock_dir = Path(settings.get("lock_dir", str(repo_dir / ".autowiki-locks")))

    # Deduplicate repositories - first occurrence wins
    repositories = deduplicate_repositories(settings.get("repositories", []))
    repositories = deduplicate_repository_sources(repositories)
    if not isinstance(poll_interval, (int, float)) or poll_interval <= 0:
        raise ValueError("poll_interval_seconds must be greater than zero")
    if not isinstance(build_timeout, (int, float)) or build_timeout <= 0:
        raise ValueError("build_timeout_seconds must be greater than zero")
    if not isinstance(max_concurrent_builds, int) or max_concurrent_builds <= 0:
        raise ValueError("max_concurrent_builds must be greater than zero")
    logger.info(
        "Loaded %d unique repositories (from %d total)",
        len(repositories),
        len(settings.get("repositories", []))
    )
    write_catalog(repositories, site_dir)

    nginx_proc = start_nginx()
    logger.info("Wiki runner started; polling every %s seconds", poll_interval)
    logger.info("Build timeout: %s seconds, Max concurrent: %s",
                build_timeout, max_concurrent_builds)

    running: dict[str, tuple[subprocess.Popen, Path, float, list[TextIO], str]] = {}
    source_locks: dict[str, str] = {}
    next_poll_at = 0.0
    try:
        while not is_stop_requested():
            now = time.monotonic()
            start_new_builds = now >= next_poll_at
            run_poll_cycle(
                repositories=repositories,
                repo_dir=repo_dir,
                site_dir=site_dir,
                base_url=base_url,
                max_workers=max_concurrent_builds,
                build_timeout=build_timeout,
                lock_dir=lock_dir,
                running=running,
                source_locks=source_locks,
                start_new_builds=start_new_builds,
            )
            if start_new_builds:
                next_poll_at = now + poll_interval

            if nginx_proc.poll() is not None:
                raise RuntimeError(
                    f"Nginx exited unexpectedly (status {nginx_proc.returncode})"
                )

            if not is_stop_requested():
                until_poll = max(0, next_poll_at - time.monotonic())
                stop_event.wait(min(0.2, until_poll) if until_poll else 0)

    except InterruptedError:
        logger.info("Interrupted, shutting down...")
    finally:
        stop_nginx_and_workers(running, source_locks, nginx_proc)
        logger.info("Wiki runner stopped")


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--worker":
        try:
            worker_payload = json.load(sys.stdin)
        except Exception:
            logger.exception("Could not read repository worker payload")
            raise SystemExit(2)
        raise SystemExit(build_worker(worker_payload))
    main()
