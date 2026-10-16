"""Scope-bound rules: observation, exact request delivery and host release."""
from dataclasses import asdict
import hashlib
import json
import re
from ..Port.Out.SkillStorage import SkillLedger, SkillSources


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SkillLifecycle:
    def __init__(self, *, ledger: SkillLedger, sources: SkillSources, owner: str, attempt: str, available_tools=(), retained_rules=False):
        self.ledger, self.sources, self.owner, self.attempt = ledger, sources, owner, attempt
        self.available_tools = frozenset(available_tools)
        self.retained_rules = retained_rules is True
        # Runtime constructs this service only after admitting its activation.
        # A resumed attempt takes over the persisted scope; a late callback
        # from the old in-memory service cannot release the newer activation.
        existing = self.snapshot()
        if any(i["state"] != "RELEASED" for i in existing["invocations"]) or existing.get("blocked"):
            def adopt(state):
                if state.get("bound_attempt") != self.attempt:
                    state["bound_attempt"] = self.attempt
                    for item in state["invocations"]:
                        if item["state"] != "RELEASED":
                            item["state"] = "PENDING_READ"
            self.ledger.change(self.owner, adopt, event="attempt_bound")

    def snapshot(self):
        return self.ledger.read(self.owner)

    def _check_attempt(self, state):
        live = any(i["state"] != "RELEASED" for i in state["invocations"]) or state.get("blocked")
        if live and state.get("bound_attempt", self.attempt) != self.attempt:
            raise ValueError("Skill scope belongs to a newer admitted attempt")

    def invoke(self, name, *, arguments="", branch="", caller="model"):
        if caller not in {"model", "user"}:
            raise ValueError("Skill caller must be assigned by the host")
        if not isinstance(arguments, str) or len(arguments.encode("utf-8")) > 16384:
            raise ValueError("Skill arguments exceed 16384 UTF-8 bytes")
        if not isinstance(branch, str):
            raise ValueError("Skill branch must be text")
        document = self.sources.load(name)
        metadata = document.metadata
        if (caller == "model" and metadata.disable_model_invocation) or (caller == "user" and not metadata.user_invocable):
            raise ValueError(f"Skill {name} does not permit the {caller} invocation entry")
        unsupported = [d for d in metadata.diagnostics if d.startswith("Unsupported execution field:")]
        if unsupported:
            raise ValueError("; ".join(unsupported))
        if re.search(r"!`[^`]*`", document.body):
            raise ValueError("Skill shell pre-execution macros are unsupported; use explicit permitted tools")
        missing = set(metadata.required_tools) - self.available_tools
        if missing:
            raise ValueError("Skill requires unavailable tools: " + ", ".join(sorted(missing)))
        if branch and branch not in metadata.branches:
            raise ValueError(f"Unknown Skill branch: {branch}")
        paths = tuple(dict.fromkeys((*metadata.required_resources, *metadata.branches.get(branch, ()))))
        observed = [self.sources.resource(document, path) for path in paths]
        resources = list({resource.path: asdict(resource) for resource in observed}.values())
        rendered = re.sub(r"\$ARGUMENTS\b|\{args\}", lambda _: arguments, document.body)
        if arguments and rendered == document.body:
            rendered += "\n\nInvocation arguments (literal user/task data):\n" + arguments
        if len(rendered.encode("utf-8")) + sum(len(r["content"].encode("utf-8")) for r in resources) > 1_048_576:
            raise ValueError("Skill and required resources exceed the 1 MiB material limit; split the workflow")
        identity = {"name": name, "source": document.source, "path": document.path,
                    "package_root": document.package_root, "source_hash": document.source_hash,
                    "version": document.version, "arguments": arguments, "branch": branch,
                    "rendered_hash": _hash(rendered), "resources": [{"path": r["path"], "source_hash": r["source_hash"]} for r in resources]}
        key = _hash(json.dumps(identity, ensure_ascii=False, sort_keys=True))

        def register(state):
            self._check_attempt(state)
            state["bound_attempt"] = self.attempt
            active = [item for item in state["invocations"] if item["state"] != "RELEASED"]
            for item in active:
                if item["name"] != name and (item["name"] in metadata.conflicts or name in item.get("conflicts", [])):
                    raise ValueError("Conflicting active Skill: " + item["name"])
                if item["key"] == key:
                    # Re-delivery is checked on every actual request. A previous
                    # successful load alone never proves present visibility.
                    return {**item, "reused": True}
            if len([item for item in active if item["name"] != name]) >= 16:
                raise ValueError("At most 16 Skill invocations may be active in one owner scope")
            item = {**identity, "id": "skill-" + _hash(self.owner + key + str(state["revision"]))[:24],
                    "key": key, "owner": self.owner, "attempt": self.attempt, "caller": caller,
                    "state": "PENDING_READ", "body": rendered, "raw_body": document.body,
                    "source_text": document.source_text,
                    "resources": resources, "conflicts": list(metadata.conflicts),
                    "diagnostics": list(metadata.diagnostics), "delivery_count": 0}
            # Selecting different parameters or a branch explicitly replaces the
            # previous invocation of this same Skill within this owner only.
            for old in active:
                if old["name"] == name:
                    old.update(state="RELEASED", release_reason="invocation_replaced")
            state["invocations"].append(item)
            state["blocked"] = {key: value for key, value in state.get("blocked", {}).items() if key != name}
            return item
        return self.ledger.change(self.owner, register, event="invoked")

    def required(self):
        snapshot = self.snapshot()
        self._check_attempt(snapshot)
        if snapshot.get("blocked"):
            raise ValueError("Invalidated Skill requires explicit reload or host release: " + ", ".join(snapshot["blocked"]))
        items = [item for item in snapshot["invocations"] if item["state"] != "RELEASED"]
        invalid = {}
        for item in items:
            try:
                if "load_skill" not in self.available_tools and not self.retained_rules:
                    raise ValueError("load_skill is no longer available in this owner")
                document = self.sources.load(item["name"])
                if (document.source_hash != item["source_hash"] or document.source != item["source"]
                        or document.path != item["path"] or document.package_root != item["package_root"]):
                    raise ValueError("Skill source/version changed")
                if ((item["caller"] == "model" and document.metadata.disable_model_invocation)
                        or (item["caller"] == "user" and not document.metadata.user_invocable)
                        or (not self.retained_rules and set(document.metadata.required_tools) - self.available_tools)):
                    raise ValueError("Skill invocation eligibility changed")
                if _hash(item["body"]) != item["rendered_hash"]:
                    raise ValueError("Skill rendered body integrity check failed")
                # Request-local metadata from the freshly authorized document,
                # including invocations persisted before this field existed.
                # It can select schemas but never grants tool capabilities.
                item["required_tools"] = list(document.metadata.required_tools)
                for resource in item["resources"]:
                    observed = self.sources.resource(document, resource["path"])
                    if observed.source_hash != resource["source_hash"] or observed.content != resource["content"]:
                        raise ValueError("Skill required resource changed: " + resource["path"])
            except (OSError, ValueError, RuntimeError) as error:
                invalid[item["id"]] = str(error)
        if invalid:
            def invalidate(state):
                self._check_attempt(state)
                for item in state["invocations"]:
                    if item["id"] in invalid:
                        item.update(state="RELEASED", release_reason="source_invalidated", invalidation=invalid[item["id"]])
                        state.setdefault("blocked", {})[item["name"]] = invalid[item["id"]]
            self.ledger.change(self.owner, invalidate, event="invalidated")
            raise ValueError("Skill rules invalidated; reload before continuing: " + "; ".join(invalid.values()))
        return items

    def delivered(self, identities, *, request_hash):
        expected = dict(identities)
        def activate(state):
            self._check_attempt(state)
            active = {item["id"]: item for item in state["invocations"] if item["state"] != "RELEASED"}
            if set(expected) != set(active) or any(active[key]["key"] != value for key, value in expected.items()):
                raise ValueError("Skill state changed between compilation and request submission")
            for item in active.values():
                item.update(state="ACTIVE", delivered_attempt=self.attempt, last_request_hash=request_hash,
                            delivery_count=item["delivery_count"] + 1)
        self.ledger.change(self.owner, activate, event="request_submitted")

    def pending(self):
        state = self.snapshot()
        self._check_attempt(state)
        return bool(state.get("blocked")) or any(item["state"] != "RELEASED" and (item["state"] != "ACTIVE" or item.get("delivered_attempt") != self.attempt)
                   for item in state["invocations"])

    def release(self, reason):
        if reason not in {"completed", "canceled", "explicit_switch", "host_ended"}:
            raise ValueError("Skill release requires an explicit host lifecycle reason")
        def release(state):
            if state.get("bound_attempt", self.attempt) != self.attempt:
                return False
            state["blocked"] = {}
            for item in state["invocations"]:
                if item["state"] != "RELEASED":
                    item.update(state="RELEASED", release_reason=reason)
            return True
        self.ledger.change(self.owner, release, event="released:" + reason)
