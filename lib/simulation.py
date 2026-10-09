"""Local build/cache/release mechanics. No real applications are deployed."""

# Exercise infrastructure: do not modify unless fixing an exercise bug.
# Commit infrastructure fixes separately; normal solution changes belong in pipeline.py.

from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import tempfile
import time

from lib.fixture import git

APPS = ("zorch", "greeb", "blerg")
BUILD_SECONDS = {"zorch": 0.3, "greeb": 0.6, "blerg": 0.6}


@contextmanager
def file_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
        temporary = Path(out.name)
        try:
            json.dump(value, out, sort_keys=True)
            out.write("\n")
            out.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


class Simulation:
    """Shared state lives in state_dir; observer(event) runs after each logged event.

    speed scales delays (0 removes them). fail_build(revision, app) may return
    True to simulate a failed cache miss. fail_deploy(revision, app) may fail
    a rollout before publishing deployed state. Callbacks may block or raise.
    """

    def __init__(self, repo, state_dir, *, speed=1, observer=None, fail_build=None,
                 fail_deploy=None):
        if speed < 0:
            raise ValueError("speed must be nonnegative")
        self.repo = Path(repo)
        self.state = Path(state_dir)
        self.state.mkdir(parents=True, exist_ok=True)
        self.speed = speed
        self.observer = observer
        self.fail_build = fail_build
        self.fail_deploy = fail_deploy

    def lock(self, name):
        return file_lock(self.state / "locks" / f"{name}.lock")

    def event(self, kind, revision, **fields):
        with self.lock("events"):
            event = dict(kind=kind, revision=revision, time=time.monotonic(), **fields)
            with (self.state / "events.jsonl").open("a") as out:
                out.write(json.dumps(event, sort_keys=True) + "\n")
        if self.observer:
            self.observer(event)

    def resolve(self, revision):
        return git(self.repo, "rev-parse", "--verify", f"{revision}^{{commit}}")

    def cache_key(self, revision, app):
        if app not in APPS:
            raise ValueError(f"unknown app: {app}")
        inputs = {app: git(self.repo, "rev-parse", f"{revision}:source/{app}.txt")}
        if app == "blerg":
            inputs["greeb"] = self.cache_key(revision, "greeb")
        encoded = json.dumps(["build-v1", app, inputs], sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()

    def artifact_path(self, revision, app):
        return self.state / "artifacts" / revision / f"{app}.json"

    def _image_content(self, revision, app):
        content = {"app": app, "key": self.cache_key(revision, app)}
        if app == "blerg":
            content["greeb_key"] = self.cache_key(revision, "greeb")
        return content

    @staticmethod
    def _image_digest(content):
        encoded = json.dumps(["image-v1", content], sort_keys=True).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def image_digest(self, revision, app):
        return self._image_digest(self._image_content(revision, app))

    def image_tag(self, revision, app):
        if app not in APPS:
            raise ValueError(f"unknown app: {app}")
        blob = git(self.repo, "rev-parse", f"{revision}:source/{app}.txt")
        return f"{app}-{blob}"

    @staticmethod
    def _validate_reference(reference):
        if reference not in ("tag", "digest"):
            raise ValueError(f"unknown image reference: {reference}")

    @staticmethod
    def _read_registry(path):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError) as error:
            raise ValueError(f"missing or invalid registry entry: {path}") from error

    def _validate_image(self, record, app, digest):
        if not isinstance(record, dict):
            raise ValueError("invalid image record")
        content = {key: value for key, value in record.items() if key != "digest"}
        fields = {"app", "key", "greeb_key"} if app == "blerg" else {"app", "key"}
        keys = [content.get("key")]
        if app == "blerg":
            keys.append(content.get("greeb_key"))
        if (set(content) != fields or content.get("app") != app
                or any(not isinstance(key, str) or len(key) != 64
                       or any(char not in "0123456789abcdef" for char in key) for key in keys)
                or record.get("digest") != digest
                or self._image_digest(content) != digest):
            raise ValueError("invalid image record or digest")
        return record

    def resolve_image(self, revision, app, reference="tag"):
        self._validate_reference(reference)
        digest = self.image_digest(revision, app)
        if reference == "tag":
            tag = self.image_tag(revision, app)
            pointer = self._read_registry(self.state / "registry" / "tags" / f"{tag}.json")
            if not isinstance(pointer, dict) or set(pointer) != {"app", "tag", "digest"}:
                raise ValueError("invalid image tag")
            digest = pointer["digest"]
            if pointer["app"] != app or pointer["tag"] != tag:
                raise ValueError("invalid image tag")
        if (not isinstance(digest, str) or not digest.startswith("sha256:")
                or len(digest) != 71
                or any(char not in "0123456789abcdef" for char in digest[7:])):
            raise ValueError("invalid image digest")
        path = self.state / "registry" / "images" / f"{digest[7:]}.json"
        return self._validate_image(self._read_registry(path), app, digest)

    def publish_image(self, revision, app):
        revision = self.resolve(revision)
        content = self._image_content(revision, app)
        digest = self._image_digest(content)
        record = dict(content, digest=digest)
        path = self.state / "registry" / "images" / f"{digest[7:]}.json"
        with self.lock(f"image-{digest[7:]}"):
            if path.exists():
                self._validate_image(self._read_registry(path), app, digest)
            else:
                write_json(path, record)
        tag = self.image_tag(revision, app)
        write_json(self.state / "registry" / "tags" / f"{tag}.json",
                   {"app": app, "tag": tag, "digest": digest})
        self.event("image_published", revision, app=app, tag=tag, digest=digest)
        return record

    def build(self, revision, app, dependency=None):
        revision = self.resolve(revision)
        key = self.cache_key(revision, app)
        if app == "blerg":
            self.validate_artifact(revision, "greeb", dependency)
        self.event("build_requested", revision, app=app, key=key)
        try:
            with self.lock(f"cache-{key}"):
                cache = self.state / "cache" / f"{key}.json"
                hit = cache.exists()
                if hit:
                    payload = json.loads(cache.read_text())
                    if payload != {"app": app, "key": key}:
                        raise ValueError("invalid cache entry")
                    self.event("cache_hit", revision, app=app, key=key)
                else:
                    self.event("build_started", revision, app=app, key=key)
                    time.sleep(BUILD_SECONDS[app] * self.speed)
                    if self.fail_build and self.fail_build(revision, app):
                        raise RuntimeError(f"simulated build failure: {app}")
                    payload = {"app": app, "key": key}
                    write_json(cache, payload)
                    self.event("cache_published", revision, app=app, key=key)
                artifact = dict(payload, revision=revision)
                write_json(self.artifact_path(revision, app), artifact)
            self.publish_image(revision, app)
            self.event("build_finished", revision, app=app, key=key, cache_hit=hit)
            return artifact
        except Exception:
            self.event("build_failed", revision, app=app, key=key)
            raise

    def validate_artifact(self, revision, app, artifact):
        expected = {"revision": revision, "app": app,
                    "key": self.cache_key(revision, app)}
        if artifact != expected:
            raise ValueError(f"invalid {app} artifact for {revision}")
        path = self.artifact_path(revision, app)
        if not path.exists() or json.loads(path.read_text()) != expected:
            raise ValueError(f"missing {app} artifact for {revision}")

    def deployed(self, app):
        if app not in APPS:
            raise ValueError(f"unknown app: {app}")
        path = self.state / "deployments" / f"{app}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def deploy(self, revision, app, artifact, *, image_reference="tag"):
        self._validate_reference(image_reference)
        revision = self.resolve(revision)
        self.validate_artifact(revision, app, artifact)
        self.event("deploy_requested", revision, app=app)
        try:
            expected = self.image_digest(revision, app)
            current = self.deployed(app)
            if current and current["image"]["digest"] == expected:
                self.event("deploy_skipped", revision, app=app, reason="already deployed")
                return
            self.event("deploy_started", revision, app=app)
            time.sleep(0.2 * self.speed)
            image = self.resolve_image(revision, app, image_reference)
            self.event("image_resolved", revision, app=app, image=image,
                       image_reference=image_reference)
            if self.fail_deploy and self.fail_deploy(revision, app):
                raise RuntimeError(f"simulated deployment failure: {app}")
            write_json(self.state / "deployments" / f"{app}.json",
                       {"revision": revision, "artifact": artifact, "image": image,
                        "image_reference": image_reference})
            self.event("deploy_finished", revision, app=app)
        except Exception:
            self.event("deploy_failed", revision, app=app)
            raise

    def events(self):
        with self.lock("events"):
            path = self.state / "events.jsonl"
            return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
